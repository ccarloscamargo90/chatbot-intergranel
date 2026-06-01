"""Aplicación FastAPI: webhooks de WhatsApp y de notificaciones del ERP."""

from __future__ import annotations

import json
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response

from .assistant import Assistant
from .config import get_settings
from .models import OrderEvent
from .notifications import notify_order_event
from .whatsapp import WhatsAppClient, verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("intergranel")

settings = get_settings()
app = FastAPI(title="Intergranel · Asistente de WhatsApp")

wa = WhatsAppClient()
assistant = Assistant()


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


async def _process_message(message: dict) -> None:
    phone = message.get("from")
    if not phone:
        return
    try:
        if message.get("type") != "text":
            await wa.send_text(
                phone,
                "Por ahora solo puedo atender mensajes de texto. "
                "¿En qué puedo ayudarle con sus órdenes? 🙂",
            )
            return
        text = message["text"]["body"]
        reply = await assistant.handle(phone, text)
        await wa.send_text(phone, reply)
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
