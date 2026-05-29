"""Pruebas de los endpoints HTTP (sin llamadas a Claude ni a la red).

Usan el ERP simulado y WhatsApp en modo desarrollo (registra en vez de enviar),
por lo que no requieren credenciales.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["erp"] == "mock"


def test_whatsapp_verify_ok():
    resp = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "intergranel-verify",
            "hub.challenge": "12345",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "12345"


def test_whatsapp_verify_rejects_bad_token():
    resp = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "incorrecto",
            "hub.challenge": "12345",
        },
    )
    assert resp.status_code == 403


def test_order_update_notification():
    resp = client.post(
        "/webhooks/erp/order-update",
        json={
            "order_id": "OC-1001",
            "telefono": "5215512345678",
            "estado_nuevo": "en_ruta",
            "cliente": "Molinos del Bajío",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    # En modo desarrollo el cliente de WhatsApp devuelve el payload que enviaría.
    assert "OC-1001" in body["result"]["payload"]["text"]["body"]


def test_order_update_rejects_bad_secret(monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "erp_webhook_secret", "secreto")
    resp = client.post(
        "/webhooks/erp/order-update",
        headers={"X-Webhook-Secret": "incorrecto"},
        json={
            "order_id": "OC-1001",
            "telefono": "5215512345678",
            "estado_nuevo": "en_ruta",
        },
    )
    assert resp.status_code == 401
