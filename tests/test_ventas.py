"""Pruebas del agente de Ventas (tools contra mock y bus en memoria)."""

import asyncio
import json

import pytest

from app.agents.ventas import VentasAgent
from app.bus import InMemoryEventBus
from app.erp import MockERPClient

PHONE = "5215512345678"


@pytest.fixture
def ventas() -> VentasAgent:
    a = VentasAgent.__new__(VentasAgent)
    a._erp = MockERPClient()
    a._bus = InMemoryEventBus()
    return a


def _run(agent, name, payload):
    return json.loads(asyncio.run(agent.run_tool(name, payload, PHONE)))


def test_consultar_precio_maiz_amarillo(ventas):
    data = _run(ventas, "consultar_precio", {"producto": "maíz amarillo"})
    assert data["encontrado"] is True
    assert data["precio_ton"] == 5200.0
    assert data["moneda"] == "MXN"


def test_consultar_precio_desconocido(ventas):
    data = _run(ventas, "consultar_precio", {"producto": "café"})
    assert data["encontrado"] is False


def test_generar_cotizacion_calcula_total_y_publica(ventas):
    data = _run(ventas, "generar_cotizacion", {"producto": "trigo", "cantidad_ton": 10})
    assert data["total"] == 71000.0
    assert data["estado"] == "borrador"
    # Se publicó en el bus.
    evento = asyncio.run(ventas._bus.read(f"bus:ventas:cotizacion:{PHONE}"))
    assert evento["total"] == 71000.0


def test_consultar_contrato_existente(ventas):
    data = _run(ventas, "consultar_contrato", {"folio": "CONT-2026-0001"})
    assert data["encontrado"] is True
    assert data["contrato"]["id"] == "CONT-2026-0001"


def test_listar_contratos_cliente(ventas):
    data = _run(ventas, "listar_contratos_cliente", {})
    assert data["total"] == 2


def test_solicitar_pedido_publica_en_bus(ventas):
    data = _run(ventas, "solicitar_pedido", {"producto": "soya", "cantidad_ton": 5})
    assert data["estado"] == "pendiente"
    evento = asyncio.run(ventas._bus.read(f"bus:ventas:solicitud:{PHONE}"))
    assert evento["producto"] == "soya"


def test_transferir_a_soporte_cambia_agente_activo(ventas):
    data = _run(ventas, "transferir_a_soporte", {"motivo": "reclamo"})
    assert data["transferido"] is True
    assert asyncio.run(ventas._bus.get_active_agent(PHONE)) == "soporte"
