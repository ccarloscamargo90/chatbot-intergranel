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


# --------------------------------------------------------------------------- #
# Autoservicio del cliente: identidad y consultas propias.
#
# El cliente se identifica con su nombre (o el de su empresa) y su RFC; el ERP
# valida el par y devuelve un TOKEN de sesión. A partir de ahí el bot consulta
# con el token, nunca con el id interno del cliente: si el token se filtra,
# caduca solo, y el bot no puede pedir los datos de un cliente que no sea el
# que se identificó.
# --------------------------------------------------------------------------- #
class CustomerIdentification(BaseModel):
    """Respuesta del ERP a un intento de identificación.

    Un intento fallido NO dice si el RFC existe: `motivo` solo distingue entre
    "no coincide" y las razones que el cliente sí puede corregir (RFC genérico,
    demasiados intentos). Decir "ese RFC existe pero el nombre no" convertiría
    el bot en un verificador de RFCs.
    """

    encontrado: bool
    cliente: str | None = None       # nombre comercial
    razon_social: str | None = None
    rfc: str | None = None
    token: str | None = None
    expira_en_segundos: int = 0
    # no_coincide | rfc_generico | rfc_invalido | bloqueado
    motivo: str | None = None
    intentos_restantes: int | None = None
    espera_minutos: int | None = None


class CustomerDebtLine(BaseModel):
    """Un renglón de la cuenta del cliente: factura, adeudo o nota de crédito."""

    tipo: str  # FACTURA | MANUAL | CREDITO
    folio: str
    concepto: str
    fecha: str | None = None
    fecha_vencimiento: str | None = None
    dias_vencido: int | None = None
    vencida: bool = False
    importe: float = 0.0
    cobrado: float = 0.0
    saldo: float = 0.0
    estado: str = ""


class CustomerDebt(BaseModel):
    """Estado de cuenta del cliente: totales y renglones."""

    cliente: str
    moneda: str = "MXN"
    saldo: float = 0.0
    saldo_vencido: float = 0.0
    lineas: list[CustomerDebtLine] = Field(default_factory=list)


class CustomerOrder(BaseModel):
    """Pedido del cliente (folio PED-YYYY-NNNN)."""

    id: str
    producto: str
    cantidad: float
    unidad: str = "ton"
    total: float | None = None
    moneda: str = "MXN"
    estado: str = "pendiente"
    fecha: str | None = None
    fecha_entrega_estimada: str | None = None
    factura: str | None = None


class CustomerInvoice(BaseModel):
    """Factura del cliente (folio FACT-YYYY-NNNN)."""

    id: str
    fecha: str | None = None
    fecha_vencimiento: str | None = None
    total: float = 0.0
    saldo: float = 0.0
    moneda: str = "MXN"
    estado: str = "emitida"
    contrato: str | None = None
    uuid: str | None = None


class CustomerSummary(BaseModel):
    """Foto rápida del cliente para armar el menú con contexto."""

    cliente: str
    moneda: str = "MXN"
    contratos_activos: int = 0
    pedidos_abiertos: int = 0
    facturas_pendientes: int = 0
    saldo: float = 0.0
    saldo_vencido: float = 0.0


class ChatwootEvent(BaseModel):
    """Lo que Chatwoot manda a /webhooks/chatwoot.

    El payload cambia de forma entre versiones de Chatwoot y entre eventos, así
    que aquí todo es opcional y la lectura va por propiedades. Dos rarezas que
    conviene tener presentes:

    - En `message_created` el id de la conversación viene anidado
      (`conversation.id`), pero en `conversation_status_changed` el `id` de
      arriba YA es el de la conversación.
    - `message_type` a veces es el string "outgoing" y a veces el entero 1.
    """

    event: str = ""
    id: int | None = None
    content: str | None = None
    message_type: str | int | None = None
    private: bool = False
    status: str | None = None
    conversation: dict | None = None

    @property
    def conversacion_id(self) -> int | None:
        anidado = (self.conversation or {}).get("id")
        if anidado is not None:
            return int(anidado)
        # Solo para los eventos de conversación: en los de mensaje, `id` es del
        # mensaje y confundirlos mandaría la respuesta al teléfono equivocado.
        if self.event.startswith("conversation") and self.id is not None:
            return int(self.id)
        return None

    @property
    def es_del_asesor(self) -> bool:
        """True si lo escribió una persona del equipo y el cliente debe verlo.

        Las notas privadas quedan fuera (son para el equipo) y los entrantes
        también: esos los publicó el propio bot al reenviar lo que dijo el
        cliente, y devolvérselos sería un eco infinito.
        """
        if self.private:
            return False
        tipo = self.message_type
        return tipo in ("outgoing", 1)

    @property
    def conversacion_resuelta(self) -> bool:
        return self.event == "conversation_status_changed" and self.status == "resolved"


class CustomerDocument(BaseModel):
    """Un documento del cliente, con sus bytes.

    Viaja con el contenido y no con una URL a propósito: el bot lo sube a la
    Media API de Meta, así que en ningún momento existe un enlace desde el que
    se pueda bajar la factura de un cliente.
    """

    nombre: str
    tipo_mime: str
    contenido: bytes
