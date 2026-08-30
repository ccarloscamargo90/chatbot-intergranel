"""Sesión de autoservicio del cliente, sobre el bus.

El cliente se identifica una vez (nombre o razón social + RFC) y el ERP le
devuelve un token con caducidad. Aquí se guarda ese token contra su teléfono,
para no volver a pedirle los datos en cada mensaje.

Dos cosas que conviene tener presentes:

- **La autoridad es el ERP.** Esto es una caché: si el token caduca del lado del
  ERP, la consulta falla con `SesionClienteInvalida` aunque aquí siga viva. Por
  eso `leer()` no promete que el token sirva, solo que existía.
- **El freno de intentos de aquí es de cortesía.** El bloqueo real, el que
  cuenta y audita, vive en el ERP: es el único que ve TODOS los intentos, no
  solo los de esta réplica. Este contador sirve para contestar rápido y no
  gastar un viaje al ERP por cada intento a ciegas.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .bus import EventBus, get_event_bus

SESION_PREFIX = "bus:clientes:sesion:"
INTENTOS_PREFIX = "bus:clientes:intentos:"

# Ventana del contador local de intentos fallidos.
INTENTOS_TTL_SECONDS = 15 * 60
# A partir de aquí el bot deja de intentar contra el ERP y pide esperar.
MAX_INTENTOS_LOCALES = 5


@dataclass
class SesionCliente:
    """Un cliente identificado, visto desde el bot."""

    token: str
    cliente: str
    rfc: str
    expira_en: float  # epoch (segundos)
    abierta_en: float = 0.0  # epoch en que se identificó

    @property
    def vigente(self) -> bool:
        return time.time() < self.expira_en

    def recien_abierta(self, segundos: int = 20) -> bool:
        """True si la identificación acaba de ocurrir.

        Sirve para un detalle de trato: justo después de identificarse es
        cuando el menú completo vale más que dos botones, porque es el momento
        en que el cliente descubre qué puede pedir.
        """
        return bool(self.abierta_en) and (time.time() - self.abierta_en) <= segundos

    @property
    def minutos_restantes(self) -> int:
        return max(0, int((self.expira_en - time.time()) // 60))


class SesionClienteStore:
    """Guarda y recupera la sesión de autoservicio de cada teléfono."""

    def __init__(self, bus: EventBus | None = None) -> None:
        self._bus = bus or get_event_bus()

    # --- Sesión ------------------------------------------------------------ #
    async def abrir(
        self, telefono: str, token: str, cliente: str, rfc: str, ttl_segundos: int
    ) -> SesionCliente:
        ahora = time.time()
        sesion = SesionCliente(
            token=token,
            cliente=cliente,
            rfc=rfc,
            expira_en=ahora + ttl_segundos,
            abierta_en=ahora,
        )
        await self._bus.publish(
            f"{SESION_PREFIX}{telefono}",
            {
                "token": sesion.token,
                "cliente": sesion.cliente,
                "rfc": sesion.rfc,
                "expira_en": sesion.expira_en,
                "abierta_en": sesion.abierta_en,
            },
            ttl=ttl_segundos,
        )
        await self.limpiar_intentos(telefono)
        return sesion

    async def leer(self, telefono: str) -> SesionCliente | None:
        """La sesión del teléfono, o None si no hay o ya caducó."""
        datos = await self._bus.read(f"{SESION_PREFIX}{telefono}")
        if not datos or not datos.get("token"):
            return None
        sesion = SesionCliente(
            token=datos["token"],
            cliente=datos.get("cliente", ""),
            rfc=datos.get("rfc", ""),
            expira_en=float(datos.get("expira_en", 0)),
            abierta_en=float(datos.get("abierta_en", 0)),
        )
        return sesion if sesion.vigente else None

    async def cerrar(self, telefono: str) -> None:
        """Borra la sesión local. Se llama también cuando el ERP dice que el
        token ya no sirve, para no seguir mandando uno muerto."""
        await self._bus.publish(f"{SESION_PREFIX}{telefono}", {}, ttl=1)

    # --- Intentos fallidos ------------------------------------------------- #
    async def registrar_intento_fallido(self, telefono: str) -> int:
        """Suma uno al contador local y devuelve el total de la ventana.

        No es atómico (leer-sumar-escribir): dos mensajes simultáneos del mismo
        teléfono pueden contar como uno. Se acepta a propósito — el bloqueo que
        importa lo lleva el ERP, aquí solo se busca cortar el tecleo repetido.
        """
        clave = f"{INTENTOS_PREFIX}{telefono}"
        datos = await self._bus.read(clave) or {}
        total = int(datos.get("intentos", 0)) + 1
        await self._bus.publish(clave, {"intentos": total}, ttl=INTENTOS_TTL_SECONDS)
        return total

    async def intentos(self, telefono: str) -> int:
        datos = await self._bus.read(f"{INTENTOS_PREFIX}{telefono}") or {}
        return int(datos.get("intentos", 0))

    async def limpiar_intentos(self, telefono: str) -> None:
        await self._bus.publish(f"{INTENTOS_PREFIX}{telefono}", {"intentos": 0}, ttl=1)

    async def bloqueado(self, telefono: str) -> bool:
        return await self.intentos(telefono) >= MAX_INTENTOS_LOCALES
