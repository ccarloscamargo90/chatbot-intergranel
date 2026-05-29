"""Notificaciones proactivas sobre órdenes de compra vía WhatsApp.

Cuando el ERP reporta un cambio de estado en una orden, generamos un mensaje
y lo enviamos al cliente. Si hay una plantilla aprobada configurada
(WHATSAPP_ORDER_TEMPLATE), se usa (necesario para mensajes iniciados por el
negocio fuera de la ventana de 24h). Si no, se envía como texto libre.
"""

from __future__ import annotations

import logging

from .config import get_settings
from .models import OrderEvent
from .whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

# Mensajes legibles por estado de la orden.
ESTADO_MENSAJES: dict[str, str] = {
    "confirmada": "✅ Su orden {order_id} ha sido confirmada. ¡Gracias por su compra!",
    "en_proceso": "🔄 Su orden {order_id} está en preparación.",
    "en_ruta": "🚚 Su orden {order_id} va en camino. Le avisaremos al llegar.",
    "entregada": "📦 Su orden {order_id} fue entregada. ¡Gracias por confiar en Intergranel!",
    "retrasada": "⏳ Su orden {order_id} presenta un retraso. Un asesor le contactará con los detalles.",
    "cancelada": "❌ Su orden {order_id} ha sido cancelada. Si tiene dudas, responda a este mensaje.",
    "factura_disponible": "🧾 La factura de su orden {order_id} ya está disponible.",
    "pago_pendiente": "💳 Su orden {order_id} tiene un pago pendiente. Responda a este mensaje para más información.",
}


def build_message(event: OrderEvent) -> str:
    if event.mensaje:
        return event.mensaje
    plantilla = ESTADO_MENSAJES.get(
        event.estado_nuevo,
        "ℹ️ Su orden {order_id} cambió de estado a: " + event.estado_nuevo + ".",
    )
    return plantilla.format(order_id=event.order_id)


async def notify_order_event(wa: WhatsAppClient, event: OrderEvent) -> dict:
    settings = get_settings()
    mensaje = build_message(event)

    if settings.whatsapp_order_template:
        # Las plantillas reciben parámetros posicionales para el cuerpo.
        # Ajusta el orden/cantidad según cómo definas la plantilla en Meta.
        body_params = [
            event.cliente or "cliente",
            event.order_id,
            event.estado_nuevo,
        ]
        logger.info("Enviando plantilla a %s para orden %s", event.telefono, event.order_id)
        return await wa.send_template(
            to=event.telefono,
            template_name=settings.whatsapp_order_template,
            language=settings.whatsapp_template_language,
            body_params=body_params,
        )

    logger.info("Enviando texto a %s para orden %s", event.telefono, event.order_id)
    return await wa.send_text(event.telefono, mensaje)
