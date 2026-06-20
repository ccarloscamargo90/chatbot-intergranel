"""Pruebas del ERP: mock en memoria y cliente HTTP."""

import asyncio
import json

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


def test_get_price_mock():
    erp = MockERPClient()
    price = asyncio.run(erp.get_price("maíz amarillo"))
    assert price is not None
    assert price.precio_ton == 5200.0
    assert price.moneda == "MXN"


def test_get_price_mock_inexistente():
    erp = MockERPClient()
    assert asyncio.run(erp.get_price("café")) is None


def test_list_prices_mock():
    erp = MockERPClient()
    prices = asyncio.run(erp.list_prices())
    assert len(prices) == 5


def test_create_quote_mock_calcula_total():
    erp = MockERPClient()
    quote = asyncio.run(erp.create_quote("trigo", 10, "5215512345678"))
    assert quote is not None
    assert quote.total == 71000.0
    assert quote.estado == "borrador"


def test_create_quote_mock_producto_desconocido():
    erp = MockERPClient()
    assert asyncio.run(erp.create_quote("café", 10, "5215512345678")) is None


def test_create_request_mock():
    erp = MockERPClient()
    req = asyncio.run(erp.create_request("soya", 5, "5215512345678"))
    assert req.estado == "pendiente"
    assert req.producto == "soya"


def test_get_purchase_order_mock():
    erp = MockERPClient()
    oc = asyncio.run(erp.get_purchase_order("oc-2026-0001"))
    assert oc is not None
    assert oc.estado == "pendiente"


def test_list_pending_purchase_orders_mock():
    erp = MockERPClient()
    ocs = asyncio.run(erp.list_pending_purchase_orders())
    assert [o.id for o in ocs] == ["OC-2026-0001"]


def test_create_purchase_order_mock():
    erp = MockERPClient()
    oc = asyncio.run(erp.create_purchase_order("Proveedor X", "trigo", 50))
    assert oc.estado == "pendiente"
    # Queda registrada y consultable.
    assert asyncio.run(erp.get_purchase_order(oc.id)) is not None


def test_approve_purchase_order_mock():
    erp = MockERPClient()
    oc = asyncio.run(erp.approve_purchase_order("OC-2026-0001"))
    assert oc is not None
    assert oc.estado == "aprobada"


def test_approve_purchase_order_mock_inexistente():
    erp = MockERPClient()
    assert asyncio.run(erp.approve_purchase_order("OC-9999")) is None


def test_list_suppliers_mock():
    erp = MockERPClient()
    proveedores = asyncio.run(erp.list_suppliers())
    assert len(proveedores) == 2


def test_get_inventory_item_mock_bajo_umbral():
    erp = MockERPClient()
    item = asyncio.run(erp.get_inventory_item("trigo cristalino"))
    assert item is not None
    assert item.estado == "bajo_umbral"


def test_get_inventory_item_mock_normal():
    erp = MockERPClient()
    item = asyncio.run(erp.get_inventory_item("maíz amarillo"))
    assert item is not None
    assert item.estado == "normal"


def test_get_inventory_item_mock_inexistente():
    erp = MockERPClient()
    assert asyncio.run(erp.get_inventory_item("avena")) is None


def test_list_inventory_mock():
    erp = MockERPClient()
    items = asyncio.run(erp.list_inventory())
    assert len(items) == 5
    bajo = [i.producto for i in items if i.estado == "bajo_umbral"]
    assert set(bajo) == {"trigo cristalino", "soya"}


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


def test_get_price_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/precios/trigo"
        return httpx.Response(
            200,
            json={"producto": "trigo", "precio_ton": 7100.0, "disponible_ton": 600.0},
        )

    price = asyncio.run(_make_client(handler).get_price("trigo"))
    assert price is not None
    assert price.precio_ton == 7100.0


def test_get_price_http_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "no existe"})

    assert asyncio.run(_make_client(handler).get_price("café")) is None


def test_list_prices_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/precios"
        return httpx.Response(200, json=[{"producto": "trigo", "precio_ton": 7100.0}])

    prices = asyncio.run(_make_client(handler).list_prices())
    assert [p.producto for p in prices] == ["trigo"]


def test_create_quote_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/bot/cotizaciones"
        body = json.loads(request.content)
        assert body == {"producto": "trigo", "cantidad": 10.0, "telefono": "521"}
        return httpx.Response(
            200,
            json={
                "id": "COT-1",
                "producto": "trigo",
                "cantidad": 10.0,
                "total": 71000.0,
                "estado": "borrador",
            },
        )

    quote = asyncio.run(_make_client(handler).create_quote("trigo", 10.0, "521"))
    assert quote is not None
    assert quote.total == 71000.0


def test_create_request_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/bot/solicitudes"
        return httpx.Response(
            200,
            json={"id": "SOL-1", "producto": "soya", "cantidad": 5.0, "estado": "pendiente"},
        )

    req = asyncio.run(_make_client(handler).create_request("soya", 5.0, "521"))
    assert req.id == "SOL-1"
    assert req.estado == "pendiente"


def test_get_purchase_order_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/oc/OC-2026-0001"
        return httpx.Response(
            200,
            json={
                "id": "OC-2026-0001",
                "proveedor": "X",
                "producto": "maíz",
                "cantidad": 100.0,
                "estado": "pendiente",
            },
        )

    oc = asyncio.run(_make_client(handler).get_purchase_order("OC-2026-0001"))
    assert oc is not None
    assert oc.estado == "pendiente"


def test_list_pending_purchase_orders_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/oc"
        assert request.url.params.get("estado") == "pendiente"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "OC-1",
                    "proveedor": "X",
                    "producto": "trigo",
                    "cantidad": 10.0,
                    "estado": "pendiente",
                }
            ],
        )

    ocs = asyncio.run(_make_client(handler).list_pending_purchase_orders())
    assert [o.id for o in ocs] == ["OC-1"]


def test_create_purchase_order_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/bot/oc"
        body = json.loads(request.content)
        assert body == {"proveedor": "X", "producto": "trigo", "cantidad": 50.0}
        return httpx.Response(
            200,
            json={
                "id": "OC-2",
                "proveedor": "X",
                "producto": "trigo",
                "cantidad": 50.0,
                "estado": "pendiente",
            },
        )

    oc = asyncio.run(_make_client(handler).create_purchase_order("X", "trigo", 50.0))
    assert oc.id == "OC-2"


def test_approve_purchase_order_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/v1/bot/oc/OC-1/aprobar"
        return httpx.Response(
            200,
            json={
                "id": "OC-1",
                "proveedor": "X",
                "producto": "trigo",
                "cantidad": 10.0,
                "estado": "aprobada",
            },
        )

    oc = asyncio.run(_make_client(handler).approve_purchase_order("OC-1"))
    assert oc is not None
    assert oc.estado == "aprobada"


def test_list_suppliers_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/proveedores"
        return httpx.Response(
            200, json=[{"id": "PROV-1", "nombre": "Granos del Norte"}]
        )

    proveedores = asyncio.run(_make_client(handler).list_suppliers())
    assert proveedores[0].nombre == "Granos del Norte"


def test_get_inventory_item_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/inventario/trigo"
        return httpx.Response(
            200,
            json={
                "producto": "trigo",
                "stock_ton": 100.0,
                "umbral_ton": 250.0,
                "estado": "bajo_umbral",
            },
        )

    item = asyncio.run(_make_client(handler).get_inventory_item("trigo"))
    assert item is not None
    assert item.estado == "bajo_umbral"


def test_get_inventory_item_http_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "no existe"})

    assert asyncio.run(_make_client(handler).get_inventory_item("avena")) is None


def test_list_inventory_http_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/inventario"
        return httpx.Response(
            200,
            json=[
                {"producto": "trigo", "stock_ton": 100.0, "umbral_ton": 250.0},
                {"producto": "soya", "stock_ton": 300.0, "umbral_ton": 200.0},
            ],
        )

    items = asyncio.run(_make_client(handler).list_inventory())
    assert [i.producto for i in items] == ["trigo", "soya"]
