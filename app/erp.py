"""Cliente para el sistema de órdenes (ERP / API externo).

Define una interfaz `ERPClient` con dos implementaciones:

- `HTTPERPClient`: consulta el ERP real vía HTTP.
- `MockERPClient`: datos de ejemplo en memoria para desarrollar sin ERP.

Contrato esperado (lo expone el ERP; ver `docs/erp/` para la implementación de
referencia en NestJS):

    GET {ERP_BASE_URL}/bot/ordenes/{folio}          -> Order (JSON) | 404
    GET {ERP_BASE_URL}/bot/ordenes?telefono={tel}   -> [Order, ...] (JSON)

donde `Order` es el modelo de `app/models.py`. En el ERP, la "orden del
cliente" corresponde a un Contrato (folio CONT-YYYY-NNNN); el endpoint adapta
Contrato/Embarque/Factura a este contrato.

Autenticación (configurable):
- Si `ERP_API_KEY_HEADER` está definido, la API key viaja en ese header.
- Si no, y hay `ERP_API_KEY`, se envía como `Authorization: Bearer <key>`.
"""

from __future__ import annotations

import abc
import time
import unicodedata

import httpx

from .config import get_settings
from .models import (
    InventoryItem,
    Order,
    OrderLine,
    Price,
    PurchaseOrder,
    PurchaseRequest,
    Quote,
    Supplier,
)


class ERPClient(abc.ABC):
    @abc.abstractmethod
    async def get_order(self, order_id: str) -> Order | None:
        """Devuelve una orden por su folio, o None si no existe."""

    @abc.abstractmethod
    async def list_orders_by_phone(self, phone: str) -> list[Order]:
        """Lista las órdenes asociadas a un número de teléfono."""

    @abc.abstractmethod
    async def get_price(self, producto: str) -> Price | None:
        """Devuelve el precio vigente de un producto, o None si no existe."""

    @abc.abstractmethod
    async def list_prices(self) -> list[Price]:
        """Lista los precios vigentes."""

    @abc.abstractmethod
    async def create_quote(
        self, producto: str, cantidad_ton: float, telefono: str
    ) -> Quote | None:
        """Crea una cotización. Devuelve None si el producto no tiene precio."""

    @abc.abstractmethod
    async def create_request(
        self, producto: str, cantidad_ton: float, telefono: str
    ) -> PurchaseRequest:
        """Registra una solicitud de pedido (estado 'pendiente')."""

    # --- Compras (órdenes de compra a proveedores) ------------------------- #
    @abc.abstractmethod
    async def get_purchase_order(self, folio: str) -> PurchaseOrder | None:
        """Devuelve una orden de compra por su folio, o None si no existe."""

    @abc.abstractmethod
    async def list_pending_purchase_orders(self) -> list[PurchaseOrder]:
        """Lista las órdenes de compra pendientes de aprobación."""

    @abc.abstractmethod
    async def create_purchase_order(
        self, proveedor: str, producto: str, cantidad_ton: float
    ) -> PurchaseOrder:
        """Crea una orden de compra (estado 'pendiente')."""

    @abc.abstractmethod
    async def approve_purchase_order(self, folio: str) -> PurchaseOrder | None:
        """Aprueba una orden de compra. Devuelve None si no existe."""

    @abc.abstractmethod
    async def list_suppliers(self) -> list[Supplier]:
        """Lista los proveedores registrados."""

    # --- Inventario -------------------------------------------------------- #
    @abc.abstractmethod
    async def get_inventory_item(self, producto: str) -> InventoryItem | None:
        """Devuelve la existencia de un producto, o None si no existe."""

    @abc.abstractmethod
    async def list_inventory(self) -> list[InventoryItem]:
        """Lista todas las existencias en inventario."""


class HTTPERPClient(ERPClient):
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_key_header: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_key_header = api_key_header
        self._transport = transport  # inyectable en pruebas

    def _auth_headers(self) -> dict[str, str]:
        if self._api_key_header and self._api_key:
            return {self._api_key_header: self._api_key}
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=15, headers=self._auth_headers(), transport=self._transport
        )

    async def get_order(self, order_id: str) -> Order | None:
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/bot/ordenes/{order_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return Order(**resp.json())

    async def list_orders_by_phone(self, phone: str) -> list[Order]:
        async with self._client() as client:
            resp = await client.get(
                f"{self._base_url}/bot/ordenes", params={"telefono": phone}
            )
            resp.raise_for_status()
            return [Order(**item) for item in resp.json()]

    async def get_price(self, producto: str) -> Price | None:
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/bot/precios/{producto}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return Price(**resp.json())

    async def list_prices(self) -> list[Price]:
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/bot/precios")
            resp.raise_for_status()
            return [Price(**item) for item in resp.json()]

    async def create_quote(
        self, producto: str, cantidad_ton: float, telefono: str
    ) -> Quote | None:
        async with self._client() as client:
            resp = await client.post(
                f"{self._base_url}/bot/cotizaciones",
                json={"producto": producto, "cantidad": cantidad_ton, "telefono": telefono},
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return Quote(**resp.json())

    async def create_request(
        self, producto: str, cantidad_ton: float, telefono: str
    ) -> PurchaseRequest:
        async with self._client() as client:
            resp = await client.post(
                f"{self._base_url}/bot/solicitudes",
                json={"producto": producto, "cantidad": cantidad_ton, "telefono": telefono},
            )
            resp.raise_for_status()
            return PurchaseRequest(**resp.json())

    async def get_purchase_order(self, folio: str) -> PurchaseOrder | None:
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/bot/oc/{folio}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return PurchaseOrder(**resp.json())

    async def list_pending_purchase_orders(self) -> list[PurchaseOrder]:
        async with self._client() as client:
            resp = await client.get(
                f"{self._base_url}/bot/oc", params={"estado": "pendiente"}
            )
            resp.raise_for_status()
            return [PurchaseOrder(**item) for item in resp.json()]

    async def create_purchase_order(
        self, proveedor: str, producto: str, cantidad_ton: float
    ) -> PurchaseOrder:
        async with self._client() as client:
            resp = await client.post(
                f"{self._base_url}/bot/oc",
                json={
                    "proveedor": proveedor,
                    "producto": producto,
                    "cantidad": cantidad_ton,
                },
            )
            resp.raise_for_status()
            return PurchaseOrder(**resp.json())

    async def approve_purchase_order(self, folio: str) -> PurchaseOrder | None:
        async with self._client() as client:
            resp = await client.patch(f"{self._base_url}/bot/oc/{folio}/aprobar")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return PurchaseOrder(**resp.json())

    async def list_suppliers(self) -> list[Supplier]:
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/bot/proveedores")
            resp.raise_for_status()
            return [Supplier(**item) for item in resp.json()]

    async def get_inventory_item(self, producto: str) -> InventoryItem | None:
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/bot/inventario/{producto}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return InventoryItem(**resp.json())

    async def list_inventory(self) -> list[InventoryItem]:
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/bot/inventario")
            resp.raise_for_status()
            return [InventoryItem(**item) for item in resp.json()]


# Precios simulados por tonelada (MXN). Las claves se comparan sin acentos.
_MOCK_PRECIOS = {
    "maiz amarillo": {"precio_ton": 5200.0, "disponible_ton": 1200.0},
    "maiz blanco": {"precio_ton": 5450.0, "disponible_ton": 800.0},
    "trigo": {"precio_ton": 7100.0, "disponible_ton": 600.0},
    "sorgo": {"precio_ton": 4800.0, "disponible_ton": 900.0},
    "soya": {"precio_ton": 11500.0, "disponible_ton": 400.0},
}
_MOCK_VIGENCIA = "fin del día hábil"

# Stock simulado por producto (toneladas) con su umbral mínimo y ubicación.
_MOCK_INVENTARIO = {
    "trigo cristalino": {"stock_ton": 200.0, "umbral_ton": 250.0, "ubicacion": "Silo Querétaro"},
    "soya": {"stock_ton": 150.0, "umbral_ton": 200.0, "ubicacion": "Silo Veracruz"},
    "maiz amarillo": {"stock_ton": 850.0, "umbral_ton": 300.0, "ubicacion": "Silo Bajío"},
    "maiz blanco": {"stock_ton": 520.0, "umbral_ton": 250.0, "ubicacion": "Silo Bajío"},
    "sorgo": {"stock_ton": 640.0, "umbral_ton": 200.0, "ubicacion": "Silo Sinaloa"},
}


def _inventory_item(nombre: str, data: dict) -> InventoryItem:
    estado = "bajo_umbral" if data["stock_ton"] < data["umbral_ton"] else "normal"
    return InventoryItem(
        producto=nombre,
        stock_ton=data["stock_ton"],
        umbral_ton=data["umbral_ton"],
        ubicacion=data["ubicacion"],
        estado=estado,
    )


def _normalize(text: str) -> str:
    """Minúsculas sin acentos, para emparejar nombres de producto."""
    text = text.strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _match_precio(producto: str) -> tuple[str, dict] | None:
    key = _normalize(producto)
    if key in _MOCK_PRECIOS:
        return key, _MOCK_PRECIOS[key]
    for nombre, data in _MOCK_PRECIOS.items():
        if key in nombre or nombre in key:
            return nombre, data
    return None


class MockERPClient(ERPClient):
    """Datos de ejemplo en memoria para desarrollo local, alineados al dominio
    del ERP (folios CONT-..., estados EstadoContrato/EstadoEmbarque)."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {
            "CONT-2026-0001": Order(
                id="CONT-2026-0001",
                cliente="Molinos del Bajío S.A.",
                telefono="5215512345678",
                estado="EN_PROCESO",
                estado_embarque="EN_TRANSITO",
                estado_factura="EMITIDA",
                total=185000.0,
                moneda="MXN",
                fecha="2026-05-20",
                fecha_entrega_estimada="2026-05-31",
                lineas=[OrderLine(producto="Maíz amarillo", cantidad=50, unidad="ton")],
                notas="Entrega en planta Querétaro, horario 8-14h.",
            ),
            "CONT-2026-0002": Order(
                id="CONT-2026-0002",
                cliente="Molinos del Bajío S.A.",
                telefono="5215512345678",
                estado="ACTIVO",
                total=92000.0,
                moneda="MXN",
                fecha="2026-05-27",
                fecha_entrega_estimada="2026-06-05",
                lineas=[OrderLine(producto="Trigo cristalino", cantidad=30, unidad="ton")],
            ),
        }
        self._purchase_orders: dict[str, PurchaseOrder] = {
            "OC-2026-0001": PurchaseOrder(
                id="OC-2026-0001",
                proveedor="Granos del Norte S.A.",
                producto="Maíz amarillo",
                cantidad=100.0,
                total=510000.0,
                moneda="MXN",
                estado="pendiente",
                fecha="2026-06-01",
                fecha_entrega_estimada="2026-06-15",
            ),
            "OC-2026-0002": PurchaseOrder(
                id="OC-2026-0002",
                proveedor="Agrícola del Pacífico",
                producto="Sorgo",
                cantidad=80.0,
                total=380000.0,
                moneda="MXN",
                estado="aprobada",
                fecha="2026-05-28",
                fecha_entrega_estimada="2026-06-10",
            ),
        }
        self._suppliers: list[Supplier] = [
            Supplier(
                id="PROV-001",
                nombre="Granos del Norte S.A.",
                productos=["Maíz amarillo", "Maíz blanco"],
                contacto="ventas@granosdelnorte.mx",
            ),
            Supplier(
                id="PROV-002",
                nombre="Agrícola del Pacífico",
                productos=["Sorgo", "Trigo"],
                contacto="contacto@agripacifico.mx",
            ),
        ]

    async def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id.strip().upper())

    async def list_orders_by_phone(self, phone: str) -> list[Order]:
        return [o for o in self._orders.values() if o.telefono == phone]

    async def get_price(self, producto: str) -> Price | None:
        match = _match_precio(producto)
        if match is None:
            return None
        nombre, data = match
        return Price(
            producto=nombre,
            precio_ton=data["precio_ton"],
            moneda="MXN",
            disponible_ton=data["disponible_ton"],
            vigencia=_MOCK_VIGENCIA,
        )

    async def list_prices(self) -> list[Price]:
        return [
            Price(
                producto=nombre,
                precio_ton=data["precio_ton"],
                moneda="MXN",
                disponible_ton=data["disponible_ton"],
                vigencia=_MOCK_VIGENCIA,
            )
            for nombre, data in _MOCK_PRECIOS.items()
        ]

    async def create_quote(
        self, producto: str, cantidad_ton: float, telefono: str
    ) -> Quote | None:
        match = _match_precio(producto)
        if match is None:
            return None
        nombre, data = match
        cantidad = float(cantidad_ton)
        return Quote(
            id=f"COT-{int(time.time())}",
            producto=nombre,
            cantidad=cantidad,
            total=round(data["precio_ton"] * cantidad, 2),
            moneda="MXN",
            vigencia=_MOCK_VIGENCIA,
            estado="borrador",
        )

    async def create_request(
        self, producto: str, cantidad_ton: float, telefono: str
    ) -> PurchaseRequest:
        return PurchaseRequest(
            id=f"SOL-{int(time.time())}",
            producto=producto,
            cantidad=float(cantidad_ton),
            telefono=telefono,
            estado="pendiente",
        )

    async def get_purchase_order(self, folio: str) -> PurchaseOrder | None:
        return self._purchase_orders.get(folio.strip().upper())

    async def list_pending_purchase_orders(self) -> list[PurchaseOrder]:
        return [
            oc for oc in self._purchase_orders.values() if oc.estado == "pendiente"
        ]

    async def create_purchase_order(
        self, proveedor: str, producto: str, cantidad_ton: float
    ) -> PurchaseOrder:
        folio = f"OC-{int(time.time())}"
        oc = PurchaseOrder(
            id=folio,
            proveedor=proveedor,
            producto=producto,
            cantidad=float(cantidad_ton),
            estado="pendiente",
        )
        self._purchase_orders[folio] = oc
        return oc

    async def approve_purchase_order(self, folio: str) -> PurchaseOrder | None:
        oc = self._purchase_orders.get(folio.strip().upper())
        if oc is None:
            return None
        oc.estado = "aprobada"
        return oc

    async def list_suppliers(self) -> list[Supplier]:
        return list(self._suppliers)

    async def get_inventory_item(self, producto: str) -> InventoryItem | None:
        key = _normalize(producto)
        if key in _MOCK_INVENTARIO:
            return _inventory_item(key, _MOCK_INVENTARIO[key])
        for nombre, data in _MOCK_INVENTARIO.items():
            if key in nombre or nombre in key:
                return _inventory_item(nombre, data)
        return None

    async def list_inventory(self) -> list[InventoryItem]:
        return [_inventory_item(n, d) for n, d in _MOCK_INVENTARIO.items()]


def get_erp_client() -> ERPClient:
    settings = get_settings()
    if settings.use_mock_erp:
        return MockERPClient()
    return HTTPERPClient(
        settings.erp_base_url,
        api_key=settings.erp_api_key,
        api_key_header=settings.erp_api_key_header,
    )
