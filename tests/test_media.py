"""Pruebas del soporte de imágenes y documentos en WhatsApp."""

import asyncio
import base64

from app import main
from app.dedup import InMemoryDedupStore

JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
PDF_BYTES = b"%PDF-1.4 fake-pdf-bytes"


def _patch_media(monkeypatch, raw: bytes):
    async def fake_get_media_url(media_id):
        return f"https://media.example/{media_id}"

    async def fake_download_media(url):
        return raw

    monkeypatch.setattr(main.wa, "get_media_url", fake_get_media_url)
    monkeypatch.setattr(main.wa, "download_media", fake_download_media)


def test_build_image_content(monkeypatch):
    _patch_media(monkeypatch, JPEG_BYTES)
    message = {
        "type": "image",
        "image": {"id": "MID1", "mime_type": "image/jpeg", "caption": "mi remisión"},
    }
    content, store_text = asyncio.run(main._build_media_content(message))
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[0]["source"]["data"] == base64.standard_b64encode(JPEG_BYTES).decode()
    # El caption se adjunta como bloque de texto y al placeholder.
    assert content[1] == {"type": "text", "text": "mi remisión"}
    assert "imagen recibida" in store_text and "mi remisión" in store_text


def test_build_pdf_content(monkeypatch):
    _patch_media(monkeypatch, PDF_BYTES)
    message = {
        "type": "document",
        "document": {
            "id": "MID2",
            "mime_type": "application/pdf",
            "filename": "factura.pdf",
        },
    }
    content, store_text = asyncio.run(main._build_media_content(message))
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"
    assert "factura.pdf" in store_text


def test_build_documento_no_pdf_no_soportado(monkeypatch):
    _patch_media(monkeypatch, b"PKzip")
    message = {
        "type": "document",
        "document": {"id": "MID3", "mime_type": "application/zip", "filename": "x.zip"},
    }
    assert asyncio.run(main._build_media_content(message)) is None


def test_build_imagen_demasiado_grande(monkeypatch):
    big = b"x" * (main.MAX_IMAGE_BYTES + 1)
    _patch_media(monkeypatch, big)
    message = {"type": "image", "image": {"id": "MID4", "mime_type": "image/png"}}
    assert asyncio.run(main._build_media_content(message)) is None


def _patch_pipeline(monkeypatch):
    handled, sent = [], []

    async def fake_route(phone, content, store_text=None):
        handled.append((phone, content, store_text))
        return "ok"

    async def fake_send_text(to, text):
        sent.append((to, text))
        return {}

    monkeypatch.setattr(main.router, "route", fake_route)
    monkeypatch.setattr(main.wa, "send_text", fake_send_text)
    monkeypatch.setattr(main, "dedup", InMemoryDedupStore(ttl_seconds=60))
    return handled, sent


def test_process_image_llama_handle(monkeypatch):
    _patch_media(monkeypatch, JPEG_BYTES)
    handled, sent = _patch_pipeline(monkeypatch)
    message = {
        "id": "wamid.IMG",
        "from": "5215512345678",
        "type": "image",
        "image": {"id": "MID1", "mime_type": "image/jpeg"},
    }
    asyncio.run(main._process_message(message))
    assert len(handled) == 1
    phone, content, store_text = handled[0]
    assert isinstance(content, list) and content[0]["type"] == "image"
    assert store_text == "[imagen recibida]"
    assert len(sent) == 1


def test_process_audio_no_soportado(monkeypatch):
    handled, sent = _patch_pipeline(monkeypatch)
    message = {
        "id": "wamid.AUD",
        "from": "5215512345678",
        "type": "audio",
        "audio": {"id": "A1"},
    }
    asyncio.run(main._process_message(message))
    assert handled == []  # no se invoca al router
    assert len(sent) == 1  # se envía aviso
    assert "texto, imágenes y documentos PDF" in sent[0][1]
