"""Modelos de datos compartidos."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class OrderLine(BaseModel):
    producto: str
    cantidad: float
    unidad: str = "ton"


class Order(BaseModel):
    """Representación de una orden de compra tal como la expone el ERP."""

    id: str
    cliente: str
    telefono: Optional[str] = None
    estado: str
    total: Optional[float] = None
    moneda: str = "MXN"
    fecha: Optional[str] = None
    fecha_entrega_estimada: Optional[str] = None
    lineas: list[OrderLine] = Field(default_factory=list)
    notas: Optional[str] = None


class OrderEvent(BaseModel):
    """Payload que el ERP envía a /webhooks/erp/order-update para disparar
    una notificación proactiva al cliente por WhatsApp."""

    order_id: str
    telefono: str  # destino en formato E.164 sin '+', p.ej. 5215512345678
    estado_nuevo: str
    cliente: Optional[str] = None
    # Texto opcional para sobrescribir el mensaje generado automáticamente.
    mensaje: Optional[str] = None
