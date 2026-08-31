"""Cliente para la WhatsApp Cloud API de Meta (envío de mensajes)."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from .config import get_settings
from .replies import MAX_CUERPO, Reply

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
        self._graph = f"https://graph.facebook.com/{settings.whatsapp_api_version}"
        self._phone_number_id = settings.whatsapp_phone_number_id
        self._url = f"{self._graph}/{self._phone_number_id}/messages"

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

    async def send_buttons(
        self,
        to: str,
        body: str,
        buttons: list[dict],
        header: str = "",
        footer: str = "",
    ) -> dict[str, Any]:
        """Envía hasta 3 botones de respuesta rápida.

        `buttons` ya viene en el formato de Meta (ver `Boton.to_payload`)."""
        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body[:MAX_CUERPO]},
            "action": {"buttons": buttons},
        }
        if header:
            interactive["header"] = {"type": "text", "text": header[:60]}
        if footer:
            interactive["footer"] = {"text": footer[:60]}
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "interactive",
                "interactive": interactive,
            }
        )

    async def send_list(
        self,
        to: str,
        body: str,
        action: dict,
        header: str = "",
        footer: str = "",
    ) -> dict[str, Any]:
        """Envía un menú de lista (hasta 10 filas).

        `action` ya viene en el formato de Meta (ver `MenuLista.to_payload`)."""
        interactive: dict[str, Any] = {
            "type": "list",
            "body": {"text": body[:MAX_CUERPO]},
            "action": action,
        }
        if header:
            interactive["header"] = {"type": "text", "text": header[:60]}
        if footer:
            interactive["footer"] = {"text": footer[:60]}
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "interactive",
                "interactive": interactive,
            }
        )

    async def send_reply(self, to: str, reply: Reply | str) -> dict[str, Any]:
        """Envía una `Reply`: texto, botones o menú, según lo que traiga.

        Es el único punto por el que se contesta a un cliente, para que ningún
        agente tenga que saber cómo se arma un mensaje interactivo de Meta. Si
        el envío interactivo falla (un título inválido, una lista mal armada),
        cae a texto: el cliente prefiere la respuesta sin botones antes que el
        silencio.
        """
        reply = Reply.coerce(reply)
        if not reply.es_interactiva:
            return await self.send_text(to, reply.texto)
        try:
            if reply.lista is not None:
                return await self.send_list(
                    to,
                    reply.texto,
                    reply.lista.to_payload(),
                    header=reply.encabezado,
                    footer=reply.pie,
                )
            return await self.send_buttons(
                to,
                reply.texto,
                [b.to_payload() for b in reply.botones],
                header=reply.encabezado,
                footer=reply.pie,
            )
        except Exception:  # noqa: BLE001 - degradar a texto, nunca callar
            logger.exception("Falló el mensaje interactivo a %s; se manda como texto", to)
            return await self.send_text(to, reply.texto)

    async def upload_media(
        self, contenido: bytes, filename: str, mime_type: str
    ) -> str:
        """Sube un archivo a la Media API de Meta y devuelve su `media_id`.

        Es el paso previo a `send_document`. La alternativa sería mandarle a
        Meta un `link` público para que lo baje, pero eso obliga a publicar —
        aunque sea unos minutos, aunque sea firmada— una URL desde la que
        cualquiera puede bajar la factura de un cliente. Subiendo los bytes,
        esa URL no existe nunca.

        El id vive 30 días del lado de Meta; aquí se usa de inmediato.
        """
        if not self._token:
            logger.info("[WhatsApp DEV] upload_media %s (%s bytes)", filename, len(contenido))
            return f"dev-media-{filename}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self._graph}/{self._phone_number_id}/media",
                headers={"Authorization": f"Bearer {self._token}"},
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (filename, contenido, mime_type)},
            )
            if resp.status_code >= 400:
                logger.error("Error subiendo media %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()
            return resp.json()["id"]

    async def send_document(
        self,
        to: str,
        media_id: str,
        filename: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """Manda un documento ya subido, por su `media_id`.

        `filename` es lo que el cliente ve y lo que le queda guardado en el
        teléfono: mandar "FACT-2026-0031.pdf" y no "documento.pdf" es la
        diferencia entre que lo encuentre después y que no.
        """
        documento: dict[str, Any] = {"id": media_id, "filename": filename[:240]}
        if caption:
            documento["caption"] = caption[:1024]
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "document",
                "document": documento,
            }
        )

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

    async def get_media_url(self, media_id: str) -> str:
        """Resuelve la URL de descarga (temporal) de un media a partir de su id."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._graph}/{media_id}",
                headers={"Authorization": f"Bearer {self._token}"},
            )
            resp.raise_for_status()
            return resp.json()["url"]

    async def download_media(self, media_url: str) -> bytes:
        """Descarga el binario de un media (la URL requiere el token)."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                media_url, headers={"Authorization": f"Bearer {self._token}"}
            )
            resp.raise_for_status()
            return resp.content
