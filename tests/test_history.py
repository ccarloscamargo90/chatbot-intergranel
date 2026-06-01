"""Pruebas del almacenamiento del historial."""

import asyncio

from app.history import InMemoryHistoryStore, get_history_store


def test_in_memory_load_vacio():
    store = InMemoryHistoryStore()
    assert asyncio.run(store.load("5215512345678")) == []


def test_in_memory_save_y_load():
    store = InMemoryHistoryStore()
    history = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": [{"type": "text", "text": "¡Hola!"}]},
    ]
    asyncio.run(store.save("5215512345678", history))
    cargado = asyncio.run(store.load("5215512345678"))
    assert cargado == history


def test_in_memory_aisla_por_telefono():
    store = InMemoryHistoryStore()
    asyncio.run(store.save("111", [{"role": "user", "content": "a"}]))
    assert asyncio.run(store.load("222")) == []


def test_save_devuelve_copia_independiente():
    store = InMemoryHistoryStore()
    history = [{"role": "user", "content": "hola"}]
    asyncio.run(store.save("111", history))
    history.append({"role": "user", "content": "mutación externa"})
    # La mutación posterior no debe afectar lo almacenado.
    assert len(asyncio.run(store.load("111"))) == 1


def test_get_history_store_usa_memoria_por_defecto():
    # Sin REDIS_URL configurado (default), debe usarse el store en memoria.
    assert isinstance(get_history_store(), InMemoryHistoryStore)
