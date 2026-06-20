"""Clase base de los agentes especializados.

Cada agente hereda de `BaseAgent` y define su nombre, su prompt de sistema, sus
herramientas (tools) y la ejecución de cada una. `BaseAgent.handle` implementa
el bucle agéntico completo (cargar historial → llamar a Claude → ejecutar tools
→ continuar → persistir), compartido por todos los agentes.

El historial se guarda por número de teléfono y agente, de modo que cada agente
mantiene su propio hilo de conversación limpio (sin mezclar tool_use de tools
que no le pertenecen).
"""

from __future__ import annotations

import abc
import logging

import anthropic

from ..bus import EventBus, get_event_bus
from ..config import get_settings
from ..erp import ERPClient, get_erp_client
from ..history import HistoryStore, get_history_store

logger = logging.getLogger(__name__)

# Cuántos mensajes recientes conservar por conversación.
MAX_HISTORY = 24


class BaseAgent(abc.ABC):
    #: Nombre único del agente ("ventas", "compras", "inventario", "soporte").
    name: str = ""

    def __init__(
        self,
        erp: ERPClient | None = None,
        history_store: HistoryStore | None = None,
        bus: EventBus | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = settings.anthropic_api_key or None
        self._model = settings.claude_model
        self._erp = erp or get_erp_client()
        self._history_store = history_store or get_history_store()
        self._bus = bus or get_event_bus()
        # El cliente se crea de forma diferida para que la app arranque aunque
        # ANTHROPIC_API_KEY aún no esté configurada (útil en el primer deploy).
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    # --- Interfaz que cada agente concreto debe implementar ---------------- #
    @abc.abstractmethod
    def system_prompt(self) -> str:
        """Prompt de sistema especializado del agente."""

    @abc.abstractmethod
    def tools(self) -> list[dict]:
        """Definición de herramientas para la API de Claude."""

    @abc.abstractmethod
    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        """Ejecuta una herramienta y devuelve un string JSON con el resultado."""

    # --- Bucle agéntico común ---------------------------------------------- #
    def _history_key(self, phone: str) -> str:
        return f"{phone}:{self.name}"

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

    async def handle(
        self,
        phone: str,
        content: str | list,
        store_text: str | None = None,
    ) -> str:
        """Procesa un mensaje entrante y devuelve la respuesta del agente.

        `content` es el contenido del turno del usuario para la API: un string
        (texto) o una lista de bloques (imagen/documento + texto). `store_text`
        es la versión que se persiste en el historial; para multimedia conviene
        un placeholder de texto para no almacenar ni reenviar base64 pesado.
        """
        if store_text is None:
            store_text = content if isinstance(content, str) else "[contenido multimedia]"
        key = self._history_key(phone)
        history = await self._history_store.load(key)
        history.append({"role": "user", "content": content})
        user_index = len(history) - 1

        # Bucle agéntico: continúa mientras Claude solicite herramientas.
        while True:
            response = await self.client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": self.system_prompt(),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=self.tools(),
                messages=history,
            )
            history.append(
                {
                    "role": "assistant",
                    "content": [b.model_dump(mode="json") for b in response.content],
                }
            )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = await self.run_tool(block.name, block.input, phone)
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
            # Sustituimos el turno del usuario por su placeholder de texto antes
            # de persistir (no guardar base64 de imágenes/PDF).
            history[user_index] = {"role": "user", "content": store_text}
            await self._history_store.save(key, self._trim(history))
            return reply or "Disculpe, no pude generar una respuesta. ¿Puede reformular su mensaje?"
