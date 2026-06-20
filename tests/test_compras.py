"""Pruebas del agente de Compras (tools contra mock y lista blanca)."""

import asyncio
import json

import pytest

from app.agents.compras import ComprasAgent
from app.bus import InMemoryEventBus
from app.config import get_settings
from app.erp import MockERPClient

PHONE = "5215512345678"


@pytest.fixture
def compras() -> ComprasAgent:
    a = ComprasAgent.__new__(ComprasAgent)
    a._erp = MockERPClient()
    a._bus = InMemoryEventBus()
    return a


@pytest.fixture(autouse=True)
def _sin_lista_blanca(monkeypatch):
    """Por defecto, sin restricción (lista blanca vacía)."""
    monkeypatch.setattr(get_settings(), "compras_phones_allowed", "")


def _run(agent, name, payload):
    return json.loads(asyncio.run(agent.run_tool(name, payload, PHONE)))


def test_consultar_oc_existente(compras):
    data = _run(compras, "consultar_oc", {"folio": "OC-2026-0001"})
    assert data["encontrada"] is True
    assert data["oc"]["estado"] == "pendiente"


def test_consultar_oc_inexistente(compras):
    data = _run(compras, "consultar_oc", {"folio": "OC-9999"})
    assert data["encontrada"] is False


def test_listar_oc_pendientes(compras):
    data = _run(compras, "listar_oc_pendientes", {})
    assert data["total"] == 1
    assert data["ocs"][0]["id"] == "OC-2026-0001"


def test_crear_oc(compras):
    data = _run(
        compras,
        "crear_oc",
        {"proveedor": "X", "producto": "trigo", "cantidad_ton": 50},
    )
    assert data["creada"] is True
    assert data["oc"]["estado"] == "pendiente"


def test_aprobar_oc(compras):
    data = _run(compras, "aprobar_oc", {"folio": "OC-2026-0001"})
    assert data["aprobada"] is True
    assert data["oc"]["estado"] == "aprobada"


def test_aprobar_oc_inexistente(compras):
    data = _run(compras, "aprobar_oc", {"folio": "OC-9999"})
    assert data["encontrada"] is False


def test_listar_proveedores(compras):
    data = _run(compras, "listar_proveedores", {})
    assert data["total"] == 2


def test_transferir_a_ventas_funciona(compras):
    data = _run(compras, "transferir_a_ventas", {"motivo": "precios"})
    assert data["transferido"] is True
    assert asyncio.run(compras._bus.get_active_agent(PHONE)) == "ventas"


# ------------------------------ Lista blanca ----------------------------- #
def test_no_autorizado_si_fuera_de_lista(compras, monkeypatch):
    monkeypatch.setattr(get_settings(), "compras_phones_allowed", "5219999999999")
    data = _run(compras, "listar_oc_pendientes", {})
    assert data["autorizado"] is False


def test_autorizado_si_en_lista(compras, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "compras_phones_allowed", f"5219999999999,{PHONE}"
    )
    data = _run(compras, "listar_oc_pendientes", {})
    assert data["total"] == 1


def test_transferir_no_requiere_autorizacion(compras, monkeypatch):
    monkeypatch.setattr(get_settings(), "compras_phones_allowed", "5219999999999")
    data = _run(compras, "transferir_a_ventas", {"motivo": "x"})
    assert data["transferido"] is True
