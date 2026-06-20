"""Bus de eventos compartido entre agentes.

Permite que los agentes publiquen y lean eventos (cotizaciones, solicitudes,
alertas, etc.) y que el router recuerde qué agente atiende a cada cliente.

Define una interfaz `EventBus` con dos implementaciones:

- `InMemoryEventBus`: diccionario en memoria con expiración por TTL
  (desarrollo; se pierde al reiniciar y no se comparte entre instancias).
- `RedisEventBus`: persiste en Redis con TTL (producción; sobrevive reinicios
  y se comparte entre réplicas).

Se elige automáticamente según `REDIS_URL`.

Convención de claves: ``bus:{agente}:{tipo}:{identificador}``, por ejemplo
``bus:ventas:cotizacion:5215512345678``. La sesión activa de cada cliente se
guarda bajo ``bus:session:{phone}`` con un TTL de 30 minutos.
"""

from __future__ import annotations

import abc
import json
import time
from functools import lru_cache

from .config import get_settings

# Duración de la "sesión" de un cliente con un agente (continuidad de turnos).
SESSION_TTL_SECONDS = 30 * 60
SESSION_PREFIX = "bus:session:"


class EventBus(abc.ABC):
    @abc.abstractmethod
    async def publish(self, key: str, data: dict, ttl: int | None = None) -> None:
        """Publica un evento (dict) bajo `key`, opcionalmente con expiración."""

    @abc.abstractmethod
    async def read(self, key: str) -> dict | None:
        """Lee el evento de `key`, o None si no existe o expiró."""

    @abc.abstractmethod
    async def read_prefix(self, prefix: str) -> list[dict]:
        """Lee todos los eventos cuyas claves empiezan con `prefix`."""

    # --- Sesión activa (qué agente atiende a un cliente) ------------------- #
    async def set_active_agent(self, phone: str, agent: str) -> None:
        await self.publish(
            f"{SESSION_PREFIX}{phone}", {"agente": agent}, ttl=SESSION_TTL_SECONDS
        )

    async def get_active_agent(self, phone: str) -> str | None:
        data = await self.read(f"{SESSION_PREFIX}{phone}")
        return data.get("agente") if data else None


class InMemoryEventBus(EventBus):
    def __init__(self) -> None:
        # key -> (data, expires_at | None)
        self._data: dict[str, tuple[dict, float | None]] = {}

    def _alive(self, key: str) -> dict | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        data, expires_at = entry
        if expires_at is not None and time.time() >= expires_at:
            self._data.pop(key, None)
            return None
        return data

    async def publish(self, key: str, data: dict, ttl: int | None = None) -> None:
        expires_at = time.time() + ttl if ttl else None
        self._data[key] = (dict(data), expires_at)

    async def read(self, key: str) -> dict | None:
        data = self._alive(key)
        return dict(data) if data is not None else None

    async def read_prefix(self, prefix: str) -> list[dict]:
        results = []
        for key in list(self._data.keys()):
            if key.startswith(prefix):
                data = self._alive(key)
                if data is not None:
                    results.append(dict(data))
        return results


class RedisEventBus(EventBus):
    def __init__(self, url: str) -> None:
        # Import diferido: solo se necesita 'redis' si se usa esta implementación.
        import redis.asyncio as redis

        self._redis = redis.from_url(url, decode_responses=True)

    async def publish(self, key: str, data: dict, ttl: int | None = None) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        if ttl:
            await self._redis.set(key, payload, ex=ttl)
        else:
            await self._redis.set(key, payload)

    async def read(self, key: str) -> dict | None:
        raw = await self._redis.get(key)
        return json.loads(raw) if raw else None

    async def read_prefix(self, prefix: str) -> list[dict]:
        results = []
        async for key in self._redis.scan_iter(match=f"{prefix}*"):
            raw = await self._redis.get(key)
            if raw:
                results.append(json.loads(raw))
        return results


@lru_cache
def get_event_bus() -> EventBus:
    """Devuelve el bus compartido (cacheado para que router y agentes usen el
    mismo en memoria)."""
    settings = get_settings()
    if settings.redis_url:
        return RedisEventBus(settings.redis_url)
    return InMemoryEventBus()
