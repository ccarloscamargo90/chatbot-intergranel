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
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ..crm import CRMNoDisponible, buscar_producto
from .base import BaseAgent

logger = logging.getLogger(__name__)

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

## Reglas que no se rompen

- SIEMPRE usa las herramientas para precios, cantidades y montos. Nunca \
inventes cifras, ni siquiera aproximadas, ni las recuerdes de antes en la \
conversación: vuelve a consultar.
- Confirma producto y toneladas antes de cotizar.
- Di siempre de cuándo es el precio y que está sujeto a confirmación.
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
            "toneladas de un producto, a nombre del cliente. Pregunta el nombre "
            "antes de usarla."
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
                        "existencia_ton": producto.existencia,
                        "actualizado_el": catalogo.ultima_sync,
                    },
                    ensure_ascii=False,
                )

            if name == "generar_cotizacion":
                consulta = tool_input["producto"]
                cantidad = float(tool_input["cantidad_ton"])
                nombre = str(tool_input["nombre_cliente"]).strip()
                if not nombre:
                    return json.dumps(
                        {"disponible": False, "motivo": "falta_nombre_cliente"},
                        ensure_ascii=False,
                    )

                encontrado = await self._precio_de(consulta)
                if isinstance(encontrado, str):
                    return encontrado
                producto, _ = encontrado

                # `precio_unitario` no puede ser None aquí: `_precio_de` ya lo
                # descartó con `sin_precio_publicado`. La aserción es para que
                # un cambio futuro rompa aquí y no en el total de un cliente.
                assert producto.precio_unitario is not None
                try:
                    cotizacion = await self._crm.registrar_cotizacion(
                        folio=folio_cotizacion(caller_phone),
                        nombre_cliente=nombre,
                        telefono=caller_phone,
                        producto=producto.nombre,
                        cantidad_ton=cantidad,
                        precio_ton=producto.precio_unitario,
                        moneda=producto.moneda,
                    )
                except CRMNoDisponible as exc:
                    logger.warning("CRM no disponible al cotizar: %s", exc)
                    return self._sin_precio("crm_no_disponible")

                data = cotizacion.model_dump()
                await self._bus.publish(
                    f"bus:ventas:cotizacion:{caller_phone}", data, ttl=86400
                )
                return json.dumps({"disponible": True, **data}, ensure_ascii=False)

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
