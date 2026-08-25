"""Notificaciones proactivas sobre órdenes de compra vía WhatsApp.

Cuando el ERP reporta un cambio de estado en una orden, generamos un mensaje
y lo enviamos al cliente. Si hay una plantilla aprobada configurada
(WHATSAPP_ORDER_TEMPLATE), se usa (necesario para mensajes iniciados por el
negocio fuera de la ventana de 24h). Si no, se envía como texto libre.
"""

from __future__ import annotations

import logging

from .config import get_settings
from .models import ErpAvisoEvent, InventoryAlertEvent, OrderEvent
from .whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

# Mensajes legibles por estado de la orden. Las claves se comparan en
# minúsculas, e incluyen tanto valores propios del bot como los enums del ERP
# (EstadoContrato / EstadoEmbarque / EstadoFactura).
ESTADO_MENSAJES: dict[str, str] = {
    # Genéricos / bot
    "confirmada": "✅ Su orden {order_id} ha sido confirmada. ¡Gracias por su compra!",
    "en_proceso": "🔄 Su orden {order_id} está en preparación.",
    "en_ruta": "🚚 Su orden {order_id} va en camino. Le avisaremos al llegar.",
    "entregada": "📦 Su orden {order_id} fue entregada. ¡Gracias por confiar en Intergranel!",
    "retrasada": (
        "⏳ Su orden {order_id} presenta un retraso. "
        "Un asesor le contactará con los detalles."
    ),
    "cancelada": (
        "❌ Su orden {order_id} ha sido cancelada. "
        "Si tiene dudas, responda a este mensaje."
    ),
    "factura_disponible": "🧾 La factura de su orden {order_id} ya está disponible.",
    "pago_pendiente": (
        "💳 Su orden {order_id} tiene un pago pendiente. "
        "Responda a este mensaje para más información."
    ),
    # EstadoContrato
    "activo": "✅ Su contrato {order_id} está activo. ¡Gracias por su compra!",
    "completado": "📦 Su orden {order_id} se completó. ¡Gracias por confiar en Intergranel!",
    "cancelado": (
        "❌ Su orden {order_id} ha sido cancelada. "
        "Si tiene dudas, responda a este mensaje."
    ),
    # EstadoEmbarque
    "programado": "📅 El embarque de su orden {order_id} está programado.",
    "en_transito": "🚚 Su orden {order_id} va en camino. Le avisaremos al llegar.",
    "entregado": "📦 Su orden {order_id} fue entregada. ¡Gracias por confiar en Intergranel!",
    "incidencia": (
        "⚠️ Hubo una incidencia con el embarque de su orden {order_id}. "
        "Un asesor le contactará."
    ),
    # EstadoFactura
    "emitida": "🧾 La factura de su orden {order_id} ya está disponible.",
    "cobrada": "💳 Hemos registrado el pago total de su orden {order_id}. ¡Gracias!",
}


def build_message(event: OrderEvent) -> str:
    if event.mensaje:
        return event.mensaje
    plantilla = ESTADO_MENSAJES.get(
        event.estado_nuevo.lower(),
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


def build_inventory_message(event: InventoryAlertEvent) -> str:
    if event.mensaje:
        return event.mensaje
    ubicacion = f" en {event.ubicacion}" if event.ubicacion else ""
    return (
        f"⚠️ Alerta de inventario: *{event.producto}*{ubicacion} está bajo el umbral.\n"
        f"Stock actual: {event.stock_ton:g} ton (umbral: {event.umbral_ton:g} ton).\n"
        "Conviene reabastecer."
    )


async def notify_inventory_alert(
    wa: WhatsAppClient, event: InventoryAlertEvent
) -> dict:
    """Envía la alerta de inventario al equipo (INVENTORY_ALERT_PHONES).

    Si no hay destinatarios configurados, solo se registra en el log.
    """
    settings = get_settings()
    mensaje = build_inventory_message(event)
    destinatarios = settings.inventory_alert_list

    if not destinatarios:
        logger.info(
            "Alerta de inventario (%s) sin destinatarios configurados: %s",
            event.producto,
            mensaje,
        )
        return {"sent": 0, "recipients": []}

    enviados = []
    for telefono in destinatarios:
        logger.info("Enviando alerta de inventario a %s (%s)", telefono, event.producto)
        await wa.send_text(telefono, mensaje)
        enviados.append(telefono)
    return {"sent": len(enviados), "recipients": enviados}


# --------------------------------------------------------------------------- #
# Avisos internos del ERP al equipo (calendario, pagos, forwards…).
# --------------------------------------------------------------------------- #

# Emoji por familia de aviso. Ayuda a distinguir de un vistazo un vencimiento
# de un pendiente de cobranza cuando llegan varios en la mañana. Se busca por
# prefijo del `tipo`, así un módulo nuevo hereda el de su familia sin tocar
# esto; lo desconocido cae en el genérico.
AVISO_EMOJI: dict[str, str] = {
    "calendario.atraso": "🔴",
    "calendario.objetivo": "📅",
    "calendario.previo": "🗓️",
    "calendario.recordatorio": "📌",
    "pago": "💳",
    "forward": "📈",
    "reciba": "⚖️",
    "traza": "🔍",
    "deuda": "💰",
    "almacen": "📦",
    "nomina-operativa": "👷",
}


def _emoji_aviso(tipo: str) -> str:
    """Emoji del tipo exacto, si no del módulo, si no el genérico."""
    if tipo in AVISO_EMOJI:
        return AVISO_EMOJI[tipo]
    return AVISO_EMOJI.get(tipo.split(".", 1)[0], "🔔")


def build_aviso_message(event: ErpAvisoEvent) -> str:
    """Arma el texto del aviso interno.

    El ERP ya redactó el título y el detalle —sabe de qué habla—, así que aquí
    solo se le da forma de mensaje de WhatsApp: encabezado, cuerpo y a dónde ir
    a resolverlo.
    """
    lineas = [f"{_emoji_aviso(event.tipo)} *{event.titulo}*", "", event.mensaje]
    if event.url:
        lineas += ["", f"Resuélvelo aquí: {event.url}"]
    if event.empresa:
        lineas += ["", f"_{event.empresa} · ERP_"]
    return "\n".join(lineas)


async def notify_erp_aviso(wa: WhatsAppClient, event: ErpAvisoEvent) -> dict:
    """Manda el aviso interno por WhatsApp a la persona que le toca.

    Fuera de la ventana de 24h Meta solo acepta plantillas aprobadas, y un
    vencimiento avisado a las 7:30 casi nunca cae dentro de la ventana. Por eso
    se usa la plantilla cuando está configurada y solo se cae a texto libre
    cuando no la hay (desarrollo, o una conversación abierta).
    """
    settings = get_settings()

    if settings.whatsapp_aviso_template:
        logger.info(
            "Enviando aviso %s (%s) a %s por plantilla", event.id, event.tipo, event.telefono
        )
        return await wa.send_template(
            to=event.telefono,
            template_name=settings.whatsapp_aviso_template,
            language=settings.whatsapp_template_language,
            body_params=[
                event.titulo,
                # La plantilla lleva el detalle y la liga en un solo parámetro:
                # Meta no admite saltos de línea en los parámetros, así que se
                # unen con separador.
                f"{event.mensaje} {event.url}".strip() if event.url else event.mensaje,
                event.empresa or settings.company_name,
            ],
        )

    logger.info("Enviando aviso %s (%s) a %s como texto", event.id, event.tipo, event.telefono)
    return await wa.send_text(event.telefono, build_aviso_message(event))

