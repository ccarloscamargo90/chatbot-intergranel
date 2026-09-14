"""Trabajo que el cliente no tiene que esperar.

Cuando el cliente pide una cotización, lo que espera es su PDF. Redactar el
resumen para el vendedor —que es otra llamada a un modelo, y tarda— no puede
ir en ese camino: el cliente estaría viendo "escribiendo…" mientras se
escribe una nota que él nunca va a leer.

Así que ese trabajo se lanza aparte y la respuesta sale de inmediato. Dos
detalles que parecen de adorno y no lo son:

- **Se guarda la referencia de la tarea.** Una tarea de asyncio a la que nadie
  apunta puede ser recogida por el recolector de basura a media ejecución, y
  el resumen desaparecería sin que nada lo dijera.
- **Se registra la excepción.** Una tarea que falla sin que nadie mire su
  resultado se muere en silencio; con esto, al menos, el fallo queda en el log
  con el nombre de lo que se estaba haciendo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

#: Referencias fuertes al trabajo en vuelo. Sin esto, asyncio puede recoger
#: una tarea que nadie sostiene y el trabajo se pierde a la mitad.
_EN_VUELO: set[asyncio.Task] = set()


def lanzar(corrutina: Coroutine[Any, Any, Any], *, nombre: str) -> asyncio.Task | None:
    """Arranca el trabajo en segundo plano y devuelve de inmediato.

    Si no hay un bucle de eventos corriendo —una prueba sincrónica, un script—
    no se lanza nada y se dice en el log: es mejor que reventar el camino del
    cliente por una nota.
    """
    try:
        tarea = asyncio.get_running_loop().create_task(corrutina, name=nombre)
    except RuntimeError:
        corrutina.close()  # sin esto, Python avisa de una corrutina sin esperar
        logger.warning("Sin bucle de eventos: no se lanzó %s", nombre)
        return None
    _EN_VUELO.add(tarea)
    tarea.add_done_callback(_al_terminar)
    return tarea


def _al_terminar(tarea: asyncio.Task) -> None:
    _EN_VUELO.discard(tarea)
    if tarea.cancelled():
        return
    error = tarea.exception()
    if error is not None:
        logger.error(
            "Falló el trabajo en segundo plano %s: %s",
            tarea.get_name(),
            error,
            exc_info=error,
        )


async def esperar_todo(*, timeout: float = 15.0) -> None:
    """Espera a que termine el trabajo en vuelo.

    Sirve para dos cosas: apagar la aplicación sin tirar un resumen a medias,
    y poder revisar en una prueba lo que dejó una tarea. Si algo se queda
    pegado, se deja de esperar en vez de bloquear para siempre.
    """
    while _EN_VUELO:
        terminadas, _ = await asyncio.wait(set(_EN_VUELO), timeout=timeout)
        if not terminadas:
            logger.warning("Trabajo en segundo plano sin avanzar en %ss", timeout)
            return
