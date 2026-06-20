"""Agente de Compras: gestión de órdenes de compra a proveedores.

Funcional contra el ERP (vía `ERPClient`): consulta y crea órdenes de compra,
las aprueba y lista proveedores. El acceso está restringido a una lista blanca
de teléfonos autorizados (`COMPRAS_PHONES_ALLOWED`); si la lista está vacía no
se aplica restricción (modo desarrollo).
"""

from __future__ import annotations

import json
import logging

from ..config import get_settings
from .base import BaseAgent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Eres el agente de Compras de Intergranel, comercializadora de granos a granel. \
Atiendes al equipo interno de abastecimiento.

Tu trabajo:
- Consultar una orden de compra por folio con `consultar_oc`.
- Listar las OC pendientes de aprobación con `listar_oc_pendientes`.
- Crear una nueva OC a un proveedor con `crear_oc` (proveedor, producto, toneladas).
- Aprobar una OC por folio con `aprobar_oc`.
- Listar proveedores con `listar_proveedores`.
- Si el usuario quiere precios de venta, cotizaciones o pedidos de clientes, \
usa `transferir_a_ventas`.

Reglas:
- SIEMPRE usa las herramientas para folios, montos y estados. Nunca inventes datos.
- Confirma proveedor, producto y cantidad antes de crear una OC.
- Antes de aprobar una OC, confirma el folio con el usuario.
- Si una herramienta indica que no estás autorizado, comunícalo con amabilidad \
y no insistas.

Estilo: mensajes breves para WhatsApp, en español, claros y directos.
"""

_NO_AUTORIZADO = (
    "No tiene autorización para usar el módulo de compras. Si cree que es un "
    "error, contacte al administrador."
)

TOOLS = [
    {
        "name": "consultar_oc",
        "description": "Consulta el estado y detalles de una orden de compra por su folio.",
        "input_schema": {
            "type": "object",
            "properties": {"folio": {"type": "string", "description": "Folio de la OC."}},
            "required": ["folio"],
        },
    },
    {
        "name": "listar_oc_pendientes",
        "description": "Lista las órdenes de compra pendientes de aprobación.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "crear_oc",
        "description": "Crea una nueva orden de compra a un proveedor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proveedor": {"type": "string", "description": "Nombre del proveedor."},
                "producto": {"type": "string", "description": "Producto a comprar."},
                "cantidad_ton": {"type": "number", "description": "Cantidad en toneladas."},
            },
            "required": ["proveedor", "producto", "cantidad_ton"],
        },
    },
    {
        "name": "aprobar_oc",
        "description": "Aprueba una orden de compra por su folio.",
        "input_schema": {
            "type": "object",
            "properties": {"folio": {"type": "string", "description": "Folio de la OC."}},
            "required": ["folio"],
        },
    },
    {
        "name": "listar_proveedores",
        "description": "Lista los proveedores registrados.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "transferir_a_ventas",
        "description": "Transfiere la conversación al agente de Ventas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "description": "Motivo de la transferencia."}
            },
            "required": ["motivo"],
        },
    },
]

# Herramientas que requieren autorización (acceso a datos de compras).
_RESTRICTED_TOOLS = {
    "consultar_oc",
    "listar_oc_pendientes",
    "crear_oc",
    "aprobar_oc",
    "listar_proveedores",
}


class ComprasAgent(BaseAgent):
    name = "compras"

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def tools(self) -> list[dict]:
        return TOOLS

    def _is_authorized(self, phone: str) -> bool:
        allowed = get_settings().compras_allowed_set
        # Lista vacía = sin restricción (desarrollo).
        return not allowed or phone in allowed

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name in _RESTRICTED_TOOLS and not self._is_authorized(caller_phone):
                logger.warning("Acceso a compras no autorizado: %s", caller_phone)
                return json.dumps(
                    {"autorizado": False, "mensaje": _NO_AUTORIZADO}, ensure_ascii=False
                )

            if name == "consultar_oc":
                oc = await self._erp.get_purchase_order(tool_input["folio"])
                if oc is None:
                    return json.dumps(
                        {"encontrada": False, "folio": tool_input["folio"]},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"encontrada": True, "oc": oc.model_dump()}, ensure_ascii=False
                )

            if name == "listar_oc_pendientes":
                ocs = await self._erp.list_pending_purchase_orders()
                return json.dumps(
                    {"total": len(ocs), "ocs": [o.model_dump() for o in ocs]},
                    ensure_ascii=False,
                )

            if name == "crear_oc":
                oc = await self._erp.create_purchase_order(
                    tool_input["proveedor"],
                    tool_input["producto"],
                    float(tool_input["cantidad_ton"]),
                )
                return json.dumps(
                    {"creada": True, "oc": oc.model_dump()}, ensure_ascii=False
                )

            if name == "aprobar_oc":
                oc = await self._erp.approve_purchase_order(tool_input["folio"])
                if oc is None:
                    return json.dumps(
                        {"encontrada": False, "folio": tool_input["folio"]},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"aprobada": True, "oc": oc.model_dump()}, ensure_ascii=False
                )

            if name == "listar_proveedores":
                proveedores = await self._erp.list_suppliers()
                return json.dumps(
                    {
                        "total": len(proveedores),
                        "proveedores": [p.model_dump() for p in proveedores],
                    },
                    ensure_ascii=False,
                )

            if name == "transferir_a_ventas":
                motivo = tool_input.get("motivo", "(sin especificar)")
                await self._bus.set_active_agent(caller_phone, "ventas")
                logger.info(
                    "Transferencia compras->ventas (%s): %s", caller_phone, motivo
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
