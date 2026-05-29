"""Asistente de IA (Claude) para atención al cliente por WhatsApp.

Gestiona la conversación con cada cliente y usa herramientas (tools) para
consultar órdenes de compra en el ERP. El historial se guarda en memoria por
número de teléfono; en producción conviene moverlo a Redis o una base de datos.
"""

from __future__ import annotations

import json
import logging

import anthropic

from .config import get_settings
from .erp import ERPClient, get_erp_client
from .models import Order

logger = logging.getLogger(__name__)

# Cuántos mensajes recientes conservar por conversación (pares usuario/asistente
# más resultados de herramientas).
MAX_HISTORY = 24

SYSTEM_PROMPT = """\
Eres el asistente virtual de Intergranel, una empresa de comercialización de \
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
            "identificador (p. ej. 'OC-1001')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Identificador de la orden, p. ej. OC-1001",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "listar_ordenes_cliente",
        "description": (
            "Lista las órdenes de compra asociadas a un número de teléfono. "
            "Si no se especifica teléfono, usa el del cliente que está "
            "escribiendo."
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
            "tiene un reclamo, o la solicitud está fuera del alcance del asistente."
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


def _order_to_dict(order: Order) -> dict:
    return order.model_dump()


class Assistant:
    def __init__(self, erp: ERPClient | None = None) -> None:
        settings = get_settings()
        self._api_key = settings.anthropic_api_key or None
        self._model = settings.claude_model
        self._erp = erp or get_erp_client()
        self._history: dict[str, list] = {}
        # El cliente se crea de forma diferida para que la app arranque aunque
        # ANTHROPIC_API_KEY aún no esté configurada (útil en el primer deploy).
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    def _trim(self, history: list) -> list:
        """Limita el historial y garantiza que empiece en un turno de usuario
        limpio (evita resultados de herramienta huérfanos al inicio)."""
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        while history and not (
            history[0]["role"] == "user" and isinstance(history[0]["content"], str)
        ):
            history.pop(0)
        return history

    async def _run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name == "consultar_orden":
                order = await self._erp.get_order(tool_input["order_id"])
                if order is None:
                    return json.dumps(
                        {"encontrada": False, "order_id": tool_input["order_id"]}
                    )
                return json.dumps(
                    {"encontrada": True, "orden": _order_to_dict(order)},
                    ensure_ascii=False,
                )

            if name == "listar_ordenes_cliente":
                phone = tool_input.get("telefono") or caller_phone
                orders = await self._erp.list_orders_by_phone(phone)
                return json.dumps(
                    {
                        "telefono": phone,
                        "total": len(orders),
                        "ordenes": [_order_to_dict(o) for o in orders],
                    },
                    ensure_ascii=False,
                )

            if name == "escalar_a_humano":
                motivo = tool_input.get("motivo", "(sin especificar)")
                logger.info("Escalamiento solicitado (%s): %s", caller_phone, motivo)
                # Aquí podrías crear un ticket, notificar a un asesor, etc.
                return json.dumps(
                    {
                        "escalado": True,
                        "mensaje": (
                            "Un asesor de Intergranel continuará la atención en "
                            "breve."
                        ),
                    },
                    ensure_ascii=False,
                )

            return json.dumps({"error": f"herramienta desconocida: {name}"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error ejecutando herramienta %s", name)
            return json.dumps({"error": str(exc)})

    async def handle(self, phone: str, user_text: str) -> str:
        """Procesa un mensaje entrante y devuelve la respuesta del asistente."""
        history = self._history.setdefault(phone, [])
        history.append({"role": "user", "content": user_text})

        # Bucle agéntico: continúa mientras Claude solicite herramientas.
        while True:
            response = await self.client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOLS,
                messages=history,
            )
            history.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = await self._run_tool(block.name, block.input, phone)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            }
                        )
                history.append({"role": "user", "content": tool_results})
                continue

            # Respuesta final.
            reply = "".join(
                b.text for b in response.content if getattr(b, "type", "") == "text"
            ).strip()
            self._history[phone] = self._trim(history)
            return reply or "Disculpe, no pude generar una respuesta. ¿Puede reformular su mensaje?"
