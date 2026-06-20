"""Agente de Ventas: precios, cotizaciones, contratos y pedidos.

Funcional con precios simulados (PRECIOS_MOCK). En la Fase 2 estos se
reemplazarán por llamadas HTTP al ERP. Las cotizaciones y solicitudes se
publican en el bus de eventos para que otros agentes puedan consultarlas.
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata

from .base import BaseAgent

logger = logging.getLogger(__name__)

# Precios simulados por tonelada (MXN). Las claves se comparan sin acentos.
PRECIOS_MOCK = {
    "maiz amarillo": {"precio_ton": 5200.0, "disponible_ton": 1200.0},
    "maiz blanco": {"precio_ton": 5450.0, "disponible_ton": 800.0},
    "trigo": {"precio_ton": 7100.0, "disponible_ton": 600.0},
    "sorgo": {"precio_ton": 4800.0, "disponible_ton": 900.0},
    "soya": {"precio_ton": 11500.0, "disponible_ton": 400.0},
}
MONEDA = "MXN"
VIGENCIA = "fin del día hábil"

SYSTEM_PROMPT = """\
Eres el agente de Ventas de Intergranel, comercializadora de granos a granel \
(maíz, sorgo, trigo, soya y derivados) para clientes industriales.

Tu trabajo es ayudar a los clientes a comprar:
- Consultar precios y disponibilidad por producto con `consultar_precio`.
- Generar cotizaciones formales con `generar_cotizacion` (producto y toneladas).
- Consultar el estado de un contrato con `consultar_contrato` o listar los del \
cliente con `listar_contratos_cliente`.
- Registrar una solicitud de pedido con `solicitar_pedido`.
- Si el cliente tiene una duda sobre una orden ya existente, un reclamo o algo \
fuera de ventas, usa `transferir_a_soporte`.

Reglas:
- SIEMPRE usa las herramientas para precios, cantidades y montos. Nunca \
inventes cifras.
- Confirma con el cliente el producto y las toneladas antes de cotizar.
- Sé claro sobre la vigencia de los precios.

Estilo: mensajes breves para WhatsApp, en español, trato de "usted" salvo que \
el cliente tutee. Emojis con moderación.
"""

TOOLS = [
    {
        "name": "consultar_precio",
        "description": "Consulta el precio por tonelada y la disponibilidad de un producto.",
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
            "Genera una cotización con el total para una cantidad de toneladas de "
            "un producto. La registra en el sistema."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {"type": "string", "description": "Producto a cotizar."},
                "cantidad_ton": {
                    "type": "number",
                    "description": "Cantidad en toneladas.",
                },
            },
            "required": ["producto", "cantidad_ton"],
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


def _normalize(text: str) -> str:
    """Minúsculas sin acentos, para emparejar nombres de producto."""
    text = text.strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _lookup_precio(producto: str) -> tuple[str, dict] | None:
    key = _normalize(producto)
    if key in PRECIOS_MOCK:
        return key, PRECIOS_MOCK[key]
    # Coincidencia parcial (p. ej. "maíz" -> "maiz amarillo").
    for name, data in PRECIOS_MOCK.items():
        if key in name or name in key:
            return name, data
    return None


class VentasAgent(BaseAgent):
    name = "ventas"

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def tools(self) -> list[dict]:
        return TOOLS

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name == "consultar_precio":
                match = _lookup_precio(tool_input["producto"])
                if match is None:
                    return json.dumps(
                        {"encontrado": False, "producto": tool_input["producto"]},
                        ensure_ascii=False,
                    )
                prod, data = match
                return json.dumps(
                    {
                        "encontrado": True,
                        "producto": prod,
                        "precio_ton": data["precio_ton"],
                        "moneda": MONEDA,
                        "disponible_ton": data["disponible_ton"],
                        "vigencia": VIGENCIA,
                    },
                    ensure_ascii=False,
                )

            if name == "generar_cotizacion":
                match = _lookup_precio(tool_input["producto"])
                if match is None:
                    return json.dumps(
                        {"encontrado": False, "producto": tool_input["producto"]},
                        ensure_ascii=False,
                    )
                prod, data = match
                cantidad = float(tool_input["cantidad_ton"])
                total = round(data["precio_ton"] * cantidad, 2)
                cotizacion = {
                    "id": f"COT-{int(time.time())}",
                    "producto": prod,
                    "cantidad_ton": cantidad,
                    "precio_ton": data["precio_ton"],
                    "total": total,
                    "moneda": MONEDA,
                    "vigencia": VIGENCIA,
                    "estado": "borrador",
                }
                await self._bus.publish(
                    f"bus:ventas:cotizacion:{caller_phone}", cotizacion, ttl=86400
                )
                return json.dumps(cotizacion, ensure_ascii=False)

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
                solicitud = {
                    "id": f"SOL-{int(time.time())}",
                    "producto": tool_input["producto"],
                    "cantidad_ton": float(tool_input["cantidad_ton"]),
                    "telefono": caller_phone,
                    "estado": "pendiente",
                }
                await self._bus.publish(
                    f"bus:ventas:solicitud:{caller_phone}", solicitud, ttl=86400
                )
                return json.dumps(solicitud, ensure_ascii=False)

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
