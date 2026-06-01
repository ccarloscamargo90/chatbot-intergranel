"""Cliente para la WhatsApp Cloud API de Meta (envío de mensajes)."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)


def verify_signature(payload: bytes, header: str | None, app_secret: str) -> bool:
    """Valida la firma X-Hub-Signature-256 que Meta envía en cada webhook.

    Meta firma el cuerpo crudo de la petición con HMAC-SHA256 usando el
    App Secret y lo envía en el header como 'sha256=<hex>'. Comparamos en
    tiempo constante para evitar ataques de temporización.
    """
    if not header or not header.startswith("sha256="):
        return False
    expected = header.split("=", 1)[1].strip()
    digest = hmac.new(app_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


class WhatsAppClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.whatsapp_token
        self._url = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}"
            f"/{settings.whatsapp_phone_number_id}/messages"
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._token:
            # Modo desarrollo sin credenciales: registramos en vez de enviar.
            logger.info("[WhatsApp DEV] %s", payload)
            return {"dev": True, "payload": payload}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._url, headers=self._headers, json=payload)
            if resp.status_code >= 400:
                logger.error("Error WhatsApp %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()
            return resp.json()

    async def send_text(self, to: str, text: str) -> dict[str, Any]:
        """Envía un mensaje de texto (válido dentro de la ventana de 24h)."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": text[:4096]},
        }
        return await self._post(payload)

    async def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        body_params: list[str] | None = None,
    ) -> dict[str, Any]:
        """Envía una plantilla aprobada (requerido para mensajes proactivos
        fuera de la ventana de 24h)."""
        components = []
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                }
            )
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components,
            },
        }
        return await self._post(payload)
