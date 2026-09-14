"""Trabajo en segundo plano: el que no tiene que esperar el cliente.

Lo que se prueba son las dos cosas que parecen de adorno y no lo son: que la
tarea no se la lleve el recolector de basura a media ejecución, y que un fallo
quede en el log en vez de morirse en silencio.
"""

import asyncio
import gc
import logging

from app import tareas


def test_la_tarea_corre_y_se_puede_esperar():
    hecho = []

    async def trabajo():
        await asyncio.sleep(0)
        hecho.append("listo")

    async def escenario():
        tareas.lanzar(trabajo(), nombre="prueba")
        await tareas.esperar_todo(timeout=2)

    asyncio.run(escenario())
    assert hecho == ["listo"]


def test_nadie_tiene_que_sostener_la_tarea_para_que_termine():
    """Una tarea de asyncio a la que nadie apunta puede desaparecer a la
    mitad. El módulo guarda la referencia justo para eso."""
    hecho = []

    async def trabajo():
        await asyncio.sleep(0.01)
        hecho.append("listo")

    async def escenario():
        tareas.lanzar(trabajo(), nombre="sin-referencia")
        gc.collect()  # aquí se la llevaría si nadie la sostuviera
        await tareas.esperar_todo(timeout=2)

    asyncio.run(escenario())
    assert hecho == ["listo"]


def test_un_fallo_queda_en_el_log_con_el_nombre(caplog):
    async def trabajo():
        raise RuntimeError("se cayó el CRM")

    async def escenario():
        tareas.lanzar(trabajo(), nombre="resumen-cotizacion:COT-1")
        await tareas.esperar_todo(timeout=2)

    with caplog.at_level(logging.ERROR, logger="app.tareas"):
        asyncio.run(escenario())

    assert "resumen-cotizacion:COT-1" in caplog.text
    assert "se cayó el CRM" in caplog.text


def test_sin_bucle_de_eventos_no_revienta(caplog):
    """Un script o una prueba sincrónica no debe caerse por una nota."""

    async def trabajo():
        return None

    with caplog.at_level(logging.WARNING, logger="app.tareas"):
        assert tareas.lanzar(trabajo(), nombre="sin-bucle") is None
    assert "Sin bucle de eventos" in caplog.text


def test_esperar_sin_nada_en_vuelo_termina_de_inmediato():
    asyncio.run(tareas.esperar_todo(timeout=0.1))
