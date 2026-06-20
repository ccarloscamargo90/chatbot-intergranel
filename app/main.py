"""Aplicación FastAPI: webhooks de WhatsApp y de notificaciones del ERP."""

from __future__ import annotations

import base64
import json
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response

from .config import get_settings
from .dedup import get_dedup_store
from .models import OrderEvent
from .notifications import notify_order_event
from .router import Router
from .whatsapp import WhatsAppClient, verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("intergranel")

settings = get_settings()
app = FastAPI(title="Intergranel · Asistente de WhatsApp")

wa = WhatsAppClient()
router = Router()
dedup = get_dedup_store()


@app.get("/")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "intergranel-whatsapp-assistant",
        "erp": "mock" if settings.use_mock_erp else "http",
        "history": "redis" if settings.redis_url else "memory",
        "model": settings.claude_model,
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
        mtype = message.get("type")
        if mtype == "text":
            text = message["text"]["body"]
            reply = await router.route(phone, text)
            await wa.send_text(phone, reply)
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
            await wa.send_text(phone, reply)
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
