"""Pruebas del agente de Inventario (tools contra mock y bus en memoria)."""

import asyncio
import json

import pytest

from app.agents.inventario import InventarioAgent
from app.bus import InMemoryEventBus
from app.config import get_settings
from app.erp import MockERPClient

PHONE = "5215512345678"


@pytest.fixture
def inventario() -> InventarioAgent:
    a = InventarioAgent.__new__(InventarioAgent)
    a._erp = MockERPClient()
    a._bus = InMemoryEventBus()
    return a


def _run(agent, name, payload):
    return json.loads(asyncio.run(agent.run_tool(name, payload, PHONE)))


def test_consultar_stock_bajo_umbral(inventario):
    data = _run(inventario, "consultar_stock", {"producto": "trigo cristalino"})
    assert data["encontrado"] is True
    assert data["stock_ton"] == 200.0
    assert data["estado"] == "bajo_umbral"


def test_consultar_stock_normal(inventario):
    data = _run(inventario, "consultar_stock", {"producto": "maíz amarillo"})
    assert data["estado"] == "normal"


def test_consultar_stock_desconocido(inventario):
    data = _run(inventario, "consultar_stock", {"producto": "avena"})
    assert data["encontrado"] is False


def test_listar_alertas(inventario):
    data = _run(inventario, "listar_alertas_inventario", {})
    productos = {a["producto"] for a in data["alertas"]}
    assert productos == {"trigo cristalino", "soya"}


def test_resumen_inventario(inventario):
    data = _run(inventario, "resumen_inventario", {})
    assert data["total"] == 5


def test_transferir_a_ventas(inventario):
    data = _run(inventario, "transferir_a_ventas", {"motivo": "quiere precios"})
    assert data["transferido"] is True
    assert asyncio.run(inventario._bus.get_active_agent(PHONE)) == "ventas"


# --- Uso interno: cuánto hay no se le dice a cualquiera -------------------- #


@pytest.fixture
def _sin_lista_blanca(monkeypatch):
    """Por omisión la lista va vacía (desarrollo): nadie queda fuera."""
    monkeypatch.setattr(get_settings(), "inventario_phones_allowed", "")


def test_sin_lista_blanca_no_hay_restriccion(inventario, _sin_lista_blanca):
    data = _run(inventario, "resumen_inventario", {})
    assert data["total"] == 5


def test_un_numero_de_fuera_no_ve_las_existencias(inventario, monkeypatch):
    """El que sabe cuánto grano hay sabe cuánta prisa tenemos por vender."""
    monkeypatch.setattr(get_settings(), "inventario_phones_allowed", "5219999999999")
    for tool in ("consultar_stock", "listar_alertas_inventario", "resumen_inventario"):
        data = _run(inventario, tool, {"producto": "maíz amarillo"})
        assert data["autorizado"] is False, tool
        assert "stock_ton" not in data
        assert "productos" not in data
        assert "alertas" not in data


def test_un_telefono_del_equipo_si_las_ve(inventario, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "inventario_phones_allowed", f"5219999999999,{PHONE}"
    )
    data = _run(inventario, "resumen_inventario", {})
    assert data["total"] == 5


def test_pasarlo_a_ventas_no_pide_autorizacion(inventario, monkeypatch):
    """Es justo lo que queremos que pase con quien no está autorizado: que
    Ventas lo atienda, y Ventas solo habla de disponibilidad."""
    monkeypatch.setattr(get_settings(), "inventario_phones_allowed", "5219999999999")
    data = _run(inventario, "transferir_a_ventas", {"motivo": "quiere comprar"})
    assert data["transferido"] is True


def test_el_prompt_prohibe_dar_cifras_al_no_autorizado():
    from app.agents.inventario import SYSTEM_PROMPT

    assert "no estás autorizado" in SYSTEM_PROMPT
    assert "transferir_a_ventas" in SYSTEM_PROMPT
