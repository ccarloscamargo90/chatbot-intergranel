"""Agente de Inventario: stock, umbrales y alertas.

Funcional con datos simulados (INVENTARIO_MOCK). En la Fase 4 se conectará al
ERP real y se añadirán alertas proactivas.
"""

from __future__ import annotations

import json
import logging
import unicodedata

from .base import BaseAgent

logger = logging.getLogger(__name__)

# Stock simulado por producto (toneladas) con su umbral mínimo y ubicación.
INVENTARIO_MOCK = {
    "trigo cristalino": {"stock_ton": 200.0, "umbral_ton": 250.0, "ubicacion": "Silo Querétaro"},
    "soya": {"stock_ton": 150.0, "umbral_ton": 200.0, "ubicacion": "Silo Veracruz"},
    "maiz amarillo": {"stock_ton": 850.0, "umbral_ton": 300.0, "ubicacion": "Silo Bajío"},
    "maiz blanco": {"stock_ton": 520.0, "umbral_ton": 250.0, "ubicacion": "Silo Bajío"},
    "sorgo": {"stock_ton": 640.0, "umbral_ton": 200.0, "ubicacion": "Silo Sinaloa"},
}

SYSTEM_PROMPT = """\
Eres el agente de Inventario de Intergranel, comercializadora de granos a \
granel. Atiendes consultas internas sobre existencias.

Tu trabajo:
- Consultar el stock, umbral y ubicación de un producto con `consultar_stock`.
- Listar los productos por debajo de su umbral con `listar_alertas_inventario`.
- Dar un resumen de todo el inventario con `resumen_inventario`.
- Si el usuario quiere precios, cotizaciones o comprar, usa `transferir_a_ventas`.

Reglas:
- SIEMPRE usa las herramientas para cifras de stock. Nunca inventes cantidades.
- Indica con claridad cuándo un producto está por debajo de su umbral.

Estilo: mensajes breves para WhatsApp, en español, claros y directos.
"""

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


def _normalize(text: str) -> str:
    text = text.strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _estado(data: dict) -> str:
    return "bajo_umbral" if data["stock_ton"] < data["umbral_ton"] else "normal"


def _producto_dict(nombre: str, data: dict) -> dict:
    return {
        "producto": nombre,
        "stock_ton": data["stock_ton"],
        "umbral_ton": data["umbral_ton"],
        "ubicacion": data["ubicacion"],
        "estado": _estado(data),
    }


class InventarioAgent(BaseAgent):
    name = "inventario"

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def tools(self) -> list[dict]:
        return TOOLS

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name == "consultar_stock":
                key = _normalize(tool_input["producto"])
                data = INVENTARIO_MOCK.get(key)
                if data is None:
                    for nombre, d in INVENTARIO_MOCK.items():
                        if key in nombre or nombre in key:
                            key, data = nombre, d
                            break
                if data is None:
                    return json.dumps(
                        {"encontrado": False, "producto": tool_input["producto"]},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"encontrado": True, **_producto_dict(key, data)},
                    ensure_ascii=False,
                )

            if name == "listar_alertas_inventario":
                alertas = [
                    _producto_dict(n, d)
                    for n, d in INVENTARIO_MOCK.items()
                    if _estado(d) == "bajo_umbral"
                ]
                return json.dumps(
                    {"total": len(alertas), "alertas": alertas}, ensure_ascii=False
                )

            if name == "resumen_inventario":
                productos = [_producto_dict(n, d) for n, d in INVENTARIO_MOCK.items()]
                return json.dumps(
                    {"total": len(productos), "productos": productos}, ensure_ascii=False
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
