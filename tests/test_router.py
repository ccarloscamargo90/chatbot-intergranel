"""Pruebas del router: comandos, continuidad de sesión y clasificación.

No invocan a Claude: se monkeypatchea `Router._classify` y se usan agentes
falsos que registran a quién se despachó.
"""

import asyncio

import pytest

from app.bus import InMemoryEventBus
from app.router import MENU_TEXT, Router


class FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple] = []

    async def handle(self, phone, content, store_text=None):
        self.calls.append((phone, content, store_text))
        return f"[{self.name}] {content}"


def _make_router() -> tuple[Router, dict, InMemoryEventBus]:
    bus = InMemoryEventBus()
    agents = {
        "ventas": FakeAgent("ventas"),
        "compras": FakeAgent("compras"),
        "inventario": FakeAgent("inventario"),
        "soporte": FakeAgent("soporte"),
    }
    return Router(agents=agents, bus=bus), agents, bus


# ------------------------------ Comandos --------------------------------- #
def test_parse_command_agente():
    router, _, _ = _make_router()
    assert router._parse_command("/ventas precio de maíz") == ("ventas", "precio de maíz")


def test_parse_command_solo():
    router, _, _ = _make_router()
    assert router._parse_command("/inventario") == ("inventario", "")


def test_parse_command_menu():
    router, _, _ = _make_router()
    assert router._parse_command("/menu") == ("menu", "")


def test_parse_command_texto_normal():
    router, _, _ = _make_router()
    assert router._parse_command("hola quiero precios") is None


def test_route_comando_despacha_a_agente():
    router, agents, bus = _make_router()
    reply = asyncio.run(router.route("521", "/ventas precio de maíz"))
    assert reply == "[ventas] precio de maíz"
    assert agents["ventas"].calls[0][1] == "precio de maíz"
    # El agente queda como activo para el siguiente turno.
    assert asyncio.run(bus.get_active_agent("521")) == "ventas"


def test_route_comando_solo_envia_saludo():
    router, agents, _ = _make_router()
    asyncio.run(router.route("521", "/compras"))
    assert agents["compras"].calls[0][1] == "Hola"


def test_route_menu_no_despacha():
    router, agents, _ = _make_router()
    reply = asyncio.run(router.route("521", "/menu"))
    assert reply == MENU_TEXT
    assert all(not a.calls for a in agents.values())


# --------------------------- Continuidad de sesión ----------------------- #
def test_route_usa_sesion_activa(monkeypatch):
    router, agents, bus = _make_router()
    asyncio.run(bus.set_active_agent("521", "inventario"))

    async def _boom(text):
        raise AssertionError("no debería clasificar si hay sesión activa")

    monkeypatch.setattr(router, "_classify", _boom)
    asyncio.run(router.route("521", "¿cuánto trigo hay?"))
    assert len(agents["inventario"].calls) == 1


# ------------------------------ Clasificación ---------------------------- #
def test_route_clasifica_sin_sesion(monkeypatch):
    router, agents, _ = _make_router()

    async def _fake_classify(text):
        return "ventas"

    monkeypatch.setattr(router, "_classify", _fake_classify)
    asyncio.run(router.route("999", "¿cuánto cuesta el sorgo?"))
    assert len(agents["ventas"].calls) == 1


def test_route_media_sin_sesion_clasifica_por_store_text(monkeypatch):
    router, agents, _ = _make_router()
    captured = {}

    async def _fake_classify(text):
        captured["text"] = text
        return "soporte"

    monkeypatch.setattr(router, "_classify", _fake_classify)
    content = [{"type": "image"}]
    asyncio.run(router.route("999", content, "[imagen recibida] mi orden"))
    assert captured["text"] == "[imagen recibida] mi orden"
    assert agents["soporte"].calls[0][1] == content


def test_classify_vacio_devuelve_soporte():
    router, _, _ = _make_router()
    assert asyncio.run(router._classify("   ")) == "soporte"


@pytest.mark.parametrize("agente", ["ventas", "compras", "inventario", "soporte"])
def test_comandos_todos_los_agentes(agente):
    router, agents, _ = _make_router()
    asyncio.run(router.route("521", f"/{agente} algo"))
    assert len(agents[agente].calls) == 1
