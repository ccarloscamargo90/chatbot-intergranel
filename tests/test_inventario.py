"""Pruebas del agente de Inventario (tools contra mock y bus en memoria)."""

import asyncio
import json

import pytest

from app.agents.inventario import InventarioAgent
from app.bus import InMemoryEventBus

PHONE = "5215512345678"


@pytest.fixture
def inventario() -> InventarioAgent:
    a = InventarioAgent.__new__(InventarioAgent)
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
