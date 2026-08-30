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

from ..erp import SesionClienteInvalida
from ..menus import BOTONES_SEGUIMIENTO, menu_cliente, texto_menu
from ..replies import Reply
from ..sesiones import SesionCliente, SesionClienteStore
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
que existe pero es de alguien más.
- Usa `escalar_a_humano` si el cliente está molesto, tiene un reclamo, pide algo \
fuera de tu alcance (cambiar precios, cancelar, renegociar) o pide una persona. \
Esa herramienta no requiere identificación.
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

# Los datos de la cuenta y el cierre de sesión exigen estar identificado.
TOOLS_CON_SESION = {
    "resumen_de_mi_cuenta",
    "consultar_mi_saldo",
    "listar_mis_contratos",
    "listar_mis_pedidos",
    "listar_mis_facturas",
    "consultar_orden",
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
            "Escala la conversación a un asesor humano cuando el cliente lo pide, "
            "tiene un reclamo, o la solicitud está fuera del alcance del agente. "
            "No requiere identificación."
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
        self, name: str, tool_input: dict, sesion: SesionCliente
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

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name == "identificar_cliente":
                return json.dumps(
                    await self._identificar(tool_input, caller_phone), ensure_ascii=False
                )

            if name == "escalar_a_humano":
                motivo = tool_input.get("motivo", "(sin especificar)")
                logger.info("Escalamiento solicitado (%s): %s", caller_phone, motivo)
                return json.dumps(
                    {
                        "escalado": True,
                        "mensaje": (
                            "Un asesor de Intergranel continuará la atención en breve."
                        ),
                    },
                    ensure_ascii=False,
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
                    datos = await self._datos_de_cuenta(name, tool_input, sesion)
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
