"""Pruebas del bus de eventos en memoria (sin red)."""

import asyncio

from app.bus import InMemoryEventBus


def test_publish_and_read():
    bus = InMemoryEventBus()
    asyncio.run(bus.publish("bus:ventas:cotizacion:521", {"total": 100}))
    data = asyncio.run(bus.read("bus:ventas:cotizacion:521"))
    assert data == {"total": 100}


def test_read_missing_returns_none():
    bus = InMemoryEventBus()
    assert asyncio.run(bus.read("bus:no:existe")) is None


def test_read_prefix():
    bus = InMemoryEventBus()
    asyncio.run(bus.publish("bus:inventario:alerta:trigo", {"p": "trigo"}))
    asyncio.run(bus.publish("bus:inventario:alerta:soya", {"p": "soya"}))
    asyncio.run(bus.publish("bus:ventas:cotizacion:1", {"p": "x"}))
    alertas = asyncio.run(bus.read_prefix("bus:inventario:alerta:"))
    productos = sorted(a["p"] for a in alertas)
    assert productos == ["soya", "trigo"]


def test_ttl_expira():
    bus = InMemoryEventBus()
    asyncio.run(bus.publish("bus:tmp:1", {"x": 1}, ttl=-1))  # ya expirado
    assert asyncio.run(bus.read("bus:tmp:1")) is None


def test_set_and_get_active_agent():
    bus = InMemoryEventBus()
    asyncio.run(bus.set_active_agent("5215512345678", "ventas"))
    assert asyncio.run(bus.get_active_agent("5215512345678")) == "ventas"


def test_get_active_agent_default_none():
    bus = InMemoryEventBus()
    assert asyncio.run(bus.get_active_agent("5215512345678")) is None


def test_read_returns_copy():
    bus = InMemoryEventBus()
    asyncio.run(bus.publish("bus:k", {"n": 1}))
    data = asyncio.run(bus.read("bus:k"))
    data["n"] = 999
    assert asyncio.run(bus.read("bus:k"))["n"] == 1
