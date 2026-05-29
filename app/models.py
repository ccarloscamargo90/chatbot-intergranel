"""Modelos de datos compartidos."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OrderLine(BaseModel):
    producto: str
    cantidad: float
    unidad: str = "ton"


class Order(BaseModel):
    """Representación de una orden de compra tal como la expone el ERP."""

    id: str
    cliente: str
    telefono: str | None = None
    estado: str
    total: float | None = None
    moneda: str = "MXN"
    fecha: str | None = None
    fecha_entrega_estimada: str | None = None
    lineas: list[OrderLine] = Field(default_factory=list)
    notas: str | None = None


class OrderEvent(BaseModel):
    """Payload que el ERP envía a /webhooks/erp/order-update para disparar
    una notificación proactiva al cliente por WhatsApp."""

    order_id: str
    telefono: str  # destino en formato E.164 sin '+', p.ej. 5215512345678
    estado_nuevo: str
    cliente: str | None = None
    # Texto opcional para sobrescribir el mensaje generado automáticamente.
    mensaje: str | None = None
