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

import httpx

from .config import get_settings
from .models import Order, OrderLine


class ERPClient(abc.ABC):
    @abc.abstractmethod
    async def get_order(self, order_id: str) -> Order | None:
        """Devuelve una orden por su folio, o None si no existe."""

    @abc.abstractmethod
    async def list_orders_by_phone(self, phone: str) -> list[Order]:
        """Lista las órdenes asociadas a un número de teléfono."""


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

    async def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id.strip().upper())

    async def list_orders_by_phone(self, phone: str) -> list[Order]:
        return [o for o in self._orders.values() if o.telefono == phone]


def get_erp_client() -> ERPClient:
    settings = get_settings()
    if settings.use_mock_erp:
        return MockERPClient()
    return HTTPERPClient(
        settings.erp_base_url,
        api_key=settings.erp_api_key,
        api_key_header=settings.erp_api_key_header,
    )
