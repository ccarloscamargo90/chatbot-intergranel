"""Quién está hablando con una persona y no con el bot.

Mientras dura un handoff hacen falta dos búsquedas, y en direcciones opuestas:

- Llega un WhatsApp → ¿a qué conversación de Chatwoot lo mando?
- Llega un webhook de Chatwoot → ¿a qué teléfono le contesto?

Por eso se guardan las dos claves. Viven en el bus (Redis en producción), no en
memoria del proceso: el webhook de Chatwoot puede caer en una réplica distinta
de la que atendió el mensaje del cliente, y ahí una variable local no serviría
de nada.

El TTL no es un detalle de limpieza: es lo que impide que un cliente se quede
hablándole al vacío si nadie resuelve la conversación. Al vencer, el bot retoma.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .bus import EventBus, get_event_bus
from .config import get_settings

TELEFONO_PREFIX = "bus:handoff:telefono:"
CONVERSACION_PREFIX = "bus:handoff:conversacion:"


@dataclass
class Handoff:
    """Un cliente en manos de un asesor."""

    telefono: str
    conversacion_id: int
    abierto_en: float

    @property
    def minutos(self) -> int:
        return max(0, int((time.time() - self.abierto_en) // 60))


class HandoffStore:
    """Abre, consulta y cierra los handoffs."""

    def __init__(self, bus: EventBus | None = None, ttl_segundos: int | None = None) -> None:
        self._bus = bus or get_event_bus()
        self._ttl = ttl_segundos or get_settings().handoff_ttl_seconds

    async def abrir(self, telefono: str, conversacion_id: int) -> Handoff:
        handoff = Handoff(
            telefono=telefono, conversacion_id=conversacion_id, abierto_en=time.time()
        )
        datos = {
            "telefono": telefono,
            "conversacion_id": conversacion_id,
            "abierto_en": handoff.abierto_en,
        }
        await self._bus.publish(f"{TELEFONO_PREFIX}{telefono}", datos, ttl=self._ttl)
        await self._bus.publish(f"{CONVERSACION_PREFIX}{conversacion_id}", datos, ttl=self._ttl)
        return handoff

    async def por_telefono(self, telefono: str) -> Handoff | None:
        return self._leer(await self._bus.read(f"{TELEFONO_PREFIX}{telefono}"))

    async def por_conversacion(self, conversacion_id: int) -> Handoff | None:
        return self._leer(await self._bus.read(f"{CONVERSACION_PREFIX}{conversacion_id}"))

    async def cerrar(self, handoff: Handoff) -> None:
        """Devuelve el control al bot. Borra las dos claves."""
        await self._bus.publish(f"{TELEFONO_PREFIX}{handoff.telefono}", {}, ttl=1)
        await self._bus.publish(
            f"{CONVERSACION_PREFIX}{handoff.conversacion_id}", {}, ttl=1
        )

    @staticmethod
    def _leer(datos: dict | None) -> Handoff | None:
        if not datos or not datos.get("telefono"):
            return None
        return Handoff(
            telefono=datos["telefono"],
            conversacion_id=int(datos["conversacion_id"]),
            abierto_en=float(datos.get("abierto_en", 0)),
        )
