"""El diagnóstico de Chatwoot: que diga QUÉ falló, en palabras, sin red.

Chatwoot se simula con `httpx.MockTransport`, con las respuestas que da la
v4.17.0 (perfil, bandejas, búsqueda de contactos y webhooks). Lo que se prueba
es que cada error típico de configuración salga nombrado —y con qué hacer—, que
nada se marque ✅ sin haberlo comprobado, y que nunca se imprima un secreto.
"""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.chatwoot import ChatwootNoDisponible, HTTPChatwootClient
from app.chatwoot_diagnostico import FALLA, OK, como_texto, diagnosticar
from app.config import Settings

BASE = "https://chatwoot.intergranel.test"
TOKEN = "tok-usuario-123"
SECRETO = "s3creto-largo"
HOST_BOT = "bot.intergranel.test"
URL_WEBHOOK = f"https://{HOST_BOT}/webhooks/chatwoot?secret={SECRETO}"

BOT_BLOQUEADO = {"error": "Access to this endpoint is not authorized for bots"}


def _settings(**cambios) -> Settings:
    datos = {
        "chatwoot_base_url": BASE,
        "chatwoot_api_token": TOKEN,
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 7,
        "chatwoot_webhook_secret": SECRETO,
    }
    datos.update(cambios)
    return Settings(_env_file=None, **datos)


def _chatwoot(
    *,
    perfil=None,
    rol="administrator",
    inbox_tipo="Channel::Api",
    inbox_status=200,
    busqueda_status=200,
    webhooks=None,
):
    """Un Chatwoot de mentiras que contesta como la v4.17.0."""
    if webhooks is None:
        webhooks = [
            {
                "id": 3,
                "name": "Bot WhatsApp",
                "url": URL_WEBHOOK,
                "subscriptions": ["message_created", "conversation_status_changed"],
                "secret": "firma-de-chatwoot",
            }
        ]
    vistas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(request.url.path)
        assert request.headers["api_access_token"] == TOKEN
        ruta = request.url.path
        if ruta == "/api/v1/profile":
            if perfil is not None:
                return perfil
            return httpx.Response(
                200,
                json={
                    "name": "Carlos",
                    "accounts": [{"id": 1, "name": "Intergranel", "role": rol}],
                },
            )
        if ruta == "/api/v1/accounts/1/inboxes/7":
            if inbox_status != 200:
                return httpx.Response(inbox_status, json={"error": "You are not authorized"})
            return httpx.Response(
                200, json={"id": 7, "name": "Asesores WhatsApp", "channel_type": inbox_tipo}
            )
        if ruta == "/api/v1/accounts/1/inboxes":
            return httpx.Response(
                200,
                json={"payload": [{"id": 2, "name": "Web", "channel_type": "Channel::WebWidget"}]},
            )
        if ruta == "/api/v1/accounts/1/contacts/search":
            return httpx.Response(busqueda_status, json={"payload": []})
        if ruta == "/api/v1/accounts/1/webhooks":
            return httpx.Response(200, json={"payload": {"webhooks": webhooks}})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler), vistas


def _diagnosticar(settings=None, transport=None, host=HOST_BOT):
    return asyncio.run(
        diagnosticar(settings or _settings(), host_publico=host, transport=transport)
    )


def _fallas(chequeos) -> list[str]:
    return [c.texto for c in chequeos if c.estado == FALLA]


# ------------------------------- Todo bien -------------------------------- #
def test_con_todo_bien_el_veredicto_es_listo():
    transport, vistas = _chatwoot()
    chequeos = _diagnosticar(transport=transport)

    assert _fallas(chequeos) == []
    texto = como_texto(chequeos)
    assert "RESULTADO: ✅ listo" in texto
    assert "administrador" in texto
    assert "Asesores WhatsApp" in texto
    # Revisó lo mismo que hace el bot al escalar: buscar contactos.
    assert "/api/v1/accounts/1/contacts/search" in vistas


def test_nunca_imprime_el_token_ni_los_secretos():
    """La página se comparte en capturas de pantalla."""
    transport, _ = _chatwoot()
    texto = como_texto(_diagnosticar(transport=transport))
    assert TOKEN not in texto
    assert SECRETO not in texto
    assert "firma-de-chatwoot" not in texto


# --------------------------- Configuración local -------------------------- #
def test_sin_direccion_lo_dice_y_no_llama_a_nadie():
    chequeos = _diagnosticar(_settings(chatwoot_base_url=""))
    assert "CHATWOOT_BASE_URL está vacía" in _fallas(chequeos)[0]
    # Y le recuerda qué recibe hoy el cliente.
    assert "446 131 2914" in como_texto(chequeos)


def test_el_chatwoot_simulado_no_cuenta_como_configurado():
    assert "SIMULADO" in _fallas(_diagnosticar(_settings(chatwoot_base_url="mock")))[0]


def test_nombra_las_variables_que_faltan():
    chequeos = _diagnosticar(_settings(chatwoot_api_token="", chatwoot_inbox_id=0))
    assert "CHATWOOT_API_TOKEN, CHATWOOT_INBOX_ID" in _fallas(chequeos)[0]


# -------------------------------- El token -------------------------------- #
def test_un_token_de_agent_bot_se_nombra_y_se_dice_cual_usar():
    """El error más fácil de cometer: la guía anterior pedía un token de bot."""
    transport, _ = _chatwoot(perfil=httpx.Response(401, json=BOT_BLOQUEADO))
    falla = _fallas(_diagnosticar(transport=transport))[0]
    assert "AGENT BOT" in falla
    assert "Token de acceso" in falla


def test_un_token_invalido_se_dice_como_tal():
    transport, _ = _chatwoot(perfil=httpx.Response(401, json={"error": "Invalid Access Token"}))
    falla = _fallas(_diagnosticar(transport=transport))[0]
    assert "rechazó el token" in falla
    assert "Invalid Access Token" in falla


def test_una_direccion_que_no_es_chatwoot():
    transport, _ = _chatwoot(perfil=httpx.Response(200, text="<html>Railway</html>"))
    assert "no responde como Chatwoot" in _fallas(_diagnosticar(transport=transport))[0]


def test_si_no_conecta_lo_dice():
    def caido(request):
        raise httpx.ConnectError("Name or service not known")

    falla = _fallas(_diagnosticar(transport=httpx.MockTransport(caido)))[0]
    assert "No se pudo conectar" in falla


def test_una_cuenta_equivocada_muestra_las_correctas():
    falla = _fallas(_diagnosticar(_settings(chatwoot_account_id=9), _chatwoot()[0]))[0]
    assert "no pertenece a la cuenta 9" in falla
    assert "1 (Intergranel)" in falla


def test_un_agente_funciona_pero_se_advierte():
    transport, vistas = _chatwoot(rol="agent")
    chequeos = _diagnosticar(transport=transport)
    texto = como_texto(chequeos)
    assert "AGENTE" in texto
    # Un agente no puede listar webhooks: no se consulta ni se da por bueno.
    assert "/api/v1/accounts/1/webhooks" not in vistas
    assert "No se pudo revisar el webhook" in texto


# ------------------------------- La bandeja ------------------------------- #
def test_una_bandeja_de_whatsapp_no_sirve():
    transport, _ = _chatwoot(inbox_tipo="Channel::Whatsapp")
    falla = _fallas(_diagnosticar(transport=transport))[0]
    assert "tiene que ser de tipo API" in falla


def test_una_bandeja_que_no_ve_lista_las_que_si():
    transport, _ = _chatwoot(inbox_status=401)
    falla = _fallas(_diagnosticar(transport=transport))[0]
    assert "CHATWOOT_INBOX_ID" in falla
    assert "2 «Web» (WebWidget)" in falla


def test_sin_permiso_de_contactos_falla():
    transport, _ = _chatwoot(busqueda_status=401)
    fallas = _fallas(_diagnosticar(transport=transport))
    assert any("no puede buscar contactos" in f for f in fallas)


# -------------------------------- El webhook ------------------------------ #
def test_sin_webhook_da_la_url_exacta_sin_el_secreto():
    transport, _ = _chatwoot(webhooks=[])
    falla = _fallas(_diagnosticar(transport=transport))[0]
    assert f"https://{HOST_BOT}/webhooks/chatwoot?secret=<su CHATWOOT_WEBHOOK_SECRET>" in falla
    assert "conversation_status_changed" in falla


def _webhook(**cambios) -> dict:
    base = {
        "id": 3,
        "name": "Bot WhatsApp",
        "url": URL_WEBHOOK,
        "subscriptions": ["message_created", "conversation_status_changed"],
    }
    base.update(cambios)
    return base


@pytest.mark.parametrize(
    ("webhook", "esperado"),
    [
        (_webhook(url=f"https://{HOST_BOT}/webhooks/chatwoot?secret=otro"), "no coincide"),
        (_webhook(url=f"https://{HOST_BOT}/webhooks/chatwoot"), "no coincide"),
        (_webhook(subscriptions=["message_created"]), "conversation_status_changed"),
        (
            _webhook(url=f"https://viejo.up.railway.app/webhooks/chatwoot?secret={SECRETO}"),
            "apunta a",
        ),
        (_webhook(url=f"http://{HOST_BOT}/webhooks/chatwoot?secret={SECRETO}"), "https://"),
        (_webhook(inbox={"id": 2, "name": "Web"}), "solo avisa de la bandeja"),
    ],
)
def test_cada_error_del_webhook_se_nombra(webhook, esperado):
    transport, _ = _chatwoot(webhooks=[webhook])
    fallas = _fallas(_diagnosticar(transport=transport))
    assert any(esperado in f for f in fallas), fallas


def test_sin_secreto_configurado_es_una_falla():
    transport, _ = _chatwoot()
    fallas = _fallas(_diagnosticar(_settings(chatwoot_webhook_secret=""), transport))
    assert any("CHATWOOT_WEBHOOK_SECRET está vacío" in f for f in fallas)


def test_ignora_webhooks_de_otras_integraciones():
    transport, _ = _chatwoot(
        webhooks=[_webhook(id=9, name="Zapier", url="https://hooks.zapier.com/x"), _webhook()]
    )
    chequeos = _diagnosticar(transport=transport)
    assert _fallas(chequeos) == []
    assert not any("Zapier" in c.texto for c in chequeos if c.estado == OK)


# ------------------------------ La página --------------------------------- #
@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setattr(main, "settings", _settings(chatwoot_base_url=""))
    return TestClient(main.app)


def test_la_pagina_exige_el_mismo_secreto_que_el_webhook(cliente):
    assert cliente.get("/diagnostico/chatwoot").status_code == 401
    assert cliente.get("/diagnostico/chatwoot?secret=otro").status_code == 401


def test_la_pagina_se_lee_como_texto(cliente):
    resp = cliente.get(f"/diagnostico/chatwoot?secret={SECRETO}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.startswith("Diagnóstico de Chatwoot")
    assert "CHATWOOT_BASE_URL está vacía" in resp.text


# ------------------ El motivo real llega al log del escalamiento ---------- #
def test_el_escalamiento_fallido_guarda_el_motivo_de_chatwoot():
    """Antes el log decía "401 Unauthorized" y ya: ahora dice por qué."""

    def handler(request):
        return httpx.Response(401, json=BOT_BLOQUEADO)

    cliente = HTTPChatwootClient(
        BASE, api_token=TOKEN, account_id=1, inbox_id=7, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ChatwootNoDisponible) as exc:
        asyncio.run(cliente.abrir_conversacion("5214461234567"))
    assert "not authorized for bots" in str(exc.value)
