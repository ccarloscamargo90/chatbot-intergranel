"""Pruebas del agente de Soporte (tools contra el ERP simulado)."""

import asyncio
import json

import pytest

from app.agents.soporte import SoporteAgent
from app.erp import MockERPClient

PHONE = "5215512345678"


@pytest.fixture
def soporte() -> SoporteAgent:
    a = SoporteAgent.__new__(SoporteAgent)
    a._erp = MockERPClient()
    return a


def _run(agent, name, payload):
    return json.loads(asyncio.run(agent.run_tool(name, payload, PHONE)))


def test_consultar_orden_existente(soporte):
    data = _run(soporte, "consultar_orden", {"order_id": "CONT-2026-0001"})
    assert data["encontrada"] is True
    assert data["orden"]["id"] == "CONT-2026-0001"


def test_consultar_orden_inexistente(soporte):
    data = _run(soporte, "consultar_orden", {"order_id": "CONT-9999"})
    assert data["encontrada"] is False


def test_listar_usa_telefono_del_remitente(soporte):
    data = _run(soporte, "listar_ordenes_cliente", {})
    assert data["telefono"] == PHONE
    assert data["total"] == 2


def test_escalar_a_humano(soporte):
    data = _run(soporte, "escalar_a_humano", {"motivo": "reclamo"})
    assert data["escalado"] is True
