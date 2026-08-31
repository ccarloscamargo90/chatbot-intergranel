"""Cotización de fletes por WhatsApp (ERP · BUG-77).

Sin red: WhatsApp en modo desarrollo, bus en memoria, ERP mock y la
interpretación monkeypatcheada — nunca se llama a Claude.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main
from app.bus import InMemoryEventBus
from app.dedup import InMemoryDedupStore
from app.erp import MockERPClient
from app.fletes import (
    FletesPendientes,
    acuse,
    es_solicitud_de_flete,
    parsear_interpretacion,
    referencia_de,
)
from app.main import app
from app.models import InterpretacionFlete

client = TestClient(app)

MARCA_PROHIBIDA = "grancore"


def solicitud(**over) -> dict:
    """Lo que el ERP manda al pedirle precio a un transportista (77.4)."""
    base = {
        "id": "envio-1",
        "tipo": "cotizacion_flete.solicitud",
        "telefono": "5216671234567",
        "titulo": "Solicitud de cotización de flete",
        "mensaje": (
            "Buen día. Le escribimos de Intergranel Comercializadora S.A. de C.V.\n\n"
            "Queremos cotizar un flete:\n"
            "• Ruta: Los Mochis → Guadalajara\n"
            "• Carga: 30 toneladas\n\n"
            "¿Nos puede pasar su precio?\n\n"
            "Referencia: SCF-2026-0001"
        ),
        "referencia": "cotizacion_flete:c1",
        "empresa": None,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setattr(main.settings, "whatsapp_aviso_template", "")
    monkeypatch.setattr(main.settings, "erp_webhook_secret", "")
    # ERP, pendientes y deduplicación frescos por prueba: el store de dedup es
    # de módulo, así que sin esto el segundo caso con el mismo id de aviso se
    # trataría como reenvío y no saldría nada.
    monkeypatch.setattr(main, "erp", MockERPClient())
    monkeypatch.setattr(main, "fletes", FletesPendientes(InMemoryEventBus()))
    monkeypatch.setattr(main, "dedup", InMemoryDedupStore(ttl_seconds=3600))


@pytest.fixture
def enviados(monkeypatch) -> list[tuple[str, str]]:
    """Lo que sale por WhatsApp, sin tocar la red."""
    salidas: list[tuple[str, str]] = []

    async def _send_text(to: str, body: str) -> dict:
        salidas.append((to, body))
        return {"messages": [{"id": "wamid.out"}]}

    monkeypatch.setattr(main.wa, "send_text", _send_text)
    return salidas


def interpretacion_fija(monkeypatch, valor):
    async def _interpretar(_client, _modelo, _texto):
        return valor

    monkeypatch.setattr(main, "interpretar_respuesta", _interpretar)


def mensaje_entrante(texto: str, *, wamid: str = "wamid.in", telefono: str = "5216671234567"):
    return {
        "from": telefono,
        "id": wamid,
        "type": "text",
        "text": {"body": texto},
    }


# ── Salida: el mensaje al transportista ──────────────────────────────────── #


def test_la_solicitud_de_flete_no_es_un_aviso_interno():
    assert es_solicitud_de_flete("cotizacion_flete.solicitud")
    assert not es_solicitud_de_flete("calendario.objetivo")


def test_sale_el_texto_del_erp_tal_cual(enviados):
    resp = client.post("/webhooks/erp/notificacion", json=solicitud())

    assert resp.json()["status"] == "sent"
    destino, cuerpo = enviados[0]
    assert destino == "5216671234567"
    # Sin plantilla, sin título anexado, sin firma del bot: el ERP ya lo compuso.
    assert cuerpo == solicitud()["mensaje"]


def test_regla_de_oro_el_bot_no_le_agrega_marca_al_proveedor(enviados, monkeypatch):
    # El aviso interno firma con `empresa or company_name`. Si la solicitud de
    # flete pasara por ahí, ese fallback volvería a meter la marca comercial
    # que el ERP le quitó a propósito antes de mandársela a un proveedor.
    monkeypatch.setattr(main.settings, "company_name", "GRANCORE Maquinaria")

    client.post("/webhooks/erp/notificacion", json=solicitud())

    _, cuerpo = enviados[0]
    assert MARCA_PROHIBIDA not in cuerpo.lower()


def test_un_aviso_interno_sigue_yendo_por_su_camino(enviados):
    resp = client.post(
        "/webhooks/erp/notificacion",
        json=solicitud(id="aviso-9", tipo="calendario.objetivo", empresa="Intergranel"),
    )

    assert resp.json()["status"] == "sent"
    _, cuerpo = enviados[0]
    # El de siempre lleva su título; el de flete no.
    assert "Vence" in cuerpo or "Solicitud de cotización" in cuerpo


def test_marcar_el_telefono_es_lo_que_permite_amarrar_la_respuesta(enviados):
    client.post("/webhooks/erp/notificacion", json=solicitud())

    pendiente = asyncio.run(main.fletes.obtener("5216671234567"))
    assert pendiente is not None
    assert pendiente.referencia == "cotizacion_flete:c1"


# ── Entrada: lo que contesta el transportista ────────────────────────────── #


def test_la_respuesta_se_deposita_con_su_texto_crudo(enviados, monkeypatch):
    client.post("/webhooks/erp/notificacion", json=solicitud())
    interpretacion_fija(
        monkeypatch,
        InterpretacionFlete(montoCentavos=2_800_000, unidad="POR_VIAJE"),
    )

    asyncio.run(main._process_message(mensaje_entrante("te lo hago en 28 mil el viaje")))

    depositado = main.erp.respuestas_flete[0]
    assert depositado["texto"] == "te lo hago en 28 mil el viaje"
    assert depositado["referencia"] == "cotizacion_flete:c1"
    assert depositado["interpretacion"].montoCentavos == 2_800_000


def test_lo_que_no_se_entiende_se_deposita_igual(enviados, monkeypatch):
    client.post("/webhooks/erp/notificacion", json=solicitud())
    interpretacion_fija(monkeypatch, None)

    asyncio.run(main._process_message(mensaje_entrante("ahí luego te digo")))

    # Que el transportista contestó es un hecho, aunque nadie lo haya leído.
    assert len(main.erp.respuestas_flete) == 1
    assert main.erp.respuestas_flete[0]["interpretacion"] is None


def test_con_precio_la_pregunta_se_cierra(enviados, monkeypatch):
    client.post("/webhooks/erp/notificacion", json=solicitud())
    interpretacion_fija(
        monkeypatch, InterpretacionFlete(montoCentavos=2_800_000, unidad="POR_VIAJE")
    )

    asyncio.run(main._process_message(mensaje_entrante("28 mil el viaje")))

    assert asyncio.run(main.fletes.obtener("5216671234567")) is None


def test_declinar_tambien_cierra_la_pregunta(enviados, monkeypatch):
    client.post("/webhooks/erp/notificacion", json=solicitud())
    interpretacion_fija(monkeypatch, InterpretacionFlete(declina=True))

    asyncio.run(main._process_message(mensaje_entrante("ahorita no puedo")))

    assert asyncio.run(main.fletes.obtener("5216671234567")) is None


def test_sin_precio_el_telefono_sigue_marcado(enviados, monkeypatch):
    client.post("/webhooks/erp/notificacion", json=solicitud())
    interpretacion_fija(monkeypatch, None)

    asyncio.run(main._process_message(mensaje_entrante("¿cuántas toneladas son?")))

    # Sigue en la misma conversación: no es una consulta nueva.
    assert asyncio.run(main.fletes.obtener("5216671234567")) is not None


def test_sin_pregunta_pendiente_el_mensaje_va_al_router(monkeypatch):
    ruteados: list[str] = []

    async def _route(phone, content, store_text=None):
        ruteados.append(content)
        from app.replies import Reply

        return Reply("ok")

    async def _send_reply(to, reply):
        return {}

    monkeypatch.setattr(main.router, "route", _route)
    monkeypatch.setattr(main.wa, "send_reply", _send_reply)

    asyncio.run(main._process_message(mensaje_entrante("¿a cómo el maíz?")))

    assert ruteados == ["¿a cómo el maíz?"]
    assert main.erp.respuestas_flete == []


def test_si_el_erp_falla_el_transportista_recibe_respuesta(enviados, monkeypatch):
    client.post("/webhooks/erp/notificacion", json=solicitud())
    interpretacion_fija(monkeypatch, None)

    async def _explota(**_kwargs):
        raise RuntimeError("el ERP no contesta")

    monkeypatch.setattr(main.erp, "depositar_respuesta_flete", _explota)

    asyncio.run(main._process_message(mensaje_entrante("28 mil")))

    # El transportista no tiene la culpa de que el ERP esté caído.
    assert any("Recibimos su mensaje" in cuerpo for _, cuerpo in enviados)


# ── La lectura del modelo ────────────────────────────────────────────────── #


def test_parsea_una_lectura_completa():
    i = parsear_interpretacion(
        '{"monto_pesos": 28000, "unidad": "POR_VIAJE", "incluye_casetas": false, '
        '"declina": false}'
    )

    # Pesos → centavos se hace en código: pedirle centavos al modelo invita a un
    # error de x100 que nadie ve hasta que se paga.
    assert i.montoCentavos == 2_800_000
    assert i.unidad == "POR_VIAJE"
    assert i.incluyeCasetas is False
    # Lo que no dijo queda en null, no en false.
    assert i.incluyeManiobras is None


def test_un_precio_sin_unidad_no_es_un_precio():
    i = parsear_interpretacion('{"monto_pesos": 28000, "unidad": null}')

    # No se puede comparar contra nada: se deja sin monto y el ERP lo manda a
    # revisión en vez de meterlo a la comparativa.
    assert i.montoCentavos is None


def test_una_unidad_inventada_se_descarta():
    i = parsear_interpretacion('{"monto_pesos": 28000, "unidad": "POR_CAMION"}')
    assert i.unidad is None
    assert i.montoCentavos is None


def test_tolera_el_json_envuelto_en_markdown():
    i = parsear_interpretacion('```json\n{"monto_pesos": 900, "unidad": "POR_TONELADA"}\n```')
    assert i.montoCentavos == 90_000


def test_una_respuesta_que_no_es_json_no_rompe_nada():
    assert parsear_interpretacion("no entendí la pregunta") is None
    assert parsear_interpretacion("") is None
    assert parsear_interpretacion("{roto") is None


def test_un_monto_negativo_se_descarta():
    negativo = parsear_interpretacion('{"monto_pesos": -100, "unidad": "POR_VIAJE"}')
    assert negativo.montoCentavos is None


def test_declinar_no_necesita_precio():
    i = parsear_interpretacion('{"monto_pesos": null, "unidad": null, "declina": true}')
    assert i.declina is True


def test_la_fecha_tiene_que_venir_bien_formada():
    assert parsear_interpretacion('{"disponible_desde": "el jueves"}').disponibleDesde is None
    con_fecha = parsear_interpretacion('{"disponible_desde": "2026-09-03"}')
    assert con_fecha.disponibleDesde == "2026-09-03"


# ── El acuse ─────────────────────────────────────────────────────────────── #


def test_el_acuse_no_promete_lo_que_no_se_sabe():
    con_precio = acuse(InterpretacionFlete(montoCentavos=1, unidad="POR_VIAJE"))
    sin_leer = acuse(None)

    # No se le dice "lo contactamos": la decisión puede tardar días.
    assert "registrado" in con_precio
    assert "revisando" in sin_leer


def test_referencia_de_arma_el_prefijo_que_espera_el_erp():
    assert referencia_de("c1") == "cotizacion_flete:c1"
