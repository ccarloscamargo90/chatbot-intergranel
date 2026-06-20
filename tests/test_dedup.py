"""Pruebas de la deduplicación de webhooks (idempotencia)."""

import asyncio
import time

from app import main
from app.dedup import InMemoryDedupStore, get_dedup_store


def test_nuevo_luego_duplicado():
    store = InMemoryDedupStore(ttl_seconds=60)
    assert asyncio.run(store.is_duplicate("wamid.A")) is False  # nuevo
    assert asyncio.run(store.is_duplicate("wamid.A")) is True  # duplicado


def test_claves_independientes():
    store = InMemoryDedupStore(ttl_seconds=60)
    assert asyncio.run(store.is_duplicate("a")) is False
    assert asyncio.run(store.is_duplicate("b")) is False


def test_expira_por_ttl():
    store = InMemoryDedupStore(ttl_seconds=60)
    asyncio.run(store.is_duplicate("x"))
    # Forzamos la expiración de la entrada.
    store._seen["x"] = time.monotonic() - 1
    assert asyncio.run(store.is_duplicate("x")) is False  # se trata como nuevo


def test_get_dedup_store_memoria_por_defecto():
    assert isinstance(get_dedup_store(), InMemoryDedupStore)


def test_process_message_ignora_duplicados(monkeypatch):
    """El mismo message.id solo debe procesarse y responderse una vez."""
    handled: list = []
    sent: list = []

    async def fake_route(phone, content, store_text=None):
        handled.append((phone, content))
        return "ok"

    async def fake_send_text(to, text):
        sent.append((to, text))
        return {}

    monkeypatch.setattr(main.router, "route", fake_route)
    monkeypatch.setattr(main.wa, "send_text", fake_send_text)
    monkeypatch.setattr(main, "dedup", InMemoryDedupStore(ttl_seconds=60))

    message = {
        "id": "wamid.XYZ",
        "from": "5215512345678",
        "type": "text",
        "text": {"body": "hola"},
    }
    asyncio.run(main._process_message(message))
    asyncio.run(main._process_message(message))  # reenvío

    assert len(handled) == 1
    assert len(sent) == 1
