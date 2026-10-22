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


def test_crear_y_aprobar_oc_no_se_ofrecen():
    """Crear o confirmar una orden es del ERP, donde responde una persona."""
    nombres = {t["name"] for t in ComprasAgent.tools(ComprasAgent.__new__(ComprasAgent))}
    assert "crear_oc" not in nombres
    assert "aprobar_oc" not in nombres
    assert nombres >= {"consultar_oc", "listar_oc_pendientes", "listar_proveedores"}


def test_el_prompt_manda_a_crear_la_oc_al_erp():
    from app.agents.compras import SYSTEM_PROMPT

    assert "Compras › Órdenes de compra" in SYSTEM_PROMPT
    assert "Nunca digas que la creaste o la aprobaste" in SYSTEM_PROMPT


def test_una_tool_retirada_no_hace_nada(compras):
    data = _run(compras, "crear_oc", {"proveedor": "X", "producto": "trigo", "cantidad_ton": 50})
    assert "error" in data
    assert asyncio.run(compras._erp.get_purchase_order("OC-2026-0001")).estado == "pendiente"


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
