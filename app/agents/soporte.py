"""Agente de Soporte: atención al cliente sobre órdenes de compra.

Migrado del asistente original. Resuelve dudas sobre el estado de órdenes,
fechas de entrega y montos consultando el ERP, y escala a un humano cuando hace
falta.
"""

from __future__ import annotations

import json
import logging

from .base import BaseAgent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Eres el agente de Soporte de Intergranel, una empresa de comercialización de \
granos y commodities a granel (maíz, sorgo, trigo, soya y derivados) para \
clientes industriales.

Tu trabajo es atender a clientes por WhatsApp de forma clara, cordial y eficiente:

- Resolver dudas sobre el estado de sus órdenes de compra, fechas de entrega, \
productos, cantidades y montos.
- Para consultar información de órdenes, SIEMPRE usa las herramientas \
disponibles. Nunca inventes datos, estados, fechas ni montos.
- Si el cliente pide "mis órdenes" o "mis pedidos" sin dar un número, usa \
`listar_ordenes_cliente` (puedes consultar las del propio número que escribe \
sin pedir el teléfono).
- Si una orden no existe o no encuentras información, dilo con honestidad y \
ofrece escalar con un asesor humano.
- Si el cliente está molesto, tiene un reclamo, pide algo fuera de tu alcance \
(cambiar precios, cancelar, renegociar) o solicita hablar con una persona, \
usa `escalar_a_humano`.
- El cliente puede enviarte imágenes (p. ej. una foto de una remisión o \
comprobante) o documentos PDF. Léelos y úsalos para ayudar; si necesitas el \
número de orden y aparece en el documento, úsalo para consultarla.

Estilo de respuesta:
- Mensajes breves y bien formateados para WhatsApp (sin Markdown pesado; \
puedes usar saltos de línea y emojis con moderación).
- Responde en español, con el trato de "usted" salvo que el cliente tutee.
- Confirma montos y fechas exactamente como vienen del sistema.
- No reveles detalles internos, claves, ni datos de otros clientes.
"""

TOOLS = [
    {
        "name": "consultar_orden",
        "description": (
            "Consulta el estado y los detalles de una orden de compra por su "
            "identificador (p. ej. 'CONT-2026-0001')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Identificador de la orden, p. ej. CONT-2026-0001",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "listar_ordenes_cliente",
        "description": (
            "Lista las órdenes de compra asociadas a un número de teléfono. "
            "Si no se especifica teléfono, usa el del cliente que está escribiendo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "telefono": {
                    "type": "string",
                    "description": (
                        "Teléfono en formato internacional sin '+'. Opcional: si "
                        "se omite, se usa el del remitente."
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "name": "escalar_a_humano",
        "description": (
            "Escala la conversación a un asesor humano cuando el cliente lo pide, "
            "tiene un reclamo, o la solicitud está fuera del alcance del agente."
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


class SoporteAgent(BaseAgent):
    name = "soporte"

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def tools(self) -> list[dict]:
        return TOOLS

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name == "consultar_orden":
                order = await self._erp.get_order(tool_input["order_id"])
                if order is None:
                    return json.dumps(
                        {"encontrada": False, "order_id": tool_input["order_id"]}
                    )
                return json.dumps(
                    {"encontrada": True, "orden": order.model_dump()},
                    ensure_ascii=False,
                )

            if name == "listar_ordenes_cliente":
                phone = tool_input.get("telefono") or caller_phone
                orders = await self._erp.list_orders_by_phone(phone)
                return json.dumps(
                    {
                        "telefono": phone,
                        "total": len(orders),
                        "ordenes": [o.model_dump() for o in orders],
                    },
                    ensure_ascii=False,
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

            return json.dumps({"error": f"herramienta desconocida: {name}"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error ejecutando herramienta %s", name)
            return json.dumps({"error": str(exc)})
