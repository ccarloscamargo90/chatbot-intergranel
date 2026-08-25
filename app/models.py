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
    estado: str  # estado del contrato (EstadoContrato del ERP)
    estado_embarque: str | None = None  # estado del último embarque, si existe
    estado_factura: str | None = None  # estado de facturación, si existe
    total: float | None = None
    moneda: str = "MXN"
    fecha: str | None = None
    fecha_entrega_estimada: str | None = None
    lineas: list[OrderLine] = Field(default_factory=list)
    notas: str | None = None


class Price(BaseModel):
    """Precio vigente de un producto tal como lo expone el ERP."""

    producto: str
    precio_ton: float
    moneda: str = "MXN"
    disponible_ton: float | None = None
    vigencia: str | None = None


class Quote(BaseModel):
    """Cotización generada por el ERP a partir de producto y cantidad."""

    id: str
    producto: str
    cantidad: float
    total: float
    moneda: str = "MXN"
    vigencia: str | None = None
    estado: str = "borrador"


class PurchaseRequest(BaseModel):
    """Solicitud de pedido registrada en el ERP."""

    id: str
    producto: str
    cantidad: float
    telefono: str | None = None
    estado: str = "pendiente"


class PurchaseOrder(BaseModel):
    """Orden de compra a un proveedor (folio OC-YYYY-NNNN)."""

    id: str
    proveedor: str
    producto: str
    cantidad: float
    unidad: str = "ton"
    total: float | None = None
    moneda: str = "MXN"
    estado: str = "pendiente"  # pendiente | aprobada | rechazada | recibida
    fecha: str | None = None
    fecha_entrega_estimada: str | None = None


class Supplier(BaseModel):
    """Proveedor registrado en el ERP."""

    id: str
    nombre: str
    productos: list[str] = Field(default_factory=list)
    contacto: str | None = None


class InventoryItem(BaseModel):
    """Existencia de un producto en inventario tal como la expone el ERP."""

    producto: str
    stock_ton: float
    umbral_ton: float
    ubicacion: str | None = None
    estado: str = "normal"  # normal | bajo_umbral


class InventoryAlertEvent(BaseModel):
    """Payload que el ERP envía a /webhooks/erp/inventory-alert cuando un
    producto cae por debajo de su umbral."""

    producto: str
    stock_ton: float
    umbral_ton: float
    ubicacion: str | None = None
    # Texto opcional para sobrescribir el mensaje generado automáticamente.
    mensaje: str | None = None


class OrderEvent(BaseModel):
    """Payload que el ERP envía a /webhooks/erp/order-update para disparar
    una notificación proactiva al cliente por WhatsApp."""

    order_id: str
    telefono: str  # destino en formato E.164 sin '+', p.ej. 5215512345678
    estado_nuevo: str
    cliente: str | None = None
    # Texto opcional para sobrescribir el mensaje generado automáticamente.
    mensaje: str | None = None


class ErpAvisoEvent(BaseModel):
    """Aviso interno que el ERP manda a una persona del equipo.

    Llega a /webhooks/erp/notificacion desde el worker de la outbox
    `avisos_whatsapp`. A diferencia de OrderEvent (que va al CLIENTE), esto va
    a alguien de la empresa: un vencimiento del calendario, un pago estancado,
    un forward por liquidar.
    """

    # Id de la fila en la outbox del ERP. Es la llave de deduplicación: el
    # worker reintenta y el mensaje no puede salir dos veces.
    id: str
    # `tipo` del catálogo de reglas, ej. "calendario.objetivo".
    tipo: str
    telefono: str  # E.164 sin '+', p.ej. 5215512345678
    titulo: str
    mensaje: str
    # Liga profunda a la pantalla del ERP donde se resuelve el pendiente.
    url: str | None = None
    # `<prefijo>:<id>` de la entidad que lo originó, ej. "calendario_entrega:e1".
    referencia: str | None = None
    # Empresa que firma el aviso: el ERP corre para varias sobre el mismo
    # código, así que la identidad viaja con el mensaje.
    empresa: str | None = None

