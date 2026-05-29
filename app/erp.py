"""Cliente para el sistema de órdenes (ERP / API externo).

Define una interfaz `ERPClient` con dos implementaciones:

- `HTTPERPClient`: consulta un ERP real vía HTTP (configurado con ERP_BASE_URL).
- `MockERPClient`: datos de ejemplo en memoria para desarrollar sin ERP.

Para conectar tu ERP real, ajusta las rutas/campos en `HTTPERPClient` para
que coincidan con tu API, o adapta el parseo en `_to_order`.
"""

from __future__ import annotations

import abc

import httpx

from .config import get_settings
from .models import Order, OrderLine


class ERPClient(abc.ABC):
    @abc.abstractmethod
    async def get_order(self, order_id: str) -> Order | None:
        """Devuelve una orden por su ID, o None si no existe."""

    @abc.abstractmethod
    async def list_orders_by_phone(self, phone: str) -> list[Order]:
        """Lista las órdenes asociadas a un número de teléfono."""


class HTTPERPClient(ERPClient):
    """Consulta un ERP/API externo. Asume un contrato REST sencillo:

        GET {base}/orders/{id}            -> objeto Order (JSON)
        GET {base}/orders?telefono={tel}  -> lista de Order (JSON)

    Ajusta estas rutas si tu ERP usa otra forma."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def get_order(self, order_id: str) -> Order | None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._base_url}/orders/{order_id}", headers=self._headers
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return Order(**resp.json())

    async def list_orders_by_phone(self, phone: str) -> list[Order]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._base_url}/orders",
                params={"telefono": phone},
                headers=self._headers,
            )
            resp.raise_for_status()
            return [Order(**item) for item in resp.json()]


class MockERPClient(ERPClient):
    """Datos de ejemplo en memoria para desarrollo local."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {
            "OC-1001": Order(
                id="OC-1001",
                cliente="Molinos del Bajío S.A.",
                telefono="5215512345678",
                estado="en_ruta",
                total=185000.0,
                moneda="MXN",
                fecha="2026-05-20",
                fecha_entrega_estimada="2026-05-31",
                lineas=[
                    OrderLine(producto="Maíz amarillo", cantidad=50, unidad="ton"),
                    OrderLine(producto="Sorgo", cantidad=20, unidad="ton"),
                ],
                notas="Entrega en planta Querétaro, horario 8-14h.",
            ),
            "OC-1002": Order(
                id="OC-1002",
                cliente="Molinos del Bajío S.A.",
                telefono="5215512345678",
                estado="en_proceso",
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
    return HTTPERPClient(settings.erp_base_url, settings.erp_api_key)
