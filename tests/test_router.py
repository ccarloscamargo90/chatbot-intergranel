"""Pruebas del router: comandos, continuidad de sesión y clasificación.

No invocan a Claude: se monkeypatchea `Router._classify` y se usan agentes
falsos que registran a quién se despachó.
"""

import asyncio

import pytest

from app.atribucion import ReferenciasWeb
from app.bus import InMemoryEventBus
from app.menus import (
    ASESOR,
    CERRAR_SESION,
    COT_COSTAL_25,
    COTIZAR,
    IDENTIFICARME,
    MENU,
    PRECIOS,
    SALDO,
)
from app.router import CLASSIFIER_SYSTEM, Router


class FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple] = []
        self.anotados: list[tuple] = []

    async def handle(self, phone, content, store_text=None):
        self.calls.append((phone, content, store_text))
        return f"[{self.name}] {content}"

    async def anotar_turno(self, phone, usuario, respuesta):
        self.anotados.append((phone, usuario, respuesta))


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
    assert reply.texto == "[ventas] precio de maíz"
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
    # El menú lo contesta el propio router, con la lista interactiva.
    assert reply.lista is not None
    assert all(not a.calls for a in agents.values())


# ------------------------------ Botones ---------------------------------- #
def test_boton_menu_devuelve_el_menu():
    router, agents, _ = _make_router()
    reply = asyncio.run(router.route("521", MENU))
    assert reply.lista is not None
    assert all(not a.calls for a in agents.values())


def test_menu_sin_sesion_solo_ofrece_lo_que_no_expone_datos():
    router, _, _ = _make_router()
    reply = asyncio.run(router.route("521", MENU))
    ids = {o.id for o in reply.lista.opciones}
    assert SALDO not in ids
    assert CERRAR_SESION not in ids


def test_boton_despacha_al_agente_con_su_frase(monkeypatch):
    router, agents, bus = _make_router()

    async def _boom(text):
        raise AssertionError("un toque de botón no se clasifica: ya es una intención")

    monkeypatch.setattr(router, "_classify", _boom)
    asyncio.run(router.route("521", SALDO))
    assert len(agents["soporte"].calls) == 1
    assert "saldo" in agents["soporte"].calls[0][1].lower()
    assert asyncio.run(bus.get_active_agent("521")) == "soporte"


def test_boton_de_precios_va_a_ventas(monkeypatch):
    router, agents, _ = _make_router()

    async def _boom(text):
        raise AssertionError("un toque de botón no se clasifica")

    monkeypatch.setattr(router, "_classify", _boom)
    asyncio.run(router.route("521", PRECIOS))
    assert len(agents["ventas"].calls) == 1


def test_id_desconocido_no_es_comando():
    router, _, _ = _make_router()
    assert router._parse_command("cli_no_existe") is None


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


# ------------------------ Mensaje de la página web ------------------------ #
DE_LA_PAGINA = (
    "Hola Intergranel, quisiera información sobre sus granos y servicios. "
    "(ref: IG-SOC-4M2P6X)"
)


def _sin_clasificar(monkeypatch, router):
    async def _boom(text):
        raise AssertionError("el mensaje de la página no se clasifica: ya dice a qué viene")

    monkeypatch.setattr(router, "_classify", _boom)


def test_quien_llega_de_la_pagina_recibe_la_bienvenida_de_ventas(monkeypatch):
    """Antes caía en Soporte y se le ofrecían contratos y facturas a alguien
    que todavía no ha comprado nada."""
    router, agents, bus = _make_router()
    _sin_clasificar(monkeypatch, router)

    reply = asyncio.run(router.route("521", DE_LA_PAGINA))

    assert [b.id for b in reply.botones] == [COTIZAR, IDENTIFICARME, ASESOR]
    assert "cotización" in reply.texto
    # Nadie contestó por modelo: es una bienvenida fija, inmediata.
    assert all(not a.calls for a in agents.values())
    # Y lo que escriba después le llega a Ventas, no a Soporte.
    assert asyncio.run(bus.get_active_agent("521")) == "ventas"


def test_la_bienvenida_no_empieza_por_el_precio():
    """Regla del guion: primero saludar y calificar; el precio, después."""
    router, _, _ = _make_router()
    reply = asyncio.run(router.route("521", DE_LA_PAGINA))
    assert "$" not in reply.texto
    assert PRECIOS not in [b.id for b in reply.botones]


def test_ventas_sabe_que_ya_saludo():
    """El siguiente mensaje ("Quiero cotizar.") le llega a Ventas; sin la
    bienvenida en su historial, saludaría otra vez desde cero."""
    router, agents, _ = _make_router()
    reply = asyncio.run(router.route("521", DE_LA_PAGINA))
    ((telefono, usuario, respuesta),) = agents["ventas"].anotados
    assert telefono == "521"
    assert "(ref:" not in usuario
    assert respuesta == reply.texto


def test_la_referencia_se_guarda_para_el_crm():
    router, _, bus = _make_router()
    asyncio.run(router.route("521", DE_LA_PAGINA))
    assert asyncio.run(ReferenciasWeb(bus).leer("521")) == "IG-SOC-4M2P6X"


def test_si_escribio_algo_propio_se_le_contesta_eso(monkeypatch):
    router, agents, _ = _make_router()
    _sin_clasificar(monkeypatch, router)

    asyncio.run(router.route("521", "Necesito 40 toneladas en Celaya (ref: IG-ADS-K7Q9RW)"))

    ((_, contenido, guardado),) = agents["ventas"].calls
    assert contenido == "Necesito 40 toneladas en Celaya"
    assert guardado == "Necesito 40 toneladas en Celaya"


def test_la_pagina_gana_aunque_viniera_hablando_con_soporte(monkeypatch):
    """Tocar el botón de la página es empezar de nuevo, y a comprar."""
    router, agents, bus = _make_router()
    asyncio.run(bus.set_active_agent("521", "soporte"))

    reply = asyncio.run(router.route("521", DE_LA_PAGINA))

    assert COTIZAR in [b.id for b in reply.botones]
    assert not agents["soporte"].calls
    assert asyncio.run(bus.get_active_agent("521")) == "ventas"


def test_despues_de_la_bienvenida_lo_escrito_va_a_ventas(monkeypatch):
    router, agents, _ = _make_router()
    asyncio.run(router.route("521", DE_LA_PAGINA))
    _sin_clasificar(monkeypatch, router)
    asyncio.run(router.route("521", "¿a cómo el maíz?"))
    assert len(agents["ventas"].calls) == 1


def test_un_folio_de_contrato_no_se_toma_por_la_pagina(monkeypatch):
    router, agents, _ = _make_router()
    asyncio.run(router.route("521", "/soporte ¿cómo va el CONT-2026-0001?"))
    assert len(agents["soporte"].calls) == 1
    assert not agents["ventas"].anotados


def test_el_boton_cotizar_va_a_ventas(monkeypatch):
    router, agents, _ = _make_router()
    _sin_clasificar(monkeypatch, router)
    asyncio.run(router.route("521", COTIZAR))
    assert agents["ventas"].calls[0][1] == "Quiero cotizar."


def test_un_paso_de_la_cotizacion_va_a_ventas_con_su_frase(monkeypatch):
    router, agents, _ = _make_router()
    _sin_clasificar(monkeypatch, router)
    asyncio.run(router.route("521", COT_COSTAL_25))
    assert agents["ventas"].calls[0][1] == "Lo quiero en costal de 25 kg."


def test_el_clasificador_manda_la_informacion_de_granos_a_ventas():
    """Quien pide información de los granos está pensando en comprar."""
    renglon_ventas = next(r for r in CLASSIFIER_SYSTEM.split("\n") if r.startswith("- ventas"))
    assert "información sobre los granos" in renglon_ventas
