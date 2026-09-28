"""Precio de venta semanal dictado por WhatsApp.

Sin red: ERP mock, bus en memoria y WhatsApp monkeypatcheado. La lectura del
precio es determinista, así que tampoco hay modelo que simular.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.bus import InMemoryEventBus
from app.dedup import InMemoryDedupStore
from app.erp import HTTPERPClient, MockERPClient
from app.main import app
from app.precio_semanal import (
    IDS_CAPTURA,
    PS_CONFIRMAR,
    PS_CORREGIR,
    PS_DESPUES,
    PS_MISMO,
    PS_OMITIR,
    CapturaPrecioSemanal,
    es_solicitud_de_precio,
    intencion_escrita,
    leer_precio,
    telefono_comparable,
)

client = TestClient(app)

# Como lo manda el ERP (sin el "1") y como lo entrega WhatsApp (con él).
TEL_ERP = "525599990000"
TEL_WA = "5215599990000"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def erp() -> MockERPClient:
    return MockERPClient()


@pytest.fixture
def captura(erp) -> CapturaPrecioSemanal:
    return CapturaPrecioSemanal(erp, InMemoryEventBus())


def ids(reply) -> list[str]:
    return [b.id for b in reply.botones]


# ── Lectura del precio ──────────────────────────────────────────────────── #


@pytest.mark.parametrize(
    ("texto", "centavos"),
    [
        ("7050", 705_000),
        ("7,050", 705_000),
        ("$7,050.00", 705_000),
        ("7050.5", 705_050),
        ("a 7050 la tonelada", 705_000),
        ("7 mil", 700_000),
        ("7mil", 700_000),
        ("7 mil 50", 705_000),
        ("7.5 mil", 750_000),
        ("123,456.78", 12_345_678),
    ],
)
def test_lee_lo_que_la_gente_teclea(texto, centavos):
    assert leer_precio(texto).centavos == centavos


@pytest.mark.parametrize("texto", ["7.050", "7050,5", "7,05"])
def test_lo_ambiguo_no_se_adivina(texto):
    lectura = leer_precio(texto)
    assert lectura.centavos is None
    assert lectura.problema == "ambiguo"


def test_dos_numeros_se_preguntan_uno_por_uno():
    assert leer_precio("blanco 7050, amarillo 6800").problema == "varios"


def test_sin_numero_no_hay_precio():
    lectura = leer_precio("hola, buen día")
    assert lectura.centavos is None
    assert lectura.problema is None


def test_cero_o_un_disparate_no_son_precio():
    assert leer_precio("0").problema == "fuera_de_rango"
    assert leer_precio("5000000").problema == "fuera_de_rango"


def test_las_palabras_equivalen_a_botones():
    assert intencion_escrita("Sí") == PS_CONFIRMAR
    assert intencion_escrita("el mismo") == PS_MISMO
    assert intencion_escrita("después") == PS_DESPUES
    # "sí, pero 7100" no es un sí: trae otra cifra.
    assert intencion_escrita("sí, pero 7100") is None


def test_el_1_de_mexico_no_separa_al_responsable_de_su_pregunta():
    assert telefono_comparable(TEL_WA) == TEL_ERP
    assert telefono_comparable("+52 55 9999 0000") == TEL_ERP
    assert telefono_comparable("15551234567") == "15551234567"


def test_reconoce_el_aviso_del_sabado():
    assert es_solicitud_de_precio("precios.semanal_solicitud")
    assert not es_solicitud_de_precio("calendario.objetivo")


# ── La conversación ─────────────────────────────────────────────────────── #


def test_sin_pregunta_abierta_no_se_mete_en_la_conversacion(captura):
    assert run(captura.atender(TEL_WA, "7050")) is None


def test_un_boton_viejo_sin_pregunta_abierta_lo_dice(captura):
    reply = run(captura.atender(TEL_WA, PS_CONFIRMAR))
    assert "ya no está abierta" in reply.texto
    assert "/precio" in reply.texto


def test_al_contestar_el_aviso_pregunta_el_primer_producto_con_botones(captura):
    run(captura.marcar(TEL_ERP))

    reply = run(captura.atender(TEL_WA, "Hola, sí"))

    assert "Maíz blanco grado 1" in reply.texto
    assert "hoy $6,950.00 por tonelada" in reply.texto
    assert "(1 de 2)" in reply.texto
    assert ids(reply) == [PS_MISMO, PS_DESPUES]


def test_nada_se_guarda_sin_confirmar(captura, erp):
    run(captura.marcar(TEL_ERP))

    reply = run(captura.atender(TEL_WA, "7,050", wamid="wamid.1"))

    assert "¿Confirmo *Maíz blanco grado 1* a *$7,050.00* por tonelada" in reply.texto
    assert "lunes 5 de oct" in reply.texto
    assert ids(reply) == [PS_CONFIRMAR, PS_CORREGIR]
    assert erp.respuestas_precio == []


def test_confirmar_guarda_y_pasa_al_siguiente(captura, erp):
    run(captura.marcar(TEL_ERP))
    run(captura.atender(TEL_WA, "7,050", wamid="wamid.1"))

    reply = run(captura.atender(TEL_WA, PS_CONFIRMAR))

    assert erp.respuestas_precio == [
        {
            "empresa": "intergranel",
            "producto_id": "p-mb",
            "accion": "precio",
            "precio_centavos": "705000",
            "wamid": "wamid.1",
            "texto": "7,050",
        }
    ]
    assert "✅ *Maíz blanco grado 1*: $7,050.00 por tonelada" in reply.texto
    assert "a partir del lunes 5 de oct a las 00:00" in reply.texto
    # Y en el MISMO mensaje, la siguiente pregunta.
    assert "Maíz amarillo grado 2" in reply.texto
    assert ids(reply) == [PS_MISMO, PS_DESPUES]


def test_escribir_si_confirma_igual_que_el_boton(captura, erp):
    run(captura.marcar(TEL_ERP))
    run(captura.atender(TEL_WA, "7050"))
    run(captura.atender(TEL_WA, "sí"))
    assert erp.respuestas_precio[0]["precio_centavos"] == "705000"


def test_corregir_vuelve_a_preguntar_sin_guardar(captura, erp):
    run(captura.marcar(TEL_ERP))
    run(captura.atender(TEL_WA, "705"))

    reply = run(captura.atender(TEL_WA, PS_CORREGIR))

    assert erp.respuestas_precio == []
    assert "escríbame el precio correcto" in reply.texto
    assert "Maíz blanco grado 1" in reply.texto


def test_un_cero_de_menos_se_advierte(captura):
    run(captura.marcar(TEL_ERP))
    reply = run(captura.atender(TEL_WA, "705"))
    assert "⚠️ Es muy distinto al de hoy ($6,950.00)" in reply.texto


def test_una_cifra_nueva_en_la_confirmacion_reemplaza_a_la_anterior(captura, erp):
    run(captura.marcar(TEL_ERP))
    run(captura.atender(TEL_WA, "705"))
    reply = run(captura.atender(TEL_WA, "7050"))
    assert "*$7,050.00*" in reply.texto
    run(captura.atender(TEL_WA, PS_CONFIRMAR))
    assert erp.respuestas_precio[0]["precio_centavos"] == "705000"


def test_mismo_precio_y_al_terminar_se_cierra(captura, erp):
    run(captura.marcar(TEL_ERP))
    run(captura.atender(TEL_WA, "7050"))
    run(captura.atender(TEL_WA, PS_CONFIRMAR))

    reply = run(captura.atender(TEL_WA, "igual"))

    assert erp.respuestas_precio[1]["accion"] == "mismo"
    assert "Eso es todo por esta semana" in reply.texto
    assert not run(captura.esta_marcado(TEL_WA))


def test_lo_ambiguo_se_vuelve_a_preguntar(captura, erp):
    run(captura.marcar(TEL_ERP))
    reply = run(captura.atender(TEL_WA, "7.050"))
    assert "sin puntos de miles" in reply.texto
    assert erp.respuestas_precio == []


def test_sin_precio_publicado_se_ofrece_no_cambiar_en_vez_de_mismo_precio(captura, erp):
    erp.pendientes_precio[0].precioActualCentavos = None
    run(captura.marcar(TEL_ERP))
    reply = run(captura.atender(TEL_WA, "hola"))
    assert "hoy sin precio publicado" in reply.texto
    assert ids(reply) == [PS_OMITIR, PS_DESPUES]


def test_un_comando_o_un_boton_del_menu_siguen_al_router(captura):
    run(captura.marcar(TEL_ERP))
    assert run(captura.atender(TEL_WA, "/menu")) is None
    assert run(captura.atender(TEL_WA, "cli_saldo")) is None
    # La pregunta sigue abierta para cuando vuelva.
    assert run(captura.esta_marcado(TEL_WA))


def test_despues_pausa_y_precio_reanuda(captura):
    run(captura.marcar(TEL_ERP))
    reply = run(captura.atender(TEL_WA, PS_DESPUES))
    assert "/precio" in reply.texto
    assert not run(captura.esta_marcado(TEL_WA))

    # Pausado, un "7050" ya es una consulta cualquiera.
    assert run(captura.atender(TEL_WA, "7050")) is None

    reply = run(captura.reanudar(TEL_WA))
    assert "Maíz blanco grado 1" in reply.texto
    assert run(captura.esta_marcado(TEL_WA))


def test_a_un_numero_que_no_es_el_responsable_no_le_pregunta_nada(captura):
    # Aunque el bus lo tuviera marcado por error, el ERP no le da pendientes.
    run(captura.marcar("5215511112222"))
    assert run(captura.atender("5215511112222", "7050")) is None
    reply = run(captura.reanudar("5215511112222"))
    assert "No tengo precios pendientes" in reply.texto


def test_si_el_erp_falla_no_se_pierde_la_confirmacion(captura, erp, monkeypatch):
    run(captura.marcar(TEL_ERP))
    run(captura.atender(TEL_WA, "7050"))

    async def _falla(**_kw):
        raise httpx.ConnectError("ERP caído")

    monkeypatch.setattr(erp, "responder_precio_semanal", _falla)
    reply = run(captura.atender(TEL_WA, PS_CONFIRMAR))
    assert "no se cambió nada" in reply.texto
    assert ids(reply) == [PS_CONFIRMAR, PS_CORREGIR]


def test_los_botones_de_la_captura_caben_en_meta():
    from app.precio_semanal import (
        BOTON_CONFIRMAR,
        BOTON_CORREGIR,
        BOTON_DESPUES,
        BOTON_MISMO,
        BOTON_OMITIR,
    )

    botones = [BOTON_CONFIRMAR, BOTON_CORREGIR, BOTON_DESPUES, BOTON_MISMO, BOTON_OMITIR]
    assert {b.id for b in botones} == set(IDS_CAPTURA)
    for b in botones:
        assert len(b.titulo) <= 20, b.titulo


# ── De punta a punta: webhook del ERP → WhatsApp ─────────────────────────── #


@pytest.fixture
def entorno(monkeypatch):
    erp = MockERPClient()
    monkeypatch.setattr(main.settings, "whatsapp_aviso_template", "")
    monkeypatch.setattr(main.settings, "erp_webhook_secret", "")
    monkeypatch.setattr(main, "dedup", InMemoryDedupStore(ttl_seconds=3600))
    monkeypatch.setattr(main, "precio_semanal", CapturaPrecioSemanal(erp, InMemoryEventBus()))

    salidas: list = []

    async def _send_text(to, body):
        salidas.append((to, body))
        return {"messages": [{"id": "wamid.out"}]}

    async def _send_reply(to, reply):
        salidas.append((to, reply))
        return {}

    monkeypatch.setattr(main.wa, "send_text", _send_text)
    monkeypatch.setattr(main.wa, "send_reply", _send_reply)
    return erp, salidas


def aviso_del_sabado(**over) -> dict:
    base = {
        "id": "aviso-ps-1",
        "tipo": "precios.semanal_solicitud",
        "telefono": TEL_ERP,
        "titulo": "Precio de venta de la semana del 5 al 11 de oct",
        "mensaje": "Conteste este mensaje y le pregunto uno por uno el precio por tonelada de: …",
        "referencia": "precio-semanal:sol-mock",
        "empresa": "Intergranel",
    }
    base.update(over)
    return base


def texto_entrante(texto, tel=TEL_WA, wamid="wamid.in"):
    return {"from": tel, "id": wamid, "type": "text", "text": {"body": texto}}


def boton_entrante(boton_id, tel=TEL_WA, wamid="wamid.btn"):
    return {
        "from": tel,
        "id": wamid,
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": boton_id, "title": "x"}},
    }


def test_el_aviso_del_sabado_abre_la_captura_y_la_respuesta_no_va_al_router(entorno, monkeypatch):
    erp, salidas = entorno

    async def _router(*_a, **_kw):
        raise AssertionError("la respuesta al precio no debe llegar al router")

    monkeypatch.setattr(main.router, "route", _router)

    r = client.post("/webhooks/erp/notificacion", json=aviso_del_sabado())
    assert r.json()["status"] == "sent"

    run(main._process_message(texto_entrante("7050", wamid="wamid.a")))
    run(main._process_message(boton_entrante(PS_CONFIRMAR, wamid="wamid.b")))

    assert erp.respuestas_precio[0]["precio_centavos"] == "705000"
    ultima = salidas[-1][1]
    assert "Maíz amarillo grado 2" in ultima.texto


def test_si_el_aviso_no_salio_no_queda_pregunta_abierta(entorno, monkeypatch):
    async def _rechazo(*_a, **_kw):
        raise httpx.HTTPStatusError(
            "boom", request=httpx.Request("POST", "https://graph"), response=httpx.Response(400)
        )

    monkeypatch.setattr(main, "notify_erp_aviso", _rechazo)
    r = client.post("/webhooks/erp/notificacion", json=aviso_del_sabado(id="aviso-ps-2"))
    assert r.json()["status"] == "failed"
    assert not run(main.precio_semanal.esta_marcado(TEL_WA))


# ── El cliente HTTP del ERP ─────────────────────────────────────────────── #


def test_http_el_telefono_viaja_en_el_cuerpo_no_en_la_url():
    vistos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.append(request)
        if request.url.path.endswith("/pendientes"):
            return httpx.Response(
                200,
                json={
                    "pendientes": [
                        {
                            "empresa": "intergranel",
                            "solicitudId": "s1",
                            "productoId": "p1",
                            "producto": "Maíz blanco grado 1",
                            "unidad": "tonelada",
                            "precioActualCentavos": "695000",
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json={"registrado": True, "estado": "PROGRAMADO", "precioCentavos": "705000"}
        )

    c = HTTPERPClient(
        "https://erp.example.com/api/v1",
        api_key="k",
        api_key_header="X-Bot-Api-Key",
        transport=httpx.MockTransport(handler),
    )
    pendientes = run(c.precio_semanal_pendientes(TEL_WA))
    r = run(
        c.responder_precio_semanal(
            empresa="intergranel",
            solicitud_id="s1",
            producto_id="p1",
            telefono=TEL_WA,
            accion="precio",
            precio_centavos=705_000,
            wamid="w1",
            texto="7050",
        )
    )

    assert pendientes[0].producto == "Maíz blanco grado 1"
    assert r.registrado and r.precioCentavos == "705000"
    assert vistos[0].url.path == "/api/v1/bot/precio-semanal/pendientes"
    assert TEL_WA not in str(vistos[0].url)
    assert json.loads(vistos[0].content) == {"telefono": TEL_WA}
    assert vistos[1].url.path == "/api/v1/bot/precio-semanal/respuesta"
    assert json.loads(vistos[1].content) == {
        "empresa": "intergranel",
        "solicitudId": "s1",
        "productoId": "p1",
        "telefono": TEL_WA,
        "accion": "precio",
        "precioCentavos": 705_000,
        "wamid": "w1",
        "texto": "7050",
    }
    assert vistos[0].headers["X-Bot-Api-Key"] == "k"
