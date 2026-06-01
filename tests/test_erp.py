"""Pruebas del ERP: mock en memoria y cliente HTTP."""

import asyncio

import httpx

from app.erp import HTTPERPClient, MockERPClient


# ----------------------------- MockERPClient ----------------------------- #
def test_get_order_case_insensitive():
    erp = MockERPClient()
    order = asyncio.run(erp.get_order("cont-2026-0001"))
    assert order is not None
    assert order.id == "CONT-2026-0001"
    assert order.estado == "EN_PROCESO"
    assert order.estado_embarque == "EN_TRANSITO"


def test_get_order_inexistente():
    erp = MockERPClient()
    assert asyncio.run(erp.get_order("CONT-0000")) is None


def test_list_orders_by_phone():
    erp = MockERPClient()
    orders = asyncio.run(erp.list_orders_by_phone("5215512345678"))
    assert len(orders) == 2


# ----------------------------- HTTPERPClient ----------------------------- #
def test_auth_headers_api_key_header():
    c = HTTPERPClient("https://erp/api/v1", api_key="secret", api_key_header="X-Bot-Api-Key")
    assert c._auth_headers() == {"X-Bot-Api-Key": "secret"}


def test_auth_headers_bearer_por_defecto():
    c = HTTPERPClient("https://erp/api/v1", api_key="secret")
    assert c._auth_headers() == {"Authorization": "Bearer secret"}


def test_auth_headers_sin_credenciales():
    assert HTTPERPClient("https://erp/api/v1")._auth_headers() == {}


def _make_client(handler) -> HTTPERPClient:
    return HTTPERPClient(
        "https://erp.example.com/api/v1",
        api_key="k",
        api_key_header="X-Bot-Api-Key",
        transport=httpx.MockTransport(handler),
    )


def test_get_order_http_ok():
    sample = {
        "id": "CONT-2026-0001",
        "cliente": "Molinos del Bajío S.A.",
        "telefono": "5215512345678",
        "estado": "EN_PROCESO",
        "total": 185000.0,
        "lineas": [{"producto": "Maíz amarillo", "cantidad": 50, "unidad": "ton"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/ordenes/CONT-2026-0001"
        assert request.headers["X-Bot-Api-Key"] == "k"
        return httpx.Response(200, json=sample)

    order = asyncio.run(_make_client(handler).get_order("CONT-2026-0001"))
    assert order is not None
    assert order.id == "CONT-2026-0001"
    assert order.lineas[0].producto == "Maíz amarillo"


def test_get_order_http_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "no existe"})

    assert asyncio.run(_make_client(handler).get_order("CONT-9999")) is None


def test_list_orders_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/ordenes"
        assert request.url.params.get("telefono") == "5215512345678"
        body = [
            {"id": "CONT-2026-0001", "cliente": "X", "estado": "ACTIVO"},
            {"id": "CONT-2026-0002", "cliente": "X", "estado": "EN_PROCESO"},
        ]
        return httpx.Response(200, json=body)

    orders = asyncio.run(_make_client(handler).list_orders_by_phone("5215512345678"))
    assert [o.id for o in orders] == ["CONT-2026-0001", "CONT-2026-0002"]
