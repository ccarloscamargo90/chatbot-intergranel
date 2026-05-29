"""Pruebas del ERP simulado."""

import asyncio

from app.erp import MockERPClient


def test_get_order_case_insensitive():
    erp = MockERPClient()
    order = asyncio.run(erp.get_order("oc-1001"))
    assert order is not None
    assert order.id == "OC-1001"
    assert order.estado == "en_ruta"


def test_get_order_inexistente():
    erp = MockERPClient()
    assert asyncio.run(erp.get_order("OC-0000")) is None


def test_list_orders_by_phone():
    erp = MockERPClient()
    orders = asyncio.run(erp.list_orders_by_phone("5215512345678"))
    assert len(orders) == 2
