"""Almacenamiento del historial de conversación por número de teléfono.

Define una interfaz `HistoryStore` con dos implementaciones:

- `InMemoryHistoryStore`: diccionario en memoria (desarrollo; se pierde al
  reiniciar y no se comparte entre instancias).
- `RedisHistoryStore`: persiste en Redis con TTL (producción; sobrevive
  reinicios y se comparte entre réplicas).

Se elige automáticamente según `REDIS_URL`. El historial se guarda como JSON,
por lo que el contenido del asistente debe serializarse a dicts antes de
almacenarlo (ver `app/assistant.py`).
"""

from __future__ import annotations

import abc
import json

from .config import get_settings


class HistoryStore(abc.ABC):
    @abc.abstractmethod
    async def load(self, phone: str) -> list:
        """Devuelve el historial (lista de mensajes) del número, o []."""

    @abc.abstractmethod
    async def save(self, phone: str, history: list) -> None:
        """Guarda el historial del número."""


class InMemoryHistoryStore(HistoryStore):
    def __init__(self) -> None:
        self._data: dict[str, list] = {}

    async def load(self, phone: str) -> list:
        return list(self._data.get(phone, []))

    async def save(self, phone: str, history: list) -> None:
        self._data[phone] = list(history)


class RedisHistoryStore(HistoryStore):
    def __init__(
        self,
        url: str,
        ttl_seconds: int = 60 * 60 * 24 * 7,
        prefix: str = "wa:hist:",
    ) -> None:
        # Import diferido: solo se necesita 'redis' si se usa esta implementación.
        import redis.asyncio as redis

        self._redis = redis.from_url(url, decode_responses=True)
        self._ttl = ttl_seconds
        self._prefix = prefix

    def _key(self, phone: str) -> str:
        return f"{self._prefix}{phone}"

    async def load(self, phone: str) -> list:
        raw = await self._redis.get(self._key(phone))
        return json.loads(raw) if raw else []

    async def save(self, phone: str, history: list) -> None:
        await self._redis.set(
            self._key(phone), json.dumps(history, ensure_ascii=False), ex=self._ttl
        )


def get_history_store() -> HistoryStore:
    settings = get_settings()
    if settings.redis_url:
        return RedisHistoryStore(settings.redis_url, settings.history_ttl_seconds)
    return InMemoryHistoryStore()
