"""Deduplicación de webhooks (idempotencia) por identificador de mensaje.

Meta puede reenviar el mismo webhook varias veces (reintentos ante timeouts o
errores), por lo que el mismo `message.id` puede llegar más de una vez.
Procesarlo dos veces duplicaría respuestas al cliente. Aquí registramos cada
id visto con un TTL y detectamos duplicados de forma atómica.

- `InMemoryDedupStore`: para desarrollo (no compartido entre réplicas).
- `RedisDedupStore`: para producción, usando `SET key NX EX ttl` (atómico, y
  correcto incluso con varias réplicas o entregas concurrentes).

Se elige automáticamente según `REDIS_URL`.
"""

from __future__ import annotations

import abc
import time

from .config import get_settings


class DedupStore(abc.ABC):
    @abc.abstractmethod
    async def is_duplicate(self, key: str) -> bool:
        """Marca `key` como vista y devuelve True si YA había sido vista."""


class InMemoryDedupStore(DedupStore):
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._seen: dict[str, float] = {}

    async def is_duplicate(self, key: str) -> bool:
        now = time.monotonic()
        # Purga perezosa de entradas expiradas.
        for k in [k for k, exp in self._seen.items() if exp <= now]:
            del self._seen[k]
        if key in self._seen:
            return True
        self._seen[key] = now + self._ttl
        return False


class RedisDedupStore(DedupStore):
    def __init__(self, url: str, ttl_seconds: int, prefix: str = "wa:dedup:") -> None:
        import redis.asyncio as redis

        self._redis = redis.from_url(url, decode_responses=True)
        self._ttl = ttl_seconds
        self._prefix = prefix

    async def is_duplicate(self, key: str) -> bool:
        # SET NX EX devuelve True si la clave se creó (mensaje nuevo) y None si
        # ya existía (duplicado). Operación atómica.
        was_set = await self._redis.set(
            f"{self._prefix}{key}", "1", nx=True, ex=self._ttl
        )
        return not was_set


def get_dedup_store() -> DedupStore:
    settings = get_settings()
    if settings.redis_url:
        return RedisDedupStore(settings.redis_url, settings.dedup_ttl_seconds)
    return InMemoryDedupStore(settings.dedup_ttl_seconds)
