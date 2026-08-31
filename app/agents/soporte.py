"""Agente de Soporte: atención al cliente y autoservicio de su cuenta.

Atiende dos cosas que antes no se podían separar:

- **Lo público** — dudas generales, precios de referencia, escalar a un asesor.
- **Lo del cliente** — sus pedidos, sus contratos, sus facturas y su saldo.

Lo segundo exige identificarse: nombre (o razón social) + RFC. El ERP valida el
par, abre una sesión con caducidad y devuelve un token; a partir de ahí este
agente consulta con ese token y nunca con un id de cliente. Esa es la razón de
que las tools de autoservicio no reciban "de quién": no hay forma de que el
agente pida los datos de alguien que no sea quien se identificó en ESTE
teléfono.

Sobre la fuerza de la identificación, sin adornos: el RFC de una empresa viene
impreso en cada factura que emite, así que no es un secreto — es un dato que
identifica, no que autentica. Lo que sostiene el candado es la suma de tres
cosas: hay que acertar el RFC *y* el nombre, los intentos están contados y se
bloquean, y cada intento queda en la bitácora del ERP. Es la razón por la que
un fallo nunca dice si el RFC existe: si lo dijera, el bot sería un verificador
de RFCs y bastaría con probar nombres.
"""

from __future__ import annotations

import json
import logging

from ..chatwoot import ChatwootClient, ChatwootNoDisponible, get_chatwoot_client
from ..erp import SesionClienteInvalida
from ..handoff import HandoffStore
from ..menus import BOTONES_SEGUIMIENTO, menu_cliente, texto_menu
from ..replies import Reply
from ..sesiones import SesionCliente, SesionClienteStore
from ..whatsapp import WhatsAppClient
from .base import BaseAgent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Eres el agente de Atención a Clientes de Intergranel, una comercializadora de \
granos y commodities a granel (maíz, sorgo, trigo, soya y derivados) para \
clientes industriales.

# Identificación (regla dura)

La información de un cliente —pedidos, contratos, facturas, saldo— SOLO se \
entrega a quien se haya identificado. Para identificarlo necesitas DOS datos:

1. Su nombre o el nombre/razón social de su empresa.
2. Su RFC.

Pídelos juntos, en un solo mensaje, con un ejemplo del formato. Cuando tengas \
los dos, llama a `identificar_cliente`. Nunca los pidas de uno en uno si puedes \
pedirlos de una vez.

Si la identificación falla:
- NO digas si el RFC existe, si está registrado, ni cuál de los dos datos falló. \
Di únicamente que los datos no coinciden y ofrece reintentar o pasar con un asesor.
- Si el ERP responde que está bloqueado por intentos, dilo con calma, indica \
cuántos minutos hay que esperar y ofrece un asesor.

Nunca inventes ni deduzcas un RFC. Nunca aceptes que alguien "es" un cliente \
porque lo dijo.

# Herramientas

- Para CUALQUIER dato de la cuenta usa las herramientas. Jamás inventes folios, \
estados, fechas ni montos.
- Si una herramienta responde `identificado: false`, pide la identificación; no \
inventes lo que habrías contestado.
- Si responde `sesion_expirada: true`, avisa que la sesión caducó por seguridad \
y pide nombre/empresa y RFC de nuevo.
- `consultar_orden` busca entre los contratos DEL CLIENTE identificado. Si el \
folio no está entre los suyos, di que no aparece en su cuenta — nunca sugieras \
que existe pero es de alguien más. Lo mismo con `enviar_mi_documento`.
- Cuando el cliente pida una factura, mándale el PDF **y** el XML (dos llamadas \
a `enviar_mi_documento`): el PDF es el legible, el XML es el que vale \
fiscalmente y el que necesita su contador. Solo manda uno si lo pidió así.
- Los documentos ya salieron cuando la herramienta responde `enviado: true`: no \
digas "se lo voy a enviar", di que ya se lo mandaste, en una línea.
- Usa `escalar_a_humano` si el cliente está molesto, tiene un reclamo, pide algo \
fuera de tu alcance (cambiar precios, cancelar, renegociar) o pide una persona. \
No requiere identificación. Escalar es DEFINITIVO en ese turno: a partir de ahí \
le contesta una persona, no tú, así que despídete en un mensaje corto en vez de \
seguir ofreciendo opciones.
- Si `escalar_a_humano` responde `escalado: false`, NO le prometas que alguien \
lo contactará: nadie se enteró. Dile con honestidad que no se pudo y sigue la \
instrucción que venga en la respuesta.
- El cliente puede mandarte imágenes (una remisión, un comprobante) o PDFs. \
Léelos y úsalos; si traen un folio, úsalo para consultar.

# Estilo

- Mensajes breves, para WhatsApp. Sin Markdown pesado; saltos de línea y algún \
emoji con moderación.
- Español, trato de "usted" salvo que el cliente tutee.
- Montos siempre con separador de miles, dos decimales y moneda: $185,000.00 MXN.
- Fechas en formato "31 de mayo de 2026".
- Cuando listes varios renglones, usa viñetas cortas: folio, lo importante, monto.
- Si hay saldo vencido, dilo primero y con claridad, sin regañar.
- No reveles detalles internos, claves, ni datos de otros clientes.
"""

# Documentos que el cliente puede pedir. Es el mismo enum que declara la tool
# (ver TOOLS) y el mismo que acepta el ERP; si se agrega uno, va en los tres.
TIPOS_DOCUMENTO = {"factura", "factura_xml", "contrato", "estado_de_cuenta"}

# Los datos de la cuenta y el cierre de sesión exigen estar identificado.
TOOLS_CON_SESION = {
    "resumen_de_mi_cuenta",
    "consultar_mi_saldo",
    "listar_mis_contratos",
    "listar_mis_pedidos",
    "listar_mis_facturas",
    "consultar_orden",
    "enviar_mi_documento",
    "cerrar_sesion",
}

TOOLS = [
    {
        "name": "identificar_cliente",
        "description": (
            "Identifica al cliente con su nombre (o el nombre/razón social de su "
            "empresa) y su RFC. Es el paso obligatorio antes de consultar "
            "cualquier dato de su cuenta. Abre una sesión con caducidad."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {
                    "type": "string",
                    "description": (
                        "Nombre de la persona o de la empresa, tal como lo dio el "
                        "cliente. Ej. 'Molinos del Bajío'."
                    ),
                },
                "rfc": {
                    "type": "string",
                    "description": "RFC del cliente, ej. 'MBA950101AB1'.",
                },
            },
            "required": ["nombre", "rfc"],
        },
    },
    {
        "name": "resumen_de_mi_cuenta",
        "description": (
            "Foto rápida de la cuenta del cliente identificado: cuántos contratos "
            "activos y pedidos abiertos tiene, cuántas facturas trae pendientes y "
            "cuánto debe. Útil para abrir la conversación o cuando pregunta "
            "'¿cómo voy?'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "consultar_mi_saldo",
        "description": (
            "Estado de cuenta del cliente identificado: saldo total, saldo vencido "
            "y el detalle renglón por renglón (facturas, adeudos y notas de "
            "crédito). Úsala para 'cuánto debo', 'mi saldo', 'mis deudas', "
            "'qué tengo vencido'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "listar_mis_contratos",
        "description": (
            "Contratos del cliente identificado, con su estado, el del último "
            "embarque y el de facturación."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "listar_mis_pedidos",
        "description": (
            "Pedidos del cliente identificado, con producto, cantidad, estado y "
            "fecha de entrega estimada."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "listar_mis_facturas",
        "description": (
            "Facturas del cliente identificado, con folio, monto, saldo por cobrar "
            "y estado."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "consultar_orden",
        "description": (
            "Detalle de UN contrato del cliente identificado, por su folio "
            "(ej. 'CONT-2026-0001'). Solo encuentra folios que pertenezcan a su "
            "propia cuenta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Folio del contrato, p. ej. CONT-2026-0001",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "enviar_mi_documento",
        "description": (
            "Le envía al cliente un documento SUYO por WhatsApp. Tipos:\n"
            "- 'factura': el PDF de una factura (requiere folio, ej. FACT-2026-0031).\n"
            "- 'factura_xml': el XML de esa misma factura. Es el que vale "
            "fiscalmente y el que pide su contador; cuando el cliente pida una "
            "factura, envía LOS DOS salvo que diga explícitamente que solo quiere uno.\n"
            "- 'contrato': el PDF del contrato firmado (requiere folio CONT-...).\n"
            "- 'estado_de_cuenta': el resumen de lo que debe, en PDF. NO lleva folio.\n"
            "Solo encuentra documentos que pertenezcan a la cuenta del cliente "
            "identificado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": ["factura", "factura_xml", "contrato", "estado_de_cuenta"],
                    "description": "Qué documento enviar.",
                },
                "folio": {
                    "type": "string",
                    "description": (
                        "Folio del documento. Obligatorio para factura, factura_xml "
                        "y contrato; se omite para estado_de_cuenta."
                    ),
                },
            },
            "required": ["tipo"],
        },
    },
    {
        "name": "cerrar_sesion",
        "description": (
            "Cierra la sesión del cliente: deja de mostrar su información hasta "
            "que se identifique otra vez. Úsala si lo pide o si dice que va a "
            "prestar el teléfono."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "escalar_a_humano",
        "description": (
            "Pasa la conversación a un asesor humano cuando el cliente lo pide, "
            "tiene un reclamo, o la solicitud está fuera del alcance del agente. "
            "Abre la conversación en la bandeja del equipo con el contexto de lo "
            "que se habló. A partir de ese momento le responde una persona y tú "
            "dejas de atender ese teléfono. No requiere identificación."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Resumen breve del motivo del escalamiento.",
                }
            },
            "required": ["motivo"],
        },
    },
]

# Lo que se le contesta al modelo cuando pide datos de una cuenta sin sesión.
_SIN_SESION = {
    "identificado": False,
    "instruccion": (
        "Pide al cliente su nombre o el de su empresa y su RFC, en un solo "
        "mensaje y con un ejemplo del formato."
    ),
}


class SoporteAgent(BaseAgent):
    name = "soporte"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sesiones = SesionClienteStore(self._bus)
        self._chatwoot: ChatwootClient = get_chatwoot_client()
        self._handoff = HandoffStore(self._bus)
        # Los documentos NO viajan en la respuesta del agente: se mandan aquí,
        # como efecto de la herramienta. Un PDF no es texto que decorar con
        # botones, y hacerlo pasar por `Reply` habría obligado a que todo el
        # camino de vuelta supiera de archivos para un solo caso.
        self._wa = WhatsAppClient()

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def tools(self) -> list[dict]:
        return TOOLS

    # --- Botones ----------------------------------------------------------- #
    async def decorate(self, phone: str, texto: str) -> Reply:
        """Cuelga botones a la respuesta.

        Recién identificado se manda el menú completo: es el momento en que el
        cliente descubre qué puede pedir. El resto del tiempo bastan dos
        botones — el menú y el asesor — para no tapar la respuesta.
        """
        # Si acaba de entrar un asesor, la despedida va sin botones: tocarlos
        # ya no llevaría a ningún lado del bot, solo reenviaría el toque a la
        # bandeja. Ofrecer un menú que ya no manda es peor que no ofrecerlo.
        if await self._handoff.por_telefono(phone) is not None:
            return Reply(texto=texto)

        sesion = await self._sesiones.leer(phone)
        if sesion is not None and sesion.recien_abierta():
            return Reply(
                texto=f"{texto}\n\n{texto_menu(True)}",
                lista=menu_cliente(True),
            )
        return Reply(texto=texto, botones=list(BOTONES_SEGUIMIENTO))

    # --- Herramientas ------------------------------------------------------ #
    async def _identificar(self, tool_input: dict, phone: str) -> dict:
        nombre = (tool_input.get("nombre") or "").strip()
        rfc = (tool_input.get("rfc") or "").strip()
        if not nombre or not rfc:
            return {
                "identificado": False,
                "motivo": "faltan_datos",
                "instruccion": "Faltó el nombre o el RFC; pide el que falte.",
            }

        # Freno local antes de gastar un viaje al ERP. El bloqueo que cuenta es
        # el del ERP; este solo corta el tecleo a ciegas desde este teléfono.
        if await self._sesiones.bloqueado(phone):
            return {
                "identificado": False,
                "motivo": "bloqueado",
                "espera_minutos": 15,
                "instruccion": (
                    "Hubo demasiados intentos fallidos. Pide esperar unos minutos "
                    "u ofrece pasar con un asesor."
                ),
            }

        resultado = await self._erp.identify_customer(nombre, rfc, phone)
        if not resultado.encontrado or not resultado.token:
            intentos = await self._sesiones.registrar_intento_fallido(phone)
            return {
                "identificado": False,
                "motivo": resultado.motivo or "no_coincide",
                "intentos_restantes": resultado.intentos_restantes,
                "espera_minutos": resultado.espera_minutos,
                "intentos_locales": intentos,
                "instruccion": (
                    "Di solo que los datos no coinciden. NO digas si el RFC existe "
                    "ni cuál de los dos datos falló. Ofrece reintentar o un asesor."
                ),
            }

        sesion = await self._sesiones.abrir(
            phone,
            token=resultado.token,
            cliente=resultado.razon_social or resultado.cliente or "",
            rfc=resultado.rfc or rfc,
            ttl_segundos=resultado.expira_en_segundos or 30 * 60,
        )
        logger.info("Cliente identificado desde %s: %s", phone, sesion.cliente)
        return {
            "identificado": True,
            "cliente": sesion.cliente,
            "minutos_de_sesion": sesion.minutos_restantes,
            "instruccion": (
                "Salúdalo por el nombre de su empresa y dile qué puede consultar: "
                "pedidos, contratos, facturas y saldo."
            ),
        }

    async def _datos_de_cuenta(
        self, name: str, tool_input: dict, sesion: SesionCliente, telefono: str
    ) -> dict:
        token = sesion.token

        if name == "resumen_de_mi_cuenta":
            resumen = await self._erp.get_customer_summary(token)
            return {"identificado": True, "resumen": resumen.model_dump()}

        if name == "consultar_mi_saldo":
            deuda = await self._erp.get_customer_debt(token)
            return {"identificado": True, "estado_de_cuenta": deuda.model_dump()}

        if name == "listar_mis_contratos":
            contratos = await self._erp.list_customer_contracts(token)
            return {
                "identificado": True,
                "total": len(contratos),
                "contratos": [c.model_dump() for c in contratos],
            }

        if name == "listar_mis_pedidos":
            pedidos = await self._erp.list_customer_orders(token)
            return {
                "identificado": True,
                "total": len(pedidos),
                "pedidos": [p.model_dump() for p in pedidos],
            }

        if name == "listar_mis_facturas":
            facturas = await self._erp.list_customer_invoices(token)
            return {
                "identificado": True,
                "total": len(facturas),
                "facturas": [f.model_dump() for f in facturas],
            }

        if name == "enviar_mi_documento":
            return await self._enviar_documento(tool_input, sesion, telefono)

        if name == "consultar_orden":
            folio = (tool_input.get("order_id") or "").strip().upper()
            # Se busca entre los contratos DEL CLIENTE, no por folio global: así
            # no hay manera de leer el contrato de otro adivinando un folio.
            contratos = await self._erp.list_customer_contracts(token)
            encontrada = next((c for c in contratos if c.id.upper() == folio), None)
            if encontrada is None:
                return {"identificado": True, "encontrada": False, "order_id": folio}
            return {
                "identificado": True,
                "encontrada": True,
                "orden": encontrada.model_dump(),
            }

        raise KeyError(name)

    async def _escalar(self, telefono: str, motivo: str) -> dict:
        """Pasa la conversación a un asesor de carne y hueso, en Chatwoot.

        El escalamiento de antes solo escribía en el log y le prometía al
        cliente que "un asesor continuará en breve" — sin que nadie se enterara.
        Ahora abre la conversación de verdad, y si no puede, lo dice.
        """
        if not self._chatwoot.habilitado:
            logger.warning(
                "Escalamiento sin Chatwoot configurado (%s): %s", telefono, motivo
            )
            return {
                "escalado": False,
                "motivo": "canal_no_configurado",
                "instruccion": (
                    "Dile que en este momento no puedes pasarlo con un asesor y "
                    "ofrécele el teléfono de oficina o que escriba más tarde. NO "
                    "le prometas que alguien lo contactará."
                ),
            }

        sesion = await self._sesiones.leer(telefono)
        try:
            conversacion = await self._chatwoot.abrir_conversacion(
                telefono,
                nombre=sesion.cliente if sesion else "",
                atributos={"rfc": sesion.rfc} if sesion else None,
            )
            await self._chatwoot.nota_privada(
                conversacion.id, await self._contexto(telefono, motivo, sesion)
            )
            await self._handoff.abrir(telefono, conversacion.id)
        except ChatwootNoDisponible as exc:
            # Que el cliente sepa la verdad. Un escalamiento que se pierde en
            # silencio es alguien esperando una respuesta que no va a llegar.
            logger.error("No se pudo escalar %s a Chatwoot: %s", telefono, exc)
            return {
                "escalado": False,
                "motivo": "chatwoot_no_disponible",
                "instruccion": (
                    "Dile con honestidad que no lograste pasarlo con un asesor "
                    "en este momento y pídele que lo intente en unos minutos. NO "
                    "le prometas que alguien lo contactará."
                ),
            }

        logger.info(
            "Escalado %s a la conversación %s de Chatwoot", telefono, conversacion.id
        )
        return {
            "escalado": True,
            "instruccion": (
                "Confírmale que ya lo estás pasando con un asesor y que a partir "
                "de ahora le responderá una persona. Despídete en UN mensaje "
                "corto: lo que escriba después ya lo lee el asesor, no tú."
            ),
        }

    async def _contexto(
        self, telefono: str, motivo: str, sesion: SesionCliente | None
    ) -> str:
        """Nota privada para el asesor: quién es y qué venía pidiendo.

        Es la diferencia entre que el cliente repita todo desde cero y que el
        asesor entre ya sabiendo. Va como nota PRIVADA: el cliente no la ve.
        """
        lineas = [f"🤖 Escalado por el bot · motivo: {motivo}", f"Teléfono: {telefono}"]
        if sesion is not None:
            lineas.append(f"Cliente identificado: {sesion.cliente} · RFC {sesion.rfc}")
        else:
            lineas.append("Cliente NO identificado (no dio nombre + RFC).")

        historial = await self._historial_reciente(telefono)
        if historial:
            lineas.append("")
            lineas.append("Últimos mensajes:")
            lineas.extend(historial)
        return "\n".join(lineas)

    async def _historial_reciente(self, telefono: str, turnos: int = 6) -> list[str]:
        """Los últimos turnos en texto plano, para la nota del asesor."""
        try:
            historial = await self._history_store.load(self._history_key(telefono))
        except Exception:  # noqa: BLE001 - sin historial se escala igual
            logger.exception("No se pudo leer el historial de %s para la nota", telefono)
            return []

        lineas = []
        for turno in historial:
            contenido = turno.get("content")
            # Solo el texto: los bloques de tool_use/tool_result son ruido para
            # una persona que solo quiere saber de qué venían hablando.
            if not isinstance(contenido, str) or not contenido.strip():
                continue
            quien = "Cliente" if turno.get("role") == "user" else "Bot"
            lineas.append(f"· {quien}: {contenido.strip()}")
        return lineas[-turnos:]

    async def _enviar_documento(
        self, tool_input: dict, sesion: SesionCliente, telefono: str
    ) -> dict:
        """Baja el documento del ERP y se lo manda al cliente por WhatsApp.

        Los bytes van del ERP al bot y del bot a la Media API de Meta. En ningún
        momento hay una URL desde la que se pueda bajar la factura de alguien —
        ni siquiera firmada y de cinco minutos.
        """
        tipo = (tool_input.get("tipo") or "").strip()
        folio = (tool_input.get("folio") or "").strip()
        if tipo not in TIPOS_DOCUMENTO:
            return {"enviado": False, "motivo": "tipo_desconocido", "tipo": tipo}
        if tipo != "estado_de_cuenta" and not folio:
            return {
                "enviado": False,
                "motivo": "falta_folio",
                "instruccion": "Pregúntale de qué folio quiere el documento.",
            }

        documento = await self._erp.get_customer_document(sesion.token, tipo, folio)
        if documento is None:
            # Mismo "no aparece" exista o no el documento de otro cliente.
            return {
                "enviado": False,
                "motivo": "no_esta_en_su_cuenta",
                "folio": folio,
                "instruccion": (
                    "Dile que ese documento no aparece en su cuenta y ofrécele "
                    "listarle los que sí tiene. NO sugieras que existe pero es "
                    "de alguien más."
                ),
            }

        media_id = await self._wa.upload_media(
            documento.contenido, documento.nombre, documento.tipo_mime
        )
        await self._wa.send_document(telefono, media_id, documento.nombre)
        return {"enviado": True, "documento": documento.nombre}

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name == "identificar_cliente":
                return json.dumps(
                    await self._identificar(tool_input, caller_phone), ensure_ascii=False
                )

            if name == "escalar_a_humano":
                motivo = tool_input.get("motivo", "(sin especificar)")
                return json.dumps(
                    await self._escalar(caller_phone, motivo), ensure_ascii=False
                )

            if name in TOOLS_CON_SESION:
                sesion = await self._sesiones.leer(caller_phone)
                if sesion is None:
                    return json.dumps(_SIN_SESION, ensure_ascii=False)

                if name == "cerrar_sesion":
                    await self._erp.close_customer_session(sesion.token)
                    await self._sesiones.cerrar(caller_phone)
                    return json.dumps(
                        {"sesion_cerrada": True, "cliente": sesion.cliente},
                        ensure_ascii=False,
                    )

                try:
                    datos = await self._datos_de_cuenta(
                        name, tool_input, sesion, caller_phone
                    )
                except SesionClienteInvalida:
                    # El ERP mandó el token a la basura antes de que caducara
                    # aquí. Se borra la copia local para no seguir mandando uno
                    # muerto en cada turno.
                    await self._sesiones.cerrar(caller_phone)
                    return json.dumps(
                        {
                            "identificado": False,
                            "sesion_expirada": True,
                            "instruccion": (
                                "Avisa que la sesión caducó por seguridad y pide "
                                "nombre o empresa y RFC otra vez."
                            ),
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(datos, ensure_ascii=False)

            return json.dumps({"error": f"herramienta desconocida: {name}"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error ejecutando herramienta %s", name)
            return json.dumps({"error": str(exc)})
