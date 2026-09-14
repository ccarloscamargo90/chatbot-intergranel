"""Agente de Ventas: precios, cotizaciones, contratos y pedidos.

**Los precios y las cotizaciones salen del CRM, no del ERP.** El ERP publica su
catálogo al CRM, el CRM lo espeja, y este agente le pregunta al CRM
(`CRMClient`). Un solo sentido: el bot no guarda credenciales del ERP y nunca
hay dos sistemas diciendo precios distintos.

Cuando el CRM no contesta, el agente NO cae al ERP: dice que ahora no puede
consultarlo y ofrece un asesor. El atajo el día que el CRM está caído es
exactamente el día en que la regla de dirección deja de ser verdad.

Los contratos y las solicitudes de pedido siguen siendo del ERP: son operación,
no precio.

Cotizar deja tres cosas, no una: el **PDF** en el teléfono del cliente, la
**cotización con ese mismo PDF** en el tablero del vendedor, y el **resumen de
lo que se habló** como nota del prospecto. El PDF se manda como efecto de la
herramienta (igual que los documentos en Soporte); el resumen se escribe en
segundo plano, porque lo que el cliente está esperando es su archivo.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from .. import resumen, tareas
from ..config import get_settings
from ..cotizacion_pdf import DatosCotizacion, construir, nombre_archivo, vigencia
from ..crm import CotizacionAunNoRegistrada, CRMNoDisponible, buscar_producto
from ..errores import detalle_http
from ..models import CotizacionCRM
from ..whatsapp import WhatsAppClient
from .base import BaseAgent

logger = logging.getLogger(__name__)

#: Cuántas veces se intenta dejar la nota, y cuánto se espera entre intentos.
#: El CRM contesta 409 mientras todavía no ve la cotización; es "todavía no",
#: no "no existe", así que se reintenta en vez de tirar el resumen.
INTENTOS_NOTA = 3
ESPERA_NOTA_SEGUNDOS = 5.0

SYSTEM_PROMPT = """\
Eres el agente de Ventas de Intergranel, comercializadora de granos a granel \
(maíz, sorgo, trigo, soya y derivados) para clientes industriales.

Tu trabajo es ayudar a los clientes a comprar:
- `listar_productos` para decir qué se maneja. NUNCA enumeres granos de memoria: \
lo que no está en esa lista, no se vende.
- `consultar_precio` para el precio por tonelada y la disponibilidad.
- `generar_cotizacion` (producto, toneladas y NOMBRE del cliente) para dejar una \
cotización formal registrada.
- `consultar_contrato` / `listar_contratos_cliente` para contratos ya existentes.
- `solicitar_pedido` para registrar una solicitud.
- `transferir_a_soporte` para reclamos, dudas de una orden existente o temas \
fuera de ventas.

## El precio: pide el nombre antes de cotizar

Cualquiera puede preguntar "¿a cómo está el maíz?" y se le contesta. Para una \
COTIZACIÓN —con toneladas y total— pregunta primero a nombre de quién va: \
"¿Me comparte su nombre o el de su empresa, por favor?". Con el nombre ya \
puedes llamar a `generar_cotizacion`. Es una cortesía para dejar la cotización \
a nombre de alguien, no una validación: no le pidas RFC ni le digas que lo \
verificas.

## Cuando no hay precio, el motivo importa

`consultar_precio` y `generar_cotizacion` devuelven `disponible: false` con un \
`motivo` distinto según lo que pasó. Tienes PROHIBIDO mezclarlos o inventar una \
causa que la herramienta no dio:

- `no_esta_en_catalogo` → No lo manejamos. Dilo así y ofrece lo que sí hay.
- `sin_precio_publicado` → SÍ lo manejamos, pero no tiene precio cargado. \
Nunca digas que no lo vendemos. Ofrece pasarlo con un asesor.
- `datos_no_confiables` → Los precios que tengo pueden estar desactualizados. \
NO des ninguna cifra. Discúlpate y ofrece un asesor.
- `crm_no_disponible` → No puedo consultarlo en este momento. NO des ninguna \
cifra ni prometas un precio. Ofrece un asesor.

Si una herramienta falla, di que no pudiste consultarlo. Jamás expliques la \
causa técnica ni te la inventes.

## La cotización se va en PDF

`generar_cotizacion` le manda al cliente el PDF por WhatsApp como parte de su \
trabajo. Tú no lo adjuntas ni le pasas ninguna liga.

- Si devuelve `pdf_enviado: true`, el archivo YA le llegó: dile que se lo \
acabas de enviar y confírmale el folio.
- Si devuelve `pdf_enviado: false`, NO le digas que se lo mandaste ni que "ya \
va en camino". Haz lo que diga la `instruccion` que viene en la respuesta.

## Reglas que no se rompen

- SIEMPRE usa las herramientas para precios, cantidades y montos. Nunca \
inventes cifras, ni siquiera aproximadas, ni las recuerdes de antes en la \
conversación: vuelve a consultar.
- Confirma producto y toneladas antes de cotizar.
- Di siempre de cuándo es el precio y que está sujeto a confirmación.
- **NUNCA digas cuánto producto hay.** No des toneladas en existencia, ni \
totales de inventario, ni "nos quedan", ni "tenemos suficiente para surtir X". \
Cuánto grano hay en los silos es información interna. Lo único que puedes \
decir de disponibilidad es lo que trae la herramienta: disponible, en tránsito \
o sobre pedido. Si el cliente insiste en saber cuánto hay, dile que eso lo \
confirma un asesor al cerrar el pedido.
- Si `generar_cotizacion` devuelve `modo_cotizacion: "manual"`, NO le des el \
total al cliente: dile que un asesor le hace llegar la cotización formal.

Estilo: mensajes breves para WhatsApp, en español, trato de "usted" salvo que \
el cliente tutee. Emojis con moderación.
"""

TOOLS = [
    {
        "name": "listar_productos",
        "description": (
            "Lista los productos que se venden, con su precio por unidad cuando "
            "lo tienen. Úsala cuando el cliente pregunte qué se maneja."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "consultar_precio",
        "description": (
            "Consulta el precio por tonelada y la disponibilidad de un producto. "
            "El precio viene del CRM, que lo espeja del ERP."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {
                    "type": "string",
                    "description": "Nombre del producto, p. ej. 'maíz amarillo', 'trigo'.",
                }
            },
            "required": ["producto"],
        },
    },
    {
        "name": "generar_cotizacion",
        "description": (
            "Registra una cotización formal en el CRM para una cantidad de "
            "toneladas de un producto, a nombre del cliente, y le manda el PDF "
            "por WhatsApp. Pregunta el nombre antes de usarla."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {"type": "string", "description": "Producto a cotizar."},
                "cantidad_ton": {
                    "type": "number",
                    "description": "Cantidad en toneladas.",
                },
                "nombre_cliente": {
                    "type": "string",
                    "description": (
                        "Nombre de la persona o de su empresa, tal como lo dijo. "
                        "A nombre de quién queda la cotización."
                    ),
                },
            },
            "required": ["producto", "cantidad_ton", "nombre_cliente"],
        },
    },
    {
        "name": "consultar_contrato",
        "description": "Consulta el estado y detalles de un contrato por su folio.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio": {
                    "type": "string",
                    "description": "Folio del contrato, p. ej. CONT-2026-0001.",
                }
            },
            "required": ["folio"],
        },
    },
    {
        "name": "listar_contratos_cliente",
        "description": (
            "Lista los contratos asociados al teléfono del cliente que escribe."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "solicitar_pedido",
        "description": (
            "Registra una solicitud de pedido (producto y toneladas) para que el "
            "equipo la procese."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {"type": "string", "description": "Producto solicitado."},
                "cantidad_ton": {
                    "type": "number",
                    "description": "Cantidad en toneladas.",
                },
            },
            "required": ["producto", "cantidad_ton"],
        },
    },
    {
        "name": "transferir_a_soporte",
        "description": (
            "Transfiere la conversación al agente de Soporte (dudas sobre órdenes "
            "existentes, reclamos o temas fuera de ventas)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "description": "Motivo de la transferencia."}
            },
            "required": ["motivo"],
        },
    },
]


def folio_cotizacion(telefono: str, ahora: datetime | None = None) -> str:
    """Folio del bot para una cotización: `COT-20260908-064512-5678`.

    Lleva SEGUNDOS y las últimas cifras del teléfono porque el CRM deduplica
    por folio: dos cotizaciones del mismo cliente en el mismo minuto tienen que
    ser dos, no una pisando a la otra.
    """
    marca = (ahora or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"COT-{marca}-{telefono[-4:]}"


class VentasAgent(BaseAgent):
    name = "ventas"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # El PDF no viaja en la respuesta del agente: se manda aquí, como
        # efecto de la herramienta, igual que los documentos en Soporte. Un
        # archivo no es texto al que colgarle botones.
        self._wa = WhatsAppClient()

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def tools(self) -> list[dict]:
        return TOOLS

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name == "listar_productos":
                try:
                    catalogo = await self._crm.catalogo()
                except CRMNoDisponible as exc:
                    logger.warning("CRM no disponible al listar productos: %s", exc)
                    return self._sin_precio("crm_no_disponible")
                if catalogo.desactualizado:
                    return self._sin_precio(
                        "datos_no_confiables", ultima_actualizacion=catalogo.ultima_sync
                    )
                return json.dumps(
                    {
                        "disponible": True,
                        "actualizado_el": catalogo.ultima_sync,
                        "productos": [
                            {
                                "producto": p.nombre,
                                "unidad": p.unidad,
                                "precio_ton": p.precio_unitario,
                                "moneda": p.moneda,
                                "disponibilidad": p.disponibilidad,
                            }
                            for p in catalogo.productos
                        ],
                    },
                    ensure_ascii=False,
                )

            if name == "consultar_precio":
                consulta = tool_input["producto"]
                encontrado = await self._precio_de(consulta)
                if isinstance(encontrado, str):
                    return encontrado
                producto, catalogo = encontrado
                return json.dumps(
                    {
                        "disponible": True,
                        "producto": producto.nombre,
                        "precio_ton": producto.precio_unitario,
                        "moneda": producto.moneda,
                        "unidad": producto.unidad,
                        "disponibilidad": producto.disponibilidad,
                        # La existencia en toneladas NO viaja al modelo. El
                        # prompt se lo prohíbe, pero un prompt se puede rodear
                        # y un dato que no está no se puede decir: cuánto grano
                        # hay en los silos no sale por WhatsApp.
                        "actualizado_el": catalogo.ultima_sync,
                    },
                    ensure_ascii=False,
                )

            if name == "generar_cotizacion":
                return await self._cotizar(tool_input, caller_phone)

            if name == "consultar_contrato":
                order = await self._erp.get_order(tool_input["folio"])
                if order is None:
                    return json.dumps(
                        {"encontrado": False, "folio": tool_input["folio"]},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"encontrado": True, "contrato": order.model_dump()},
                    ensure_ascii=False,
                )

            if name == "listar_contratos_cliente":
                orders = await self._erp.list_orders_by_phone(caller_phone)
                return json.dumps(
                    {
                        "telefono": caller_phone,
                        "total": len(orders),
                        "contratos": [o.model_dump() for o in orders],
                    },
                    ensure_ascii=False,
                )

            if name == "solicitar_pedido":
                solicitud = await self._erp.create_request(
                    tool_input["producto"], float(tool_input["cantidad_ton"]), caller_phone
                )
                data = solicitud.model_dump()
                await self._bus.publish(
                    f"bus:ventas:solicitud:{caller_phone}", data, ttl=86400
                )
                return json.dumps(data, ensure_ascii=False)

            if name == "transferir_a_soporte":
                motivo = tool_input.get("motivo", "(sin especificar)")
                await self._bus.set_active_agent(caller_phone, "soporte")
                logger.info("Transferencia ventas->soporte (%s): %s", caller_phone, motivo)
                return json.dumps(
                    {
                        "transferido": True,
                        "agente": "soporte",
                        "mensaje": "Le paso con el equipo de soporte para ayudarle con eso.",
                    },
                    ensure_ascii=False,
                )

            return json.dumps({"error": f"herramienta desconocida: {name}"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error ejecutando herramienta %s", name)
            return json.dumps({"error": str(exc)})

    # --- Cotizar: el PDF al cliente, la nota al vendedor -------------------- #

    async def _cotizar(self, tool_input: dict, telefono: str) -> str:
        """Registra la cotización, le manda el PDF y deja el resumen al vendedor.

        El orden importa: el PDF se arma ANTES de hablarle al CRM para que el
        archivo que se guarda en el tablero sea, byte por byte, el mismo que
        recibió el cliente. Si se generara después, un vendedor podría estar
        viendo una versión y el cliente otra.
        """
        consulta = tool_input["producto"]
        cantidad = float(tool_input["cantidad_ton"])
        nombre = str(tool_input["nombre_cliente"]).strip()
        if not nombre:
            return self._sin_precio("falta_nombre_cliente")

        encontrado = await self._precio_de(consulta)
        if isinstance(encontrado, str):
            return encontrado
        producto, catalogo = encontrado

        # `precio_unitario` no puede ser None aquí: `_precio_de` ya lo
        # descartó con `sin_precio_publicado`. La aserción es para que un
        # cambio futuro rompa aquí y no en el total de un cliente.
        assert producto.precio_unitario is not None

        settings = get_settings()
        folio = folio_cotizacion(telefono)
        hoy = datetime.now(UTC).date()
        vence = vigencia(settings.cotizacion_vigencia_dias, hoy)
        pdf, archivo = self._armar_pdf(
            DatosCotizacion(
                folio=folio,
                cliente=nombre,
                producto=producto.nombre,
                cantidad_ton=cantidad,
                precio_ton=producto.precio_unitario,
                empresa=settings.company_name,
                moneda=producto.moneda,
                telefono=telefono,
                precio_actualizado_el=catalogo.ultima_sync,
                emitida=hoy,
                vigencia_hasta=vence,
                tasa_iva=settings.cotizacion_iva_tasa,
            )
        )

        try:
            cotizacion = await self._crm.registrar_cotizacion(
                folio=folio,
                nombre_cliente=nombre,
                telefono=telefono,
                producto=producto.nombre,
                cantidad_ton=cantidad,
                precio_ton=producto.precio_unitario,
                moneda=producto.moneda,
                pdf=pdf,
                pdf_nombre=archivo,
                vigencia_hasta=vence,
                tasa_iva=settings.cotizacion_iva_tasa,
            )
        except CRMNoDisponible as exc:
            logger.warning("CRM no disponible al cotizar: %s", exc)
            return self._sin_precio("crm_no_disponible")

        envio = await self._mandar_pdf(telefono, cotizacion, pdf, archivo)
        data = cotizacion.model_dump()
        await self._bus.publish(
            f"bus:ventas:cotizacion:{telefono}", {**data, **envio}, ttl=86400
        )
        # El resumen va en segundo plano: lo que el cliente está esperando es
        # su PDF, no que se acabe de escribir una nota interna.
        tareas.lanzar(
            self._nota_para_el_vendedor(telefono, cotizacion, envio),
            nombre=f"resumen-cotizacion:{folio}",
        )
        return json.dumps({"disponible": True, **data, **envio}, ensure_ascii=False)

    @staticmethod
    def _armar_pdf(datos: DatosCotizacion) -> tuple[bytes | None, str | None]:
        """Los bytes del PDF, o (None, None) si no se pudo armar.

        Un fallo aquí no cancela la cotización: queda registrada y el vendedor
        la ve en su tablero. Perder el archivo es un mal menor; perder la
        cotización —y con ella al cliente que la pidió— no lo es.
        """
        try:
            return construir(datos), nombre_archivo(datos.folio)
        except Exception:  # noqa: BLE001 - se sigue sin PDF, no se cae la venta
            logger.exception("No se pudo armar el PDF de %s", datos.folio)
            return None, None

    async def _mandar_pdf(
        self,
        telefono: str,
        cotizacion: CotizacionCRM,
        pdf: bytes | None,
        archivo: str | None,
    ) -> dict:
        """Le manda el PDF al cliente, salvo que deba verlo antes un vendedor.

        Devuelve un motivo distinto por causa, como `enviar_mi_documento`: con
        un solo "no se pudo" para todo, el modelo rellena el hueco y le
        explica al cliente una causa que nadie le dio.
        """
        if cotizacion.modo_cotizacion != "automatic":
            # El CRM manda. En modo manual una persona revisa el precio antes
            # de que el cliente lo vea, y mandarle el PDF sería exactamente lo
            # que ese modo existe para evitar.
            return {
                "pdf_enviado": False,
                "motivo_pdf": "requiere_revision_de_vendedor",
                "instruccion": (
                    "NO le des el total ni le digas que ya le mandaste algo. "
                    "Dile que un asesor revisa su cotización y se la hace "
                    "llegar formalmente."
                ),
            }
        if pdf is None or archivo is None:
            return {
                "pdf_enviado": False,
                "motivo_pdf": "no_se_pudo_generar",
                "instruccion": (
                    "Su cotización SÍ quedó registrada con el folio que traes, "
                    "pero no pudiste generar el archivo. Dale el folio, dile "
                    "que un asesor le hace llegar el documento y NO digas que "
                    "ya se lo enviaste."
                ),
            }
        try:
            media_id = await self._wa.upload_media(pdf, archivo, "application/pdf")
            await self._wa.send_document(telefono, media_id, archivo)
        except Exception as exc:  # noqa: BLE001 - el motivo real, no "hubo un error"
            motivo = detalle_http(exc, "Meta")
            logger.error("No se pudo enviar %s a %s: %s", archivo, telefono, motivo)
            return {
                "pdf_enviado": False,
                "motivo_pdf": "fallo_al_enviar",
                "detalle": motivo,
                "instruccion": (
                    "La cotización quedó registrada pero el envío del archivo "
                    "falló. Dale el folio y ofrécele pasarlo con un asesor. NO "
                    "digas que ya le llegó el documento."
                ),
            }
        return {"pdf_enviado": True, "documento": archivo}

    async def _nota_para_el_vendedor(
        self, telefono: str, cotizacion: CotizacionCRM, envio: dict
    ) -> None:
        """Deja en el CRM el resumen de lo que se habló, junto a la cotización.

        El resumen se arma con el historial GUARDADO, que no incluye todavía
        el mensaje que el cliente acaba de mandar: el turno en curso se
        persiste al final, después de esta herramienta. Por eso los hechos
        —qué pidió, cuánto, si ya recibió el PDF— viajan aparte y salen de la
        cotización, no de la plática: así la nota dice lo esencial aunque la
        transcripción venga corta.
        """
        historial = await self._history_store.load(self._history_key(telefono))
        texto = await resumen.redactar(
            hechos=self._hechos(telefono, cotizacion, envio), historial=historial
        )

        for intento in range(1, INTENTOS_NOTA + 1):
            try:
                await self._crm.registrar_nota_cotizacion(
                    folio=cotizacion.folio, resumen=texto
                )
                return
            except CotizacionAunNoRegistrada as exc:
                logger.info(
                    "El CRM aún no ve %s (intento %s/%s): %s",
                    cotizacion.folio,
                    intento,
                    INTENTOS_NOTA,
                    exc,
                )
                if intento < INTENTOS_NOTA:
                    await asyncio.sleep(ESPERA_NOTA_SEGUNDOS)
            except CRMNoDisponible as exc:
                # La cotización y su PDF ya están en el CRM; lo que se pierde
                # es el contexto. No se insiste: insistir contra un CRM caído
                # no lo levanta.
                logger.warning("No se pudo dejar la nota de %s: %s", cotizacion.folio, exc)
                return
        logger.warning("Se agotaron los intentos de dejar la nota de %s", cotizacion.folio)

    @staticmethod
    def _hechos(telefono: str, cotizacion: CotizacionCRM, envio: dict) -> list[str]:
        """Lo que el vendedor tiene que leer aunque el resumen salga vacío."""
        hechos = [
            f"- Pidió {cotizacion.producto}, {cotizacion.cantidad_ton:,.3f} t. "
            f"Escribió por WhatsApp desde el {telefono}."
        ]
        if envio.get("pdf_enviado"):
            hechos.append("- Ya recibió el PDF de la cotización por WhatsApp.")
        elif envio.get("motivo_pdf") == "requiere_revision_de_vendedor":
            hechos.append(
                "- NO se le envió el PDF: la cotización está en modo de revisión, "
                "hay que aprobarla y enviársela."
            )
        else:
            hechos.append(
                "- NO se le pudo enviar el PDF; se le dijo que un asesor se lo "
                "hace llegar."
            )
        return hechos

    # --- helpers ----------------------------------------------------------- #

    @staticmethod
    def _sin_precio(motivo: str, **extra: object) -> str:
        """El "no puedo darte un precio", con la causa REAL y ninguna otra.

        Un motivo por causa, y el prompt tiene prohibido mezclarlos. Con un solo
        motivo para todo, el modelo rellena el hueco: es cómo el bot terminó
        diciéndole a un cliente que no manejamos algo que sí manejamos.
        """
        return json.dumps({"disponible": False, "motivo": motivo, **extra}, ensure_ascii=False)

    async def _precio_de(self, consulta: str):
        """El producto con precio, o el JSON del motivo por el que no lo hay.

        Devolver dos cosas distintas es feo, pero la alternativa —repetir esta
        escalera en cada tool— es peor: el precio y la cotización tienen que
        rechazar por los MISMOS motivos, o el bot cotizaría lo que dijo que no
        podía cotizar.
        """
        try:
            catalogo = await self._crm.catalogo()
        except CRMNoDisponible as exc:
            logger.warning("CRM no disponible al consultar precio: %s", exc)
            return self._sin_precio("crm_no_disponible")

        if catalogo.desactualizado:
            # El CRM copió este precio del ERP y avisa que su copia no es de
            # fiar. Decir la cifra igual sería prometerle a un cliente un precio
            # que quizá ya no existe.
            return self._sin_precio(
                "datos_no_confiables", ultima_actualizacion=catalogo.ultima_sync
            )

        producto = buscar_producto(catalogo, consulta)
        if producto is None:
            return self._sin_precio("no_esta_en_catalogo", producto=consulta)
        if producto.precio_unitario is None:
            # Sí se vende; nadie le ha puesto precio. NO es lo mismo que no
            # manejarlo, y contestarlo igual le miente al cliente.
            return self._sin_precio("sin_precio_publicado", producto=producto.nombre)
        return producto, catalogo
