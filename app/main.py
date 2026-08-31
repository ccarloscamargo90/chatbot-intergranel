"""Aplicación FastAPI: webhooks de WhatsApp y de notificaciones del ERP."""

from __future__ import annotations

import base64
import hmac
import json
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response

from .bus import get_event_bus
from .chatwoot import ChatwootNoDisponible, get_chatwoot_client
from .config import get_settings
from .dedup import get_dedup_store
from .handoff import HandoffStore
from .menus import BOTONES_SEGUIMIENTO
from .models import ChatwootEvent, ErpAvisoEvent, InventoryAlertEvent, OrderEvent
from .notifications import notify_erp_aviso, notify_inventory_alert, notify_order_event
from .replies import Reply
from .router import Router
from .whatsapp import WhatsAppClient, verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("intergranel")

settings = get_settings()
app = FastAPI(title="Intergranel · Asistente de WhatsApp")

wa = WhatsAppClient()
router = Router()
dedup = get_dedup_store()
chatwoot = get_chatwoot_client()
handoff = HandoffStore()


@app.get("/")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "intergranel-whatsapp-assistant",
        "erp": "mock" if settings.use_mock_erp else "http",
        "history": "redis" if settings.redis_url else "memory",
        "model": settings.claude_model,
        "asesor": "chatwoot" if chatwoot.habilitado else "sin configurar",
    }


# --------------------------------------------------------------------------- #
# WhatsApp: verificación del webhook (Meta hace un GET al configurarlo).
# --------------------------------------------------------------------------- #
@app.get("/webhooks/whatsapp")
async def verify_whatsapp(request: Request) -> Response:
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.whatsapp_verify_token
    ):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verificación fallida")


# --------------------------------------------------------------------------- #
# WhatsApp: mensajes entrantes de clientes.
# Respondemos 200 de inmediato y procesamos en segundo plano (Meta exige una
# respuesta rápida y reintenta si tardamos).
# --------------------------------------------------------------------------- #
@app.post("/webhooks/whatsapp")
async def incoming_whatsapp(request: Request, background: BackgroundTasks) -> dict:
    raw = await request.body()
    # Validamos la firma de Meta sobre el cuerpo crudo (si hay app secret).
    if settings.whatsapp_app_secret and not verify_signature(
        raw, request.headers.get("X-Hub-Signature-256"), settings.whatsapp_app_secret
    ):
        raise HTTPException(status_code=401, detail="Firma inválida")
    body = json.loads(raw or b"{}")
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                background.add_task(_process_message, message)
    return {"status": "received"}


# Tipos de media soportados y límites de tamaño (bytes).
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024       # 5 MB (límite de imágenes de Claude)
MAX_PDF_BYTES = 16 * 1024 * 1024        # 16 MB (margen conservador)


async def _build_media_content(message: dict) -> tuple[list, str] | None:
    """Construye el contenido para la API a partir de un mensaje de imagen o
    documento de WhatsApp. Devuelve (content, store_text) o None si el tipo o
    tamaño no es soportado."""
    mtype = message["type"]
    media = message.get(mtype, {})
    media_id = media.get("id")
    if not media_id:
        return None
    mime = media.get("mime_type", "")
    caption = (media.get("caption") or "").strip()

    media_url = await wa.get_media_url(media_id)
    raw = await wa.download_media(media_url)

    if mtype == "image" and mime in SUPPORTED_IMAGE_TYPES:
        if len(raw) > MAX_IMAGE_BYTES:
            return None
        data = base64.standard_b64encode(raw).decode()
        blocks: list = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
        ]
        placeholder = "[imagen recibida]"
    elif mtype == "document" and mime == "application/pdf":
        if len(raw) > MAX_PDF_BYTES:
            return None
        data = base64.standard_b64encode(raw).decode()
        filename = media.get("filename") or "documento.pdf"
        blocks = [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            }
        ]
        placeholder = f"[documento PDF recibido: {filename}]"
    else:
        return None

    if caption:
        blocks.append({"type": "text", "text": caption})
        placeholder = f"{placeholder} {caption}".strip()
    return blocks, placeholder


def _texto_interactivo(message: dict) -> str | None:
    """El id del botón o de la fila que tocó el cliente.

    Meta manda el toque como `interactive.button_reply` (botones) o
    `interactive.list_reply` (menú de lista); en ambos casos trae `id` y
    `title`. Nos quedamos con el **id**, que es el contrato estable: el título
    es texto de pantalla y puede cambiar al reescribir un menú, mientras que el
    id es lo que `menus.py` y el router acordaron.
    """
    interactivo = message.get("interactive", {})
    respuesta = interactivo.get("button_reply") or interactivo.get("list_reply") or {}
    boton_id = (respuesta.get("id") or "").strip()
    return boton_id or (respuesta.get("title") or "").strip() or None


def _texto_para_el_asesor(message: dict) -> str:
    """Lo que dijo el cliente, en texto plano para la bandeja del asesor.

    Chatwoot recibe texto; una imagen o un PDF se anuncian en vez de subirse.
    Es una pérdida consciente: el asesor sabe que llegó un archivo y puede
    pedírselo, que es mucho mejor que no enterarse de que existe.
    """
    mtype = message.get("type")
    if mtype == "text":
        return message.get("text", {}).get("body", "")
    if mtype == "interactive":
        seleccion = _texto_interactivo(message) or ""
        return f"[tocó una opción del menú: {seleccion}]" if seleccion else "[tocó una opción]"
    if mtype in ("image", "document", "audio", "video", "sticker"):
        pie = (message.get(mtype, {}) or {}).get("caption") or ""
        etiqueta = f"[el cliente envió un archivo: {mtype}]"
        return f"{etiqueta} {pie}".strip()
    return f"[mensaje de tipo {mtype}, no soportado por el bot]"


async def _reenviar_al_asesor(activo, message: dict, phone: str) -> None:
    """Pasa a Chatwoot lo que escribió el cliente durante el handoff."""
    texto = _texto_para_el_asesor(message)
    try:
        await chatwoot.mensaje_del_cliente(activo.conversacion_id, texto)
    except ChatwootNoDisponible:
        # Si el mensaje no llegó a la bandeja, nadie lo va a leer. Decírselo es
        # mejor que dejarlo creyendo que un asesor lo está viendo.
        logger.exception("No se pudo reenviar a Chatwoot el mensaje de %s", phone)
        await wa.send_text(
            phone,
            "No logramos entregar su mensaje al asesor. ¿Puede intentarlo de "
            "nuevo en un momento?",
        )


async def _process_message(message: dict) -> None:
    phone = message.get("from")
    if not phone:
        return
    # Idempotencia: ignora reenvíos del mismo mensaje (Meta reintenta webhooks).
    message_id = message.get("id")
    if message_id and await dedup.is_duplicate(message_id):
        logger.info("Mensaje duplicado ignorado: %s", message_id)
        return
    try:
        # ¿Este teléfono está con un asesor? Entonces el bot no contesta: lo que
        # diga el cliente va a la bandeja donde está la persona que lo atiende.
        # Va ANTES de mirar el tipo de mensaje para que también se reenvíen los
        # toques de botón que hayan quedado en pantalla del turno anterior.
        activo = await handoff.por_telefono(phone)
        if activo is not None:
            await _reenviar_al_asesor(activo, message, phone)
            return

        mtype = message.get("type")
        if mtype == "text":
            text = message["text"]["body"]
            reply = await router.route(phone, text)
            await wa.send_reply(phone, reply)
            return

        if mtype == "interactive":
            seleccion = _texto_interactivo(message)
            if seleccion is None:
                await wa.send_text(
                    phone, "No alcancé a ver qué opción eligió. ¿Puede intentarlo otra vez?"
                )
                return
            reply = await router.route(phone, seleccion)
            await wa.send_reply(phone, reply)
            return

        if mtype in ("image", "document"):
            built = await _build_media_content(message)
            if built is None:
                await wa.send_text(
                    phone,
                    "Puedo leer imágenes y documentos PDF (hasta unos pocos MB). "
                    "¿Podría reenviarlo en ese formato o escribir su consulta?",
                )
                return
            content, store_text = built
            reply = await router.route(phone, content, store_text)
            await wa.send_reply(phone, reply)
            return

        await wa.send_text(
            phone,
            "Por ahora puedo atender texto, imágenes y documentos PDF. "
            "¿En qué puedo ayudarle con sus órdenes? 🙂",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Error procesando mensaje de %s", phone)
        try:
            await wa.send_text(
                phone,
                "Tuvimos un inconveniente técnico. Por favor, intente de nuevo "
                "en unos minutos.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo enviar el mensaje de error a %s", phone)


# --------------------------------------------------------------------------- #
# ERP: webhook de cambios de estado de órdenes -> notificación al cliente.
# --------------------------------------------------------------------------- #
@app.post("/webhooks/erp/order-update")
async def erp_order_update(
    event: OrderEvent,
    x_webhook_secret: str = Header(default=""),
) -> dict:
    if settings.erp_webhook_secret and x_webhook_secret != settings.erp_webhook_secret:
        raise HTTPException(status_code=401, detail="Secreto inválido")
    result = await notify_order_event(wa, event)
    return {"status": "sent", "result": result}


# --------------------------------------------------------------------------- #
# ERP: webhook de alerta de inventario -> notificación proactiva al equipo.
# --------------------------------------------------------------------------- #
@app.post("/webhooks/erp/inventory-alert")
async def erp_inventory_alert(
    event: InventoryAlertEvent,
    x_webhook_secret: str = Header(default=""),
) -> dict:
    if settings.erp_webhook_secret and x_webhook_secret != settings.erp_webhook_secret:
        raise HTTPException(status_code=401, detail="Secreto inválido")
    # Publicamos la alerta en el bus para que el agente de Inventario y otros
    # puedan consultarla, y notificamos al equipo por WhatsApp.
    bus = get_event_bus()
    await bus.publish(
        f"bus:inventario:alerta:{event.producto}",
        event.model_dump(),
        ttl=60 * 60 * 24,
    )
    result = await notify_inventory_alert(wa, event)
    return {"status": "sent", "result": result}


# --------------------------------------------------------------------------- #
# ERP: webhook de avisos internos -> notificación a la persona del equipo.
#
# Es el otro sentido del canal: /webhooks/erp/order-update avisa al CLIENTE,
# esto avisa a alguien de la empresa (un vencimiento del calendario, un pago
# estancado). Lo dispara el worker de la outbox `avisos_whatsapp` del ERP.
# --------------------------------------------------------------------------- #
# TTL del aviso en el bus: una semana para el detalle, un día para "lo último
# que le mandamos a este teléfono".
AVISO_TTL_SECONDS = 60 * 60 * 24 * 7
AVISO_RECIENTE_TTL_SECONDS = 60 * 60 * 24


def _wamid(resultado: dict) -> str | None:
    """Extrae el wamid de la respuesta de Meta, o None en modo desarrollo."""
    mensajes = resultado.get("messages") if isinstance(resultado, dict) else None
    if isinstance(mensajes, list) and mensajes:
        primero = mensajes[0]
        if isinstance(primero, dict):
            return primero.get("id")
    return None


def _detalle_error(exc: Exception) -> str:
    """El motivo REAL del fallo, en una línea que el ERP pueda guardar.

    Cuando Meta rechaza el envío, su respuesta trae el diagnóstico exacto
    ("template name (erp_aviso) does not exist in es_MX"). Sin esto el ERP solo
    ve "Request failed with status code 500" y hay que ir a leer logs — que es
    justo lo que la bandeja de avisos existe para evitar.
    """
    respuesta = getattr(exc, "response", None)
    if respuesta is not None:
        try:
            error = respuesta.json().get("error", {})
        except Exception:  # noqa: BLE001 - el cuerpo puede no ser JSON
            error = {}
        detalle = (error.get("error_data") or {}).get("details")
        mensaje = error.get("message")
        partes = [p for p in (mensaje, detalle) if p]
        if partes:
            return f"Meta {respuesta.status_code}: {' · '.join(partes)}"
        return f"Meta {respuesta.status_code}: {respuesta.text[:300]}"
    return f"{type(exc).__name__}: {exc}"


@app.post("/webhooks/erp/notificacion")
async def erp_notificacion(
    event: ErpAvisoEvent,
    x_webhook_secret: str = Header(default=""),
) -> dict:
    if settings.erp_webhook_secret and x_webhook_secret != settings.erp_webhook_secret:
        raise HTTPException(status_code=401, detail="Secreto inválido")

    # El worker del ERP reintenta ante timeouts, así que el mismo aviso puede
    # llegar más de una vez. Se responde "duplicate" (que el ERP trata como
    # entrega buena) en vez de mandarle el mensaje dos veces a la persona.
    clave_dedup = f"aviso:{event.id}"
    if await dedup.is_duplicate(clave_dedup):
        logger.info("Aviso %s duplicado: no se reenvía", event.id)
        return {"status": "duplicate"}

    # El aviso queda en el bus para que, si la persona responde "¿cuál
    # vencimiento?", el agente tenga el contexto en vez de contestar en frío.
    bus = get_event_bus()
    datos = event.model_dump()
    await bus.publish(f"bus:erp:aviso:{event.id}", datos, ttl=AVISO_TTL_SECONDS)
    await bus.publish(
        f"bus:erp:aviso_reciente:{event.telefono}", datos, ttl=AVISO_RECIENTE_TTL_SECONDS
    )

    try:
        resultado = await notify_erp_aviso(wa, event)
    except Exception as exc:  # noqa: BLE001 - cualquier fallo tiene que soltar la marca
        # El aviso NO salió. Si la marca de deduplicación se queda puesta, el
        # reintento del ERP entra por la rama de "duplicate", que el ERP trata
        # como entrega buena: el aviso quedaría marcado ENVIADO sin que nadie lo
        # recibiera, y sin más reintentos. Soltarla es lo que hace que el
        # reintento sirva de algo.
        await dedup.release(clave_dedup)
        motivo = _detalle_error(exc)
        logger.error("No se pudo entregar el aviso %s: %s", event.id, motivo)
        # 200 con status="failed": el detalle viaja en el cuerpo para que el ERP
        # lo guarde en su outbox. Un 5xx solo le daría "Request failed with
        # status code 500", que no dice nada.
        return {"status": "failed", "error": motivo}

    return {"status": "sent", "wamid": _wamid(resultado), "result": resultado}


# --------------------------------------------------------------------------- #
# Chatwoot: lo que escribe el asesor -> WhatsApp del cliente.
#
# Es el sentido de vuelta del handoff. El de ida vive en `_process_message`:
# mientras el teléfono está en handoff, lo que dice el cliente se reenvía a la
# conversación de Chatwoot en vez de ir al agente.
# --------------------------------------------------------------------------- #
@app.post("/webhooks/chatwoot")
async def chatwoot_webhook(
    event: ChatwootEvent,
    request: Request,
    x_webhook_secret: str = Header(default=""),
) -> dict:
    # Chatwoot NO firma sus webhooks. Sin un secreto compartido, cualquiera que
    # descubra esta URL puede hacerle decir al bot lo que quiera por WhatsApp,
    # a nombre de la empresa. El header es lo preferible; el query param existe
    # porque la UI de Chatwoot solo deja capturar una URL.
    if settings.chatwoot_webhook_secret:
        recibido = x_webhook_secret or request.query_params.get("secret", "")
        if not hmac.compare_digest(recibido, settings.chatwoot_webhook_secret):
            raise HTTPException(status_code=401, detail="Secreto inválido")

    conversacion_id = event.conversacion_id
    if conversacion_id is None:
        return {"status": "ignored", "motivo": "sin conversación"}

    activo = await handoff.por_conversacion(conversacion_id)
    if activo is None:
        # Conversación que no abrió el bot, o handoff ya vencido/cerrado.
        return {"status": "ignored", "motivo": "sin handoff"}

    # El asesor cerró el caso: el bot retoma ese teléfono.
    if event.conversacion_resuelta:
        await handoff.cerrar(activo)
        logger.info("Handoff de %s cerrado tras %s min", activo.telefono, activo.minutos)
        await wa.send_reply(
            activo.telefono,
            Reply(
                texto="Quedamos atentos. ¿Puedo ayudarle en algo más? 🌾",
                botones=list(BOTONES_SEGUIMIENTO),
            ),
        )
        return {"status": "closed"}

    if not event.es_del_asesor:
        # Nota privada, o el eco de lo que el propio bot acaba de publicar.
        return {"status": "ignored", "motivo": "no es del asesor"}

    contenido = (event.content or "").strip()
    if not contenido:
        return {"status": "ignored", "motivo": "sin contenido"}

    # Chatwoot reintenta ante un timeout: el cliente no puede recibir dos veces
    # el mismo mensaje del asesor.
    if event.id is not None and await dedup.is_duplicate(f"chatwoot:{event.id}"):
        return {"status": "duplicate"}

    try:
        await wa.send_text(activo.telefono, contenido)
    except Exception as exc:  # noqa: BLE001 - el asesor tiene que enterarse
        # El caso típico: pasaron más de 24h desde el último mensaje del cliente
        # y Meta rechaza el texto libre. El asesor lo ve entregado en Chatwoot y
        # no lo está; por eso se lo decimos ahí mismo, en su propia bandeja.
        motivo = _detalle_error(exc)
        logger.error("No se entregó a %s la respuesta del asesor: %s", activo.telefono, motivo)
        if event.id is not None:
            await dedup.release(f"chatwoot:{event.id}")
        try:
            await chatwoot.nota_privada(
                conversacion_id,
                f"⚠️ WhatsApp NO entregó este mensaje: {motivo}\n"
                "Si pasaron más de 24 h desde el último mensaje del cliente, "
                "Meta rechaza el texto libre y hay que reabrir con una plantilla.",
            )
        except ChatwootNoDisponible:
            logger.exception("Tampoco se pudo avisar del fallo en Chatwoot")
        return {"status": "failed", "error": motivo}

    return {"status": "sent"}
