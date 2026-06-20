"""Router central: clasifica cada mensaje y lo despacha a un agente.

Orden de decisión:

1. Comando explícito (`/ventas`, `/compras`, `/inventario`, `/soporte`, `/menu`).
2. Sesión activa en el bus (continuidad con el agente del turno anterior).
3. Clasificación de intención con Claude Haiku (una palabra), con fallback a
   Soporte.

Tras decidir el agente, se fija como agente activo en el bus (TTL 30 min) y se
delega el mensaje a `agent.handle(...)`. Las herramientas `transferir_a_*` de
los agentes pueden cambiar el agente activo durante el turno; ese cambio se
respeta para el siguiente mensaje.
"""

from __future__ import annotations

import logging

import anthropic

from .agents.base import BaseAgent
from .agents.compras import ComprasAgent
from .agents.inventario import InventarioAgent
from .agents.soporte import SoporteAgent
from .agents.ventas import VentasAgent
from .bus import EventBus, get_event_bus
from .config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_AGENT = "soporte"
CLASSIFIER_MODEL = "claude-haiku-4-5"

# Comandos explícitos -> nombre de agente.
COMMANDS = {
    "/ventas": "ventas",
    "/compras": "compras",
    "/inventario": "inventario",
    "/soporte": "soporte",
}

MENU_TEXT = (
    "¿En qué le puedo ayudar? 🌾\n\n"
    "• /ventas — precios, cotizaciones y pedidos\n"
    "• /inventario — existencias y disponibilidad\n"
    "• /compras — órdenes de compra a proveedores\n"
    "• /soporte — estado de sus órdenes y atención\n\n"
    "También puede escribir su consulta directamente y le atenderé."
)

CLASSIFIER_SYSTEM = (
    "Clasifica el mensaje del usuario de un chatbot de una comercializadora de "
    "granos en UNA de estas categorías y responde SOLO con la palabra exacta:\n"
    "- ventas: precios, cotizaciones, comprar producto, hacer un pedido.\n"
    "- compras: órdenes de compra a proveedores, abastecimiento interno.\n"
    "- inventario: existencias, stock, disponibilidad en silos.\n"
    "- soporte: estado de órdenes existentes, entregas, reclamos, dudas generales.\n"
    "Responde únicamente con: ventas, compras, inventario o soporte."
)


def _build_default_agents(bus: EventBus) -> dict[str, BaseAgent]:
    return {
        "ventas": VentasAgent(bus=bus),
        "compras": ComprasAgent(bus=bus),
        "inventario": InventarioAgent(bus=bus),
        "soporte": SoporteAgent(bus=bus),
    }


class Router:
    def __init__(
        self,
        agents: dict[str, BaseAgent] | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._bus = bus or get_event_bus()
        self._agents = agents if agents is not None else _build_default_agents(self._bus)
        settings = get_settings()
        self._api_key = settings.anthropic_api_key or None
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    def _parse_command(self, text: str) -> tuple[str, str] | None:
        """Si el texto empieza con un comando conocido, devuelve (agente, resto).

        Para `/menu` devuelve ("menu", ""). None si no hay comando."""
        if not text or not text.startswith("/"):
            return None
        head, _, rest = text.strip().partition(" ")
        cmd = head.lower()
        if cmd == "/menu":
            return "menu", ""
        if cmd in COMMANDS:
            return COMMANDS[cmd], rest.strip()
        return None

    async def _classify(self, text: str) -> str:
        """Clasifica la intención con Claude Haiku. Fallback: soporte."""
        if not text.strip():
            return DEFAULT_AGENT
        try:
            response = await self.client.messages.create(
                model=CLASSIFIER_MODEL,
                max_tokens=20,
                system=CLASSIFIER_SYSTEM,
                messages=[{"role": "user", "content": text}],
            )
            answer = "".join(
                b.text for b in response.content if getattr(b, "type", "") == "text"
            ).strip().lower()
            for agent_name in self._agents:
                if agent_name in answer:
                    return agent_name
            return DEFAULT_AGENT
        except Exception:  # noqa: BLE001
            logger.exception("Error clasificando intención; uso fallback soporte")
            return DEFAULT_AGENT

    async def route(
        self,
        phone: str,
        content: str | list,
        store_text: str | None = None,
    ) -> str:
        """Decide el agente y delega el mensaje. Devuelve la respuesta."""
        text = content if isinstance(content, str) else (store_text or "")

        command = self._parse_command(text) if isinstance(content, str) else None
        if command is not None:
            agent_name, rest = command
            if agent_name == "menu":
                return MENU_TEXT
            # Si el comando trae texto adicional, ese es el mensaje; si no, un saludo.
            content = rest or "Hola"
            store_text = content
        else:
            agent_name = await self._bus.get_active_agent(phone)
            if agent_name not in self._agents:
                agent_name = await self._classify(text)

        # Fija el agente activo para dar continuidad al siguiente turno. Una
        # herramienta transferir_a_* puede cambiarlo durante el handle.
        await self._bus.set_active_agent(phone, agent_name)
        agent = self._agents[agent_name]
        return await agent.handle(phone, content, store_text)
