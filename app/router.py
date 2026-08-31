"""Router central: clasifica cada mensaje y lo despacha a un agente.

Orden de decisión:

1. Comando explícito: un `/comando` (`/ventas`, `/compras`, `/inventario`,
   `/soporte`, `/menu`) o el id de un botón del menú (`cli_*`, ver `menus.py`).
2. Sesión activa en el bus (continuidad con el agente del turno anterior).
3. Clasificación de intención con Claude Haiku (una palabra), con fallback a
   Soporte.

La respuesta es una `Reply` (texto + botones opcionales), no un string: así el
router puede contestar el menú con la lista interactiva de WhatsApp y los
agentes pueden colgarle botones de seguimiento a lo que contestan.

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
from .agents.proveedores import ProveedoresAgent
from .agents.soporte import SoporteAgent
from .agents.ventas import VentasAgent
from .bus import EventBus, get_event_bus
from .config import get_settings
from .menus import MENU, accion, menu_cliente, texto_menu
from .replies import Reply
from .sesiones import SesionClienteStore

logger = logging.getLogger(__name__)

DEFAULT_AGENT = "soporte"
CLASSIFIER_MODEL = "claude-haiku-4-5"

# Comandos explícitos -> nombre de agente.
COMMANDS = {
    "/ventas": "ventas",
    "/compras": "compras",
    "/inventario": "inventario",
    "/soporte": "soporte",
    "/proveedor": "proveedores",
}

CLASSIFIER_SYSTEM = (
    "Clasifica el mensaje del usuario de un chatbot de una comercializadora de "
    "granos en UNA de estas categorías y responde SOLO con la palabra exacta:\n"
    "- ventas: precios, cotizaciones, comprar producto, hacer un pedido.\n"
    "- compras: órdenes de compra a proveedores, abastecimiento interno.\n"
    "- inventario: existencias, stock, disponibilidad en silos.\n"
    "- soporte: estado de órdenes existentes, entregas, reclamos, dudas generales.\n"
    "Responde únicamente con: ventas, compras, inventario o soporte."
)

# `proveedores` NO es una categoría del clasificador a propósito. Un modelo
# no puede distinguir por el texto si quien pregunta "¿cuándo me pagan?" es
# un proveedor o un cliente pidiendo su nota de crédito, y equivocarse ahí
# manda a alguien a identificarse contra el padrón que no es. Se entra por
# el botón "🚚 Soy proveedor" o por /proveedor: intenciones exactas.
# Soporte, que es el fallback, sabe ofrecer esa puerta.


def _build_default_agents(bus: EventBus) -> dict[str, BaseAgent]:
    return {
        "ventas": VentasAgent(bus=bus),
        "compras": ComprasAgent(bus=bus),
        "inventario": InventarioAgent(bus=bus),
        "soporte": SoporteAgent(bus=bus),
        "proveedores": ProveedoresAgent(bus=bus),
    }


class Router:
    def __init__(
        self,
        agents: dict[str, BaseAgent] | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._bus = bus or get_event_bus()
        self._agents = agents if agents is not None else _build_default_agents(self._bus)
        self._sesiones = SesionClienteStore(self._bus)
        settings = get_settings()
        self._api_key = settings.anthropic_api_key or None
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    def _parse_command(self, text: str) -> tuple[str, str] | None:
        """Si el mensaje es un comando conocido, devuelve (agente, resto).

        Cuentan como comando dos cosas:

        - Un `/comando` escrito por la persona.
        - El id de un botón del menú (`cli_*`), que llega como texto desde
          `main.py`. Un toque es una intención EXACTA, así que salta la
          clasificación por completo: ir a preguntarle a un modelo qué quiso
          decir quien ya nos lo dijo sería gastar tiempo para perder precisión.

        Para el menú devuelve ("menu", ""). None si no hay comando."""
        if not text:
            return None
        limpio = text.strip()

        if limpio == MENU:
            return "menu", ""
        toque = accion(limpio)
        if toque is not None:
            return toque.agente, toque.texto

        if not limpio.startswith("/"):
            return None
        head, _, rest = limpio.partition(" ")
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

    async def _menu(self, phone: str) -> Reply:
        """El menú principal, con las opciones que correspondan a si el cliente
        ya se identificó o no."""
        sesion = await self._sesiones.leer(phone)
        identificado = sesion is not None
        return Reply(
            texto=texto_menu(identificado, sesion.cliente if sesion else ""),
            lista=menu_cliente(identificado),
        )

    async def route(
        self,
        phone: str,
        content: str | list,
        store_text: str | None = None,
    ) -> Reply:
        """Decide el agente y delega el mensaje. Devuelve la respuesta."""
        text = content if isinstance(content, str) else (store_text or "")

        command = self._parse_command(text) if isinstance(content, str) else None
        if command is not None:
            agent_name, rest = command
            if agent_name == "menu":
                return await self._menu(phone)
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
        return Reply.coerce(await agent.handle(phone, content, store_text))
