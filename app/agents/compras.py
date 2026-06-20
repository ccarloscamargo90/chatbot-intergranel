"""Agente de Compras: gestión de órdenes de compra a proveedores.

Stub de la Fase 1. Las herramientas de compras devuelven un mensaje de "próximamente";
se implementarán en la Fase 3 (consultar_oc, listar_oc_pendientes, crear_oc,
aprobar_oc, listar_proveedores) junto con una lista blanca de teléfonos
autorizados. La transferencia a Ventas sí es funcional.
"""

from __future__ import annotations

import json
import logging

from .base import BaseAgent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Eres el agente de Compras de Intergranel, comercializadora de granos a granel. \
Atiendes al equipo interno de abastecimiento.

Por ahora el módulo de compras está en construcción: si te piden consultar o \
crear órdenes de compra, aprobar OCs o listar proveedores, usa la herramienta \
correspondiente y comunica con amabilidad que la función estará disponible \
próximamente.

Si el usuario en realidad quiere precios de venta, cotizaciones o pedidos de \
clientes, usa `transferir_a_ventas`.

Estilo: mensajes breves para WhatsApp, en español, claros y directos.
"""

_PROXIMAMENTE = (
    "El módulo de compras estará disponible próximamente. Por ahora no puedo "
    "ejecutar esta acción."
)

TOOLS = [
    {
        "name": "consultar_oc",
        "description": "Consulta una orden de compra por su folio. (Próximamente)",
        "input_schema": {
            "type": "object",
            "properties": {"folio": {"type": "string", "description": "Folio de la OC."}},
            "required": ["folio"],
        },
    },
    {
        "name": "listar_oc_pendientes",
        "description": "Lista las órdenes de compra pendientes de aprobación. (Próximamente)",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "crear_oc",
        "description": "Crea una nueva orden de compra a un proveedor. (Próximamente)",
        "input_schema": {
            "type": "object",
            "properties": {
                "proveedor": {"type": "string"},
                "producto": {"type": "string"},
                "cantidad_ton": {"type": "number"},
            },
            "required": ["proveedor", "producto", "cantidad_ton"],
        },
    },
    {
        "name": "aprobar_oc",
        "description": "Aprueba una orden de compra por su folio. (Próximamente)",
        "input_schema": {
            "type": "object",
            "properties": {"folio": {"type": "string", "description": "Folio de la OC."}},
            "required": ["folio"],
        },
    },
    {
        "name": "listar_proveedores",
        "description": "Lista los proveedores registrados. (Próximamente)",
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

# Herramientas aún no implementadas (devuelven "próximamente").
_STUB_TOOLS = {
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

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name in _STUB_TOOLS:
                return json.dumps(
                    {"disponible": False, "mensaje": _PROXIMAMENTE}, ensure_ascii=False
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
