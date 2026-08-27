"""Avisos internos del ERP al equipo por WhatsApp.

Sin red: WhatsApp corre en modo desarrollo (registra en vez de enviar) y el bus
es el de memoria.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main
from app.bus import get_event_bus
from app.main import app
from app.models import ErpAvisoEvent
from app.notifications import build_aviso_message, notify_erp_aviso

client = TestClient(app)


def aviso(**over) -> dict:
    base = {
        "id": "aviso-1",
        "tipo": "calendario.objetivo",
        "telefono": "5215512345678",
        "titulo": "Vence hoy: IMSS — Intergranel",
        "mensaje": "“IMSS” de Intergranel se entrega HOY (2026-08-25).",
        "url": "https://erp.example.com/calendario",
        "referencia": "calendario_entrega:e1",
        "empresa": "Intergranel",
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _sin_plantilla(monkeypatch):
    """Por default, texto libre: es el camino de desarrollo."""
    monkeypatch.setattr(main.settings, "whatsapp_aviso_template", "")
    monkeypatch.setattr(main.settings, "erp_webhook_secret", "")


# --------------------------------------------------------------------------- #
# Redacción del mensaje
# --------------------------------------------------------------------------- #
def test_mensaje_lleva_titulo_detalle_y_liga():
    texto = build_aviso_message(ErpAvisoEvent(**aviso()))

    assert "*Vence hoy: IMSS — Intergranel*" in texto
    assert "se entrega HOY" in texto
    assert "https://erp.example.com/calendario" in texto
    assert "Intergranel · ERP" in texto


def test_mensaje_sin_liga_no_inventa_una():
    texto = build_aviso_message(ErpAvisoEvent(**aviso(url=None)))
    assert "Resuélvelo aquí" not in texto


def test_emoji_distingue_lo_vencido_de_lo_que_vence_hoy():
    # Cuando llegan varios en la mañana, el emoji es lo que se lee primero.
    vencido = build_aviso_message(ErpAvisoEvent(**aviso(tipo="calendario.atraso")))
    hoy = build_aviso_message(ErpAvisoEvent(**aviso(tipo="calendario.objetivo")))
    assert vencido.startswith("🔴")
    assert hoy.startswith("📅")


def test_emoji_de_un_tipo_nuevo_hereda_el_de_su_modulo():
    # Un módulo que registre "pago.lo_que_sea" no necesita tocar el mapa.
    texto = build_aviso_message(ErpAvisoEvent(**aviso(tipo="pago.tipo_que_no_existe_aun")))
    assert texto.startswith("💳")


def test_emoji_desconocido_cae_en_el_generico():
    texto = build_aviso_message(ErpAvisoEvent(**aviso(tipo="modulo_inventado.algo")))
    assert texto.startswith("🔔")


# --------------------------------------------------------------------------- #
# Plantilla vs texto libre (ventana de 24h de Meta)
# --------------------------------------------------------------------------- #
def test_usa_la_plantilla_aprobada_cuando_esta_configurada(monkeypatch):
    # Un vencimiento avisado a las 7:30 casi nunca cae dentro de la ventana de
    # 24h: sin plantilla, Meta rechaza el mensaje.
    monkeypatch.setattr(main.settings, "whatsapp_aviso_template", "erp_aviso")
    llamadas = {}

    async def fake_send_template(to, template_name, language, body_params=None):
        llamadas.update(
            to=to, template_name=template_name, language=language, body_params=body_params
        )
        return {"messages": [{"id": "wamid.PLANTILLA"}]}

    monkeypatch.setattr(main.wa, "send_template", fake_send_template)

    asyncio.run(notify_erp_aviso(main.wa, ErpAvisoEvent(**aviso())))

    assert llamadas["template_name"] == "erp_aviso"
    assert llamadas["to"] == "5215512345678"
    titulo, detalle, empresa = llamadas["body_params"]
    assert titulo == "Vence hoy: IMSS — Intergranel"
    assert "https://erp.example.com/calendario" in detalle
    assert empresa == "Intergranel"


def test_sin_plantilla_manda_texto_libre(monkeypatch):
    enviados = []

    async def fake_send_text(to, text):
        enviados.append((to, text))
        return {"messages": [{"id": "wamid.TEXTO"}]}

    monkeypatch.setattr(main.wa, "send_text", fake_send_text)

    asyncio.run(notify_erp_aviso(main.wa, ErpAvisoEvent(**aviso())))

    assert len(enviados) == 1
    assert enviados[0][0] == "5215512345678"


# --------------------------------------------------------------------------- #
# Webhook
# --------------------------------------------------------------------------- #
def test_webhook_envia_publica_en_el_bus_y_devuelve_el_wamid(monkeypatch):
    async def fake_send_text(to, text):
        return {"messages": [{"id": "wamid.ABC"}]}

    monkeypatch.setattr(main.wa, "send_text", fake_send_text)

    resp = client.post("/webhooks/erp/notificacion", json=aviso(id="aviso-bus"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    # El ERP guarda el wamid en su outbox para poder rastrear el mensaje.
    assert body["wamid"] == "wamid.ABC"

    bus = get_event_bus()
    # El detalle queda para el agente…
    evento = asyncio.run(bus.read("bus:erp:aviso:aviso-bus"))
    assert evento is not None and evento["tipo"] == "calendario.objetivo"
    # …y también "lo último que le mandamos a este teléfono", que es lo que el
    # agente necesita si la persona contesta "¿cuál vencimiento?".
    reciente = asyncio.run(bus.read("bus:erp:aviso_reciente:5215512345678"))
    assert reciente is not None and reciente["id"] == "aviso-bus"


def test_webhook_no_manda_dos_veces_el_mismo_aviso(monkeypatch):
    # El worker del ERP reintenta ante timeouts; la persona no puede recibir el
    # mismo vencimiento dos veces.
    enviados = []

    async def fake_send_text(to, text):
        enviados.append(to)
        return {"messages": [{"id": "wamid.DUP"}]}

    monkeypatch.setattr(main.wa, "send_text", fake_send_text)

    primero = client.post("/webhooks/erp/notificacion", json=aviso(id="aviso-dup"))
    segundo = client.post("/webhooks/erp/notificacion", json=aviso(id="aviso-dup"))

    assert primero.json()["status"] == "sent"
    assert segundo.json()["status"] == "duplicate"
    assert len(enviados) == 1


def test_webhook_rechaza_secreto_invalido(monkeypatch):
    monkeypatch.setattr(main.settings, "erp_webhook_secret", "secreto")

    resp = client.post(
        "/webhooks/erp/notificacion",
        headers={"X-Webhook-Secret": "incorrecto"},
        json=aviso(id="aviso-401"),
    )

    assert resp.status_code == 401


def test_webhook_acepta_el_secreto_correcto(monkeypatch):
    monkeypatch.setattr(main.settings, "erp_webhook_secret", "secreto")

    resp = client.post(
        "/webhooks/erp/notificacion",
        headers={"X-Webhook-Secret": "secreto"},
        json=aviso(id="aviso-ok"),
    )

    assert resp.status_code == 200


def test_webhook_exige_los_campos_minimos():
    # Sin teléfono no hay a quién mandarle: mejor 422 que un mensaje al vacío.
    resp = client.post("/webhooks/erp/notificacion", json={"id": "x", "tipo": "y"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Reintentos: un envío fallido NO puede quedar sellado como duplicado
# --------------------------------------------------------------------------- #
def test_un_fallo_libera_la_marca_para_que_el_reintento_si_salga(monkeypatch):
    """El bug que se llevó una tarde: `is_duplicate` marca ANTES de mandar.

    Si el envío falla y la marca se queda puesta, el reintento del ERP entra por
    la rama de "duplicate" —que el ERP trata como entrega buena— y el aviso
    queda marcado ENVIADO sin que nadie lo haya recibido, sin más reintentos.
    """
    intentos = []

    async def falla_la_primera(to, text):
        intentos.append(to)
        if len(intentos) == 1:
            raise RuntimeError("Meta rechazó el mensaje")
        return {"messages": [{"id": "wamid.SEGUNDO"}]}

    monkeypatch.setattr(main.wa, "send_text", falla_la_primera)

    primero = client.post("/webhooks/erp/notificacion", json=aviso(id="aviso-reintento"))
    segundo = client.post("/webhooks/erp/notificacion", json=aviso(id="aviso-reintento"))

    assert primero.json()["status"] == "failed"
    # Lo importante: el reintento NO se descarta como duplicado.
    assert segundo.json()["status"] == "sent"
    assert segundo.json()["wamid"] == "wamid.SEGUNDO"
    assert len(intentos) == 2


def test_una_entrega_buena_si_sella_contra_duplicados(monkeypatch):
    # El candado sigue vivo para el caso que sí importa: el ERP reintenta ante
    # timeouts y nadie puede recibir el mismo vencimiento dos veces.
    enviados = []

    async def fake_send_text(to, text):
        enviados.append(to)
        return {"messages": [{"id": "wamid.OK"}]}

    monkeypatch.setattr(main.wa, "send_text", fake_send_text)

    client.post("/webhooks/erp/notificacion", json=aviso(id="aviso-sellado"))
    segundo = client.post("/webhooks/erp/notificacion", json=aviso(id="aviso-sellado"))

    assert segundo.json()["status"] == "duplicate"
    assert len(enviados) == 1


def test_el_motivo_de_meta_llega_al_erp(monkeypatch):
    """Sin esto, la bandeja solo dice "Request failed with status code 500"."""

    class RespuestaMeta:
        status_code = 404
        text = "…"

        def json(self):
            return {
                "error": {
                    "message": "(#132001) Template name does not exist in the translation",
                    "error_data": {"details": "template name (erp_aviso) does not exist in es_MX"},
                }
            }

    class ErrorHttp(Exception):
        response = RespuestaMeta()

    async def rechaza(to, text):
        raise ErrorHttp()

    monkeypatch.setattr(main.wa, "send_text", rechaza)

    resp = client.post("/webhooks/erp/notificacion", json=aviso(id="aviso-motivo"))

    body = resp.json()
    assert body["status"] == "failed"
    assert "132001" in body["error"]
    assert "es_MX" in body["error"]
    assert body["error"].startswith("Meta 404:")


def test_un_fallo_sin_respuesta_de_meta_igual_explica_algo(monkeypatch):
    async def revienta(to, text):
        raise ConnectionError("se cayó la red")

    monkeypatch.setattr(main.wa, "send_text", revienta)

    resp = client.post("/webhooks/erp/notificacion", json=aviso(id="aviso-sin-respuesta"))

    assert resp.json()["error"] == "ConnectionError: se cayó la red"

