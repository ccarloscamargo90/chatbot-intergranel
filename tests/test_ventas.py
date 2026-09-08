"""Pruebas del agente de Ventas.

Los precios y las cotizaciones vienen del CRM simulado; los contratos y las
solicitudes, del ERP simulado. Ninguna toca la red (regla 6).
"""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.agents.ventas import VentasAgent, folio_cotizacion
from app.bus import InMemoryEventBus
from app.crm import CRMNoDisponible, MockCRMClient
from app.erp import MockERPClient

PHONE = "5215512345678"


@pytest.fixture
def ventas() -> VentasAgent:
    a = VentasAgent.__new__(VentasAgent)
    a._erp = MockERPClient()
    a._crm = MockCRMClient()
    a._bus = InMemoryEventBus()
    return a


class CRMCaido(MockCRMClient):
    """Un CRM que no contesta, para ver qué dice el bot cuando no puede consultar."""

    async def catalogo(self):
        raise CRMNoDisponible("CRM: connection refused")

    async def registrar_cotizacion(self, **kwargs):
        raise CRMNoDisponible("CRM: connection refused")


def _run(agent, name, payload):
    return json.loads(asyncio.run(agent.run_tool(name, payload, PHONE)))


# --- Precio ---------------------------------------------------------------- #


def test_consultar_precio_lee_del_crm(ventas):
    data = _run(ventas, "consultar_precio", {"producto": "maíz blanco"})
    assert data["disponible"] is True
    assert data["precio_ton"] == 6169.56
    assert data["moneda"] == "MXN"
    # De cuándo es el dato: sin esto el bot no puede decir "precio de hoy".
    assert data["actualizado_el"]


def test_consultar_precio_tolera_acentos_y_mayusculas(ventas):
    sin_acento = _run(ventas, "consultar_precio", {"producto": "MAIZ BLANCO"})
    assert sin_acento["disponible"] is True
    assert sin_acento["producto"] == "Maíz blanco"


def test_consultar_precio_no_confunde_blanco_con_amarillo(ventas):
    data = _run(ventas, "consultar_precio", {"producto": "maíz amarillo"})
    assert data["producto"] == "Maíz amarillo"
    assert data["precio_ton"] == 5890.00


def test_producto_que_no_vendemos_dice_que_no_esta_en_catalogo(ventas):
    data = _run(ventas, "consultar_precio", {"producto": "café"})
    assert data["disponible"] is False
    assert data["motivo"] == "no_esta_en_catalogo"


def test_producto_sin_precio_no_se_confunde_con_uno_que_no_vendemos(ventas):
    """El motivo tiene que distinguirlos: si no, el bot le dice a un cliente
    que no manejamos algo que sí manejamos."""
    data = _run(ventas, "consultar_precio", {"producto": "sorgo dulce"})
    assert data["disponible"] is False
    assert data["motivo"] == "sin_precio_publicado"
    assert data["producto"] == "Sorgo dulce"


def test_espejo_desactualizado_no_da_ninguna_cifra(ventas):
    ventas._crm.desactualizado = True
    data = _run(ventas, "consultar_precio", {"producto": "maíz blanco"})
    assert data["disponible"] is False
    assert data["motivo"] == "datos_no_confiables"
    assert "precio_ton" not in data


def test_crm_caido_no_cae_al_erp(ventas):
    """La regla de dirección no tiene atajos: sin CRM no hay precio, aunque el
    ERP esté ahí. Un fallback al ERP el día que el CRM falla es el día en que
    dos sistemas dicen precios distintos."""
    ventas._crm = CRMCaido()
    data = _run(ventas, "consultar_precio", {"producto": "maíz blanco"})
    assert data["disponible"] is False
    assert data["motivo"] == "crm_no_disponible"
    assert "precio_ton" not in data


# --- Catálogo -------------------------------------------------------------- #


def test_listar_productos_devuelve_el_catalogo_del_crm(ventas):
    data = _run(ventas, "listar_productos", {})
    assert data["disponible"] is True
    nombres = [p["producto"] for p in data["productos"]]
    assert "Maíz blanco" in nombres
    # El que no tiene precio también sale, con precio en null.
    sorgo = next(p for p in data["productos"] if p["producto"] == "Sorgo dulce")
    assert sorgo["precio_ton"] is None


def test_listar_productos_calla_si_el_espejo_esta_viejo(ventas):
    ventas._crm.desactualizado = True
    data = _run(ventas, "listar_productos", {})
    assert data["disponible"] is False
    assert data["motivo"] == "datos_no_confiables"


# --- Cotización ------------------------------------------------------------ #


def test_generar_cotizacion_registra_en_el_crm_y_publica_en_el_bus(ventas):
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "maíz blanco", "cantidad_ton": 10, "nombre_cliente": "Molinos del Bajío"},
    )
    assert data["disponible"] is True
    assert data["total"] == 61695.60
    assert data["precio_ton"] == 6169.56
    # Quedó en el CRM, que es donde un vendedor la va a trabajar.
    assert len(ventas._crm.cotizaciones) == 1
    assert ventas._crm.cotizaciones[0].folio == data["folio"]
    # Y en el bus, para el resto de los agentes.
    evento = asyncio.run(ventas._bus.read(f"bus:ventas:cotizacion:{PHONE}"))
    assert evento["total"] == 61695.60


def test_cotizacion_sin_nombre_no_se_registra(ventas):
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "maíz blanco", "cantidad_ton": 10, "nombre_cliente": "   "},
    )
    assert data["disponible"] is False
    assert data["motivo"] == "falta_nombre_cliente"
    assert ventas._crm.cotizaciones == []


def test_no_se_cotiza_un_producto_sin_precio(ventas):
    """Rechaza por el MISMO motivo que consultar_precio: si no, el bot cotizaría
    lo que acaba de decir que no puede cotizar."""
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "sorgo dulce", "cantidad_ton": 10, "nombre_cliente": "Molinos"},
    )
    assert data["disponible"] is False
    assert data["motivo"] == "sin_precio_publicado"
    assert ventas._crm.cotizaciones == []


def test_no_se_cotiza_con_el_espejo_desactualizado(ventas):
    ventas._crm.desactualizado = True
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "maíz blanco", "cantidad_ton": 10, "nombre_cliente": "Molinos"},
    )
    assert data["disponible"] is False
    assert data["motivo"] == "datos_no_confiables"
    assert ventas._crm.cotizaciones == []


def test_cotizar_con_el_crm_caido_no_promete_nada(ventas):
    ventas._crm = CRMCaido()
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "maíz blanco", "cantidad_ton": 10, "nombre_cliente": "Molinos"},
    )
    assert data["disponible"] is False
    assert data["motivo"] == "crm_no_disponible"
    assert "total" not in data


def test_folio_lleva_segundos_para_no_pisar_otra_cotizacion():
    """El CRM deduplica por folio: dos cotizaciones del mismo cliente en el
    mismo minuto tienen que ser dos, no una encima de la otra."""
    a = folio_cotizacion(PHONE, datetime(2026, 9, 8, 6, 45, 12, tzinfo=UTC))
    b = folio_cotizacion(PHONE, datetime(2026, 9, 8, 6, 45, 49, tzinfo=UTC))
    assert a == "COT-20260908-064512-5678"
    assert a != b


# --- Lo que sigue siendo del ERP ------------------------------------------- #


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


# --- El prompt ------------------------------------------------------------- #


def test_el_prompt_prohibe_mezclar_los_motivos():
    from app.agents.ventas import SYSTEM_PROMPT

    for motivo in (
        "no_esta_en_catalogo",
        "sin_precio_publicado",
        "datos_no_confiables",
        "crm_no_disponible",
    ):
        assert motivo in SYSTEM_PROMPT, f"el prompt no explica el motivo {motivo}"
    assert "PROHIBIDO" in SYSTEM_PROMPT
