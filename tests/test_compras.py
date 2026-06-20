"""Pruebas del agente de Compras (stub de la Fase 1)."""

import asyncio
import json

import pytest

from app.agents.compras import ComprasAgent
from app.bus import InMemoryEventBus

PHONE = "5215512345678"


@pytest.fixture
def compras() -> ComprasAgent:
    a = ComprasAgent.__new__(ComprasAgent)
    a._bus = InMemoryEventBus()
    return a


def _run(agent, name, payload):
    return json.loads(asyncio.run(agent.run_tool(name, payload, PHONE)))


@pytest.mark.parametrize(
    "tool,payload",
    [
        ("consultar_oc", {"folio": "OC-1"}),
        ("listar_oc_pendientes", {}),
        ("crear_oc", {"proveedor": "X", "producto": "trigo", "cantidad_ton": 10}),
        ("aprobar_oc", {"folio": "OC-1"}),
        ("listar_proveedores", {}),
    ],
)
def test_tools_stub_proximamente(compras, tool, payload):
    data = _run(compras, tool, payload)
    assert data["disponible"] is False
    assert "próximamente" in data["mensaje"]


def test_transferir_a_ventas_funciona(compras):
    data = _run(compras, "transferir_a_ventas", {"motivo": "precios"})
    assert data["transferido"] is True
    assert asyncio.run(compras._bus.get_active_agent(PHONE)) == "ventas"
