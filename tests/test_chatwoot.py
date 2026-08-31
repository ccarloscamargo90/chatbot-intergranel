"""Handoff a un asesor: el ciclo completo, sin red.

Lo que se prueba aquí no es "llama a Chatwoot", es que el cable no se cruce:
que el bot se calle cuando entra una persona, que lo que publica el propio bot
no se le devuelva al cliente como si fuera del asesor, que un reintento de
Chatwoot no le mande el mismo mensaje dos veces, y que un envío rechazado por
Meta no se dé por entregado.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.bus import InMemoryEventBus
from app.chatwoot import (
    ChatwootNoDisponible,
    MockChatwootClient,
    NullChatwootClient,
)
from app.dedup import InMemoryDedupStore
from app.handoff import HandoffStore

client = TestClient(main.app)

PHONE = "5215512345678"


def _run(agent, name, payload=None, phone=PHONE):
    return json.loads(asyncio.run(agent.run_tool(name, payload or {}, phone)))


# ------------------------------ Escalamiento ------------------------------ #
def test_escalar_abre_la_conversacion(soporte):
    data = _run(soporte, "escalar_a_humano", {"motivo": "reclamo por merma"})
    assert data["escalado"] is True
    assert len(soporte._chatwoot.conversaciones) == 1


def test_el_asesor_recibe_el_contexto_como_nota_privada(soporte):
    _run(soporte, "identificar_cliente", {"nombre": "Molinos del Bajío", "rfc": "MBA950101AB1"})
    _run(soporte, "escalar_a_humano", {"motivo": "quiere renegociar el precio"})

    conv = next(iter(soporte._chatwoot.conversaciones.values()))
    nota = conv.mensajes[0]
    assert nota["privado"] is True  # el cliente NO la ve
    assert "renegociar el precio" in nota["texto"]
    assert "MOLINOS DEL BAJÍO" in nota["texto"].upper()
    assert "MBA950101AB1" in nota["texto"]


def test_sin_identificar_la_nota_lo_dice(soporte):
    _run(soporte, "escalar_a_humano", {"motivo": "duda general"})
    nota = next(iter(soporte._chatwoot.conversaciones.values())).mensajes[0]
    assert "NO identificado" in nota["texto"]


def test_la_nota_lleva_el_historial_reciente(soporte):
    asyncio.run(
        soporte._history_store.save(
            soporte._history_key(PHONE),
            [
                {"role": "user", "content": "¿cuánto debo?"},
                {"role": "assistant", "content": "Su saldo es $204,500.00 MXN"},
                # Los bloques de tools son ruido para una persona: no van.
                {"role": "user", "content": [{"type": "tool_result", "content": "{}"}]},
            ],
        )
    )
    _run(soporte, "escalar_a_humano", {"motivo": "no entiende el saldo"})

    texto = next(iter(soporte._chatwoot.conversaciones.values())).mensajes[0]["texto"]
    assert "¿cuánto debo?" in texto
    assert "204,500.00" in texto
    assert "tool_result" not in texto


def test_queda_en_handoff(soporte):
    _run(soporte, "escalar_a_humano", {"motivo": "reclamo"})
    activo = asyncio.run(soporte._handoff.por_telefono(PHONE))
    assert activo is not None
    # Y se encuentra también por el otro lado, que es como llega el webhook.
    assert asyncio.run(soporte._handoff.por_conversacion(activo.conversacion_id)) is not None


# --------------------- Cuando el canal no está disponible ------------------ #
def test_sin_chatwoot_no_promete_un_asesor(soporte):
    """El escalamiento de antes decía 'un asesor continuará en breve' sin que
    nadie se enterara. Eso es peor que decir que no se pudo."""
    soporte._chatwoot = NullChatwootClient()
    data = _run(soporte, "escalar_a_humano", {"motivo": "reclamo"})

    assert data["escalado"] is False
    assert data["motivo"] == "canal_no_configurado"
    assert "NO le prometas" in data["instruccion"]
    assert asyncio.run(soporte._handoff.por_telefono(PHONE)) is None


def test_si_chatwoot_falla_lo_dice_y_no_deja_el_handoff_abierto(soporte):
    class Rota(MockChatwootClient):
        async def abrir_conversacion(self, telefono, nombre="", atributos=None):
            raise ChatwootNoDisponible("502 del reverse proxy")

    soporte._chatwoot = Rota()
    data = _run(soporte, "escalar_a_humano", {"motivo": "reclamo"})

    assert data["escalado"] is False
    assert data["motivo"] == "chatwoot_no_disponible"
    # Sin handoff: el bot sigue atendiendo en vez de dejarlo hablando al vacío.
    assert asyncio.run(soporte._handoff.por_telefono(PHONE)) is None


# ------------------------------ Ida: cliente -> Chatwoot ------------------- #
@pytest.fixture
def handoff_activo(monkeypatch):
    """Un teléfono ya en manos de un asesor, con todo cableado a mocks."""
    bus = InMemoryEventBus()
    cw = MockChatwootClient()
    store = HandoffStore(bus, ttl_segundos=3600)
    conv = asyncio.run(cw.abrir_conversacion(PHONE))
    asyncio.run(store.abrir(PHONE, conv.id))

    monkeypatch.setattr(main, "chatwoot", cw)
    monkeypatch.setattr(main, "handoff", store)
    monkeypatch.setattr(main, "dedup", InMemoryDedupStore(ttl_seconds=60))
    return cw, store, conv.id


def _mensaje(texto: str, wamid: str = "wamid.1") -> dict:
    return {"id": wamid, "from": PHONE, "type": "text", "text": {"body": texto}}


def test_en_handoff_el_bot_no_contesta(handoff_activo, monkeypatch):
    cw, _, conv_id = handoff_activo
    ruteados = []

    async def _no_deberia(*args, **kwargs):
        ruteados.append(args)
        raise AssertionError("el bot no debe atender a alguien que está con un asesor")

    monkeypatch.setattr(main.router, "route", _no_deberia)
    asyncio.run(main._process_message(_mensaje("¿ya lo revisaron?")))

    assert ruteados == []
    assert cw.conversaciones[conv_id].mensajes[-1] == {
        "tipo": "incoming",
        "privado": False,
        "texto": "¿ya lo revisaron?",
    }


def test_un_archivo_se_anuncia_aunque_no_se_suba(handoff_activo):
    cw, _, conv_id = handoff_activo
    asyncio.run(
        main._process_message(
            {
                "id": "wamid.IMG",
                "from": PHONE,
                "type": "image",
                "image": {"id": "M1", "mime_type": "image/jpeg", "caption": "la remisión"},
            }
        )
    )
    texto = cw.conversaciones[conv_id].mensajes[-1]["texto"]
    assert "archivo" in texto and "la remisión" in texto


def test_si_no_llega_a_la_bandeja_se_le_avisa_al_cliente(handoff_activo, monkeypatch):
    cw, _, _ = handoff_activo
    enviados = []

    async def _rota(conversacion_id, texto):
        raise ChatwootNoDisponible("timeout")

    async def _send_text(to, text):
        enviados.append(text)
        return {}

    monkeypatch.setattr(cw, "mensaje_del_cliente", _rota)
    monkeypatch.setattr(main.wa, "send_text", _send_text)
    asyncio.run(main._process_message(_mensaje("hola?")))

    assert len(enviados) == 1
    assert "no logramos entregar" in enviados[0].lower()


# ---------------------------- Vuelta: asesor -> cliente -------------------- #
def _webhook(conv_id: int, **campos) -> dict:
    cuerpo = {
        "event": "message_created",
        "id": 1,
        "content": "Ya lo estamos viendo, don Carlos.",
        "message_type": "outgoing",
        "private": False,
        "conversation": {"id": conv_id},
    }
    cuerpo.update(campos)
    return cuerpo


def test_la_respuesta_del_asesor_sale_por_whatsapp(handoff_activo, monkeypatch):
    _, _, conv_id = handoff_activo
    enviados = []

    async def _send_text(to, text):
        enviados.append((to, text))
        return {}

    monkeypatch.setattr(main.wa, "send_text", _send_text)
    resp = client.post("/webhooks/chatwoot", json=_webhook(conv_id))

    assert resp.json()["status"] == "sent"
    assert enviados == [(PHONE, "Ya lo estamos viendo, don Carlos.")]


def test_lo_que_publica_el_bot_no_se_le_devuelve_al_cliente(handoff_activo, monkeypatch):
    """El bot publica en Chatwoot lo que dice el cliente; Chatwoot avisa de cada
    mensaje. Sin este filtro, cada mensaje del cliente le rebotaría de vuelta."""
    _, _, conv_id = handoff_activo
    enviados = []
    monkeypatch.setattr(main.wa, "send_text", lambda to, text: enviados.append(text))

    resp = client.post("/webhooks/chatwoot", json=_webhook(conv_id, message_type="incoming"))
    assert resp.json()["status"] == "ignored"
    assert enviados == []


def test_las_notas_privadas_no_salen(handoff_activo, monkeypatch):
    _, _, conv_id = handoff_activo
    enviados = []
    monkeypatch.setattr(main.wa, "send_text", lambda to, text: enviados.append(text))

    resp = client.post("/webhooks/chatwoot", json=_webhook(conv_id, private=True))
    assert resp.json()["status"] == "ignored"
    assert enviados == []


def test_un_reintento_no_manda_el_mensaje_dos_veces(handoff_activo, monkeypatch):
    _, _, conv_id = handoff_activo
    enviados = []

    async def _send_text(to, text):
        enviados.append(text)
        return {}

    monkeypatch.setattr(main.wa, "send_text", _send_text)
    assert client.post("/webhooks/chatwoot", json=_webhook(conv_id)).json()["status"] == "sent"
    assert (
        client.post("/webhooks/chatwoot", json=_webhook(conv_id)).json()["status"] == "duplicate"
    )
    assert len(enviados) == 1


def test_si_meta_rechaza_no_se_da_por_entregado(handoff_activo, monkeypatch):
    """Fuera de la ventana de 24h Meta rechaza el texto libre. El asesor lo ve
    entregado en Chatwoot y no lo está: hay que decírselo en su bandeja."""
    cw, _, conv_id = handoff_activo

    async def _rechaza(to, text):
        raise RuntimeError("(#131047) Message failed to send")

    monkeypatch.setattr(main.wa, "send_text", _rechaza)
    resp = client.post("/webhooks/chatwoot", json=_webhook(conv_id))

    assert resp.json()["status"] == "failed"
    nota = cw.conversaciones[conv_id].mensajes[-1]
    assert nota["privado"] is True
    assert "NO entregó" in nota["texto"]
    # Y se suelta el candado: el reintento de Chatwoot tiene que poder pasar.
    resp2 = client.post("/webhooks/chatwoot", json=_webhook(conv_id))
    assert resp2.json()["status"] == "failed"


def test_resolver_la_conversacion_le_devuelve_el_control_al_bot(handoff_activo, monkeypatch):
    _, store, conv_id = handoff_activo
    enviados = []

    async def _send_reply(to, reply):
        enviados.append(reply)
        return {}

    monkeypatch.setattr(main.wa, "send_reply", _send_reply)
    resp = client.post(
        "/webhooks/chatwoot",
        json={"event": "conversation_status_changed", "id": conv_id, "status": "resolved"},
    )

    assert resp.json()["status"] == "closed"
    assert asyncio.run(store.por_telefono(PHONE)) is None
    # Y el cliente recupera los botones para seguir solo.
    assert enviados and enviados[0].botones


def test_una_conversacion_ajena_se_ignora(handoff_activo, monkeypatch):
    enviados = []
    monkeypatch.setattr(main.wa, "send_text", lambda to, text: enviados.append(text))
    resp = client.post("/webhooks/chatwoot", json=_webhook(999999))
    assert resp.json()["status"] == "ignored"
    assert enviados == []


def test_el_webhook_exige_el_secreto(handoff_activo, monkeypatch):
    _, _, conv_id = handoff_activo
    monkeypatch.setattr(main.settings, "chatwoot_webhook_secret", "s3creto")

    assert client.post("/webhooks/chatwoot", json=_webhook(conv_id)).status_code == 401
    assert (
        client.post(
            "/webhooks/chatwoot",
            headers={"X-Webhook-Secret": "incorrecto"},
            json=_webhook(conv_id),
        ).status_code
        == 401
    )


def test_el_secreto_tambien_vale_por_query(handoff_activo, monkeypatch):
    """La UI de Chatwoot solo deja capturar una URL, así que el query param es
    la vía práctica."""
    _, _, conv_id = handoff_activo
    monkeypatch.setattr(main.settings, "chatwoot_webhook_secret", "s3creto")

    async def _send_text(to, text):
        return {}

    monkeypatch.setattr(main.wa, "send_text", _send_text)
    resp = client.post("/webhooks/chatwoot?secret=s3creto", json=_webhook(conv_id))
    assert resp.json()["status"] == "sent"


def test_tras_escalar_la_despedida_va_sin_botones(soporte):
    """Un menú que ya no manda a ningún lado es peor que ningún menú: tocarlo
    solo reenviaría el toque a la bandeja del asesor."""
    _run(soporte, "escalar_a_humano", {"motivo": "reclamo"})
    reply = asyncio.run(soporte.decorate(PHONE, "Enseguida lo atiende un asesor."))
    assert reply.botones == []
    assert reply.lista is None


def test_sin_handoff_los_botones_siguen(soporte):
    reply = asyncio.run(soporte.decorate(PHONE, "Aquí tiene."))
    assert reply.botones
