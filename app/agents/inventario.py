"""Agente de Inventario: stock, umbrales y alertas. **Uso interno.**

Las existencias provienen del ERP (vía `ERPClient`): con `ERP_BASE_URL`
configurado se consultan por HTTP; en desarrollo se usa el ERP simulado. Las
alertas proactivas (cuando un producto cae bajo su umbral) llegan por el webhook
`POST /webhooks/erp/inventory-alert` y se notifican al equipo.

**Este agente es del equipo, no del cliente**, y por eso lleva lista blanca de
teléfonos igual que Compras (`INVENTARIO_PHONES_ALLOWED`). Lo que contesta son
toneladas exactas, umbrales y ubicación de los silos: cuánto grano hay es
información de negociación —el que la sabe sabe cuánta prisa tenemos por
vender— y el router manda aquí a cualquiera que pregunte "¿cuánto maíz
tienen?". Un número de fuera se queda con Ventas, que solo habla de
disponibilidad: disponible, en tránsito o sobre pedido.
"""

from __future__ import annotations

import json
import logging

from ..config import get_settings
from .base import BaseAgent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Eres el agente de Inventario de Intergranel, comercializadora de granos a \
granel. Atiendes consultas INTERNAS del equipo sobre existencias.

Tu trabajo:
- Consultar el stock, umbral y ubicación de un producto con `consultar_stock`.
- Listar los productos por debajo de su umbral con `listar_alertas_inventario`.
- Dar un resumen de todo el inventario con `resumen_inventario`.
- Si el usuario quiere precios, cotizaciones o comprar, usa `transferir_a_ventas`.

Reglas:
- SIEMPRE usa las herramientas para cifras de stock. Nunca inventes cantidades.
- Indica con claridad cuándo un producto está por debajo de su umbral.
- Si una herramienta dice que no estás autorizado, NO des ninguna cifra de \
existencias, ni aproximada, ni de memoria, ni "en general". Dilo con amabilidad, \
ofrece pasarlo a Ventas con `transferir_a_ventas` y no insistas. Quien pregunta \
puede ser un cliente o un competidor.

Estilo: mensajes breves para WhatsApp, en español, claros y directos.
"""

_NO_AUTORIZADO = (
    "La consulta de existencias es de uso interno. Si busca disponibilidad de "
    "producto para comprar, con gusto le paso con Ventas."
)

# Todo lo que devuelve cifras de existencias. `transferir_a_ventas` NO va aquí:
# mandar a alguien con Ventas es justo lo que queremos que pase cuando no está
# autorizado, y pedirle permiso para eso lo dejaría atorado.
_RESTRICTED_TOOLS = {
    "consultar_stock",
    "listar_alertas_inventario",
    "resumen_inventario",
}

TOOLS = [
    {
        "name": "consultar_stock",
        "description": "Consulta el stock, umbral, ubicación y estado de un producto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {
                    "type": "string",
                    "description": "Nombre del producto, p. ej. 'trigo cristalino'.",
                }
            },
            "required": ["producto"],
        },
    },
    {
        "name": "listar_alertas_inventario",
        "description": "Lista los productos cuyo stock está por debajo de su umbral.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "resumen_inventario",
        "description": "Devuelve el estado de todos los productos en inventario.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "transferir_a_ventas",
        "description": (
            "Transfiere la conversación al agente de Ventas (precios, cotizaciones, "
            "pedidos)."
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


class InventarioAgent(BaseAgent):
    name = "inventario"

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def tools(self) -> list[dict]:
        return TOOLS

    def _is_authorized(self, phone: str) -> bool:
        allowed = get_settings().inventario_allowed_set
        # Lista vacía = sin restricción (desarrollo).
        return not allowed or phone in allowed

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name in _RESTRICTED_TOOLS and not self._is_authorized(caller_phone):
                logger.warning("Consulta de inventario no autorizada: %s", caller_phone)
                return json.dumps(
                    {"autorizado": False, "mensaje": _NO_AUTORIZADO}, ensure_ascii=False
                )

            if name == "consultar_stock":
                item = await self._erp.get_inventory_item(tool_input["producto"])
                if item is None:
                    return json.dumps(
                        {"encontrado": False, "producto": tool_input["producto"]},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"encontrado": True, **item.model_dump()}, ensure_ascii=False
                )

            if name == "listar_alertas_inventario":
                items = await self._erp.list_inventory()
                alertas = [i.model_dump() for i in items if i.estado == "bajo_umbral"]
                return json.dumps(
                    {"total": len(alertas), "alertas": alertas}, ensure_ascii=False
                )

            if name == "resumen_inventario":
                items = await self._erp.list_inventory()
                return json.dumps(
                    {"total": len(items), "productos": [i.model_dump() for i in items]},
                    ensure_ascii=False,
                )

            if name == "transferir_a_ventas":
                motivo = tool_input.get("motivo", "(sin especificar)")
                await self._bus.set_active_agent(caller_phone, "ventas")
                logger.info(
                    "Transferencia inventario->ventas (%s): %s", caller_phone, motivo
                )
                return json.dumps(
                    {
                        "transferido": True,
                        "agente": "ventas",
                        "mensaje": "Le paso con el equipo de ventas.",
                    },
                    ensure_ascii=False,
                )

            return json.dumps({"error": f"herramienta desconocida: {name}"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error ejecutando herramienta %s", name)
            return json.dumps({"error": str(exc)})
