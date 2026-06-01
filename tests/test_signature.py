"""Pruebas de la verificación de firma del webhook de WhatsApp."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.whatsapp import verify_signature

client = TestClient(app)

SECRET = "test-app-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_verify_signature_valida():
    body = b'{"hello":"world"}'
    assert verify_signature(body, _sign(body), SECRET) is True


def test_verify_signature_invalida():
    body = b'{"hello":"world"}'
    assert verify_signature(body, _sign(b"otro"), SECRET) is False


def test_verify_signature_header_ausente_o_malformado():
    body = b"{}"
    assert verify_signature(body, None, SECRET) is False
    assert verify_signature(body, "md5=abc", SECRET) is False


def test_webhook_acepta_firma_valida(monkeypatch):
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", SECRET)
    # Payload sin mensajes: no dispara procesamiento en segundo plano.
    raw = json.dumps({"entry": []}).encode()
    resp = client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={"X-Hub-Signature-256": _sign(raw), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"


def test_webhook_rechaza_firma_invalida(monkeypatch):
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", SECRET)
    raw = json.dumps({"entry": []}).encode()
    resp = client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={
            "X-Hub-Signature-256": "sha256=deadbeef",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_webhook_sin_secreto_omite_verificacion(monkeypatch):
    # Modo desarrollo: sin app secret, no se exige firma.
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", "")
    raw = json.dumps({"entry": []}).encode()
    resp = client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
