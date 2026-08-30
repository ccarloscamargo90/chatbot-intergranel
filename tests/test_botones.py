"""Botones: límites de Meta, envío y recepción del toque.

Los límites de la Cloud API no son consejos: si un título se pasa de largo o la
lista trae 11 filas, Meta rechaza el mensaje ENTERO y el cliente no recibe
nada. Por eso se recortan al construir la `Reply`, y por eso se prueba aquí.
"""

import asyncio

from fastapi.testclient import TestClient

from app import main
from app.dedup import InMemoryDedupStore
from app.menus import ACCIONES, MENU, SALDO, menu_cliente
from app.replies import (
    MAX_BOTONES,
    MAX_FILAS_LISTA,
    MAX_TITULO_BOTON,
    MAX_TITULO_FILA,
    Boton,
    MenuLista,
    OpcionLista,
    Reply,
)
from app.whatsapp import WhatsAppClient

client = TestClient(main.app)


# ------------------------------ Límites de Meta --------------------------- #
def test_no_se_mandan_mas_de_tres_botones():
    reply = Reply("hola", botones=[Boton(f"b{i}", f"Opción {i}") for i in range(6)])
    assert len(reply.botones) == MAX_BOTONES


def test_titulo_de_boton_se_recorta():
    payload = Boton("x", "Un título larguísimo que Meta no acepta").to_payload()
    assert len(payload["reply"]["title"]) <= MAX_TITULO_BOTON


def test_lista_recorta_filas_y_titulos():
    menu = MenuLista(
        boton="Ver",
        opciones=[OpcionLista(f"o{i}", f"Opción número {i} con nombre largo") for i in range(15)],
    )
    filas = menu.to_payload()["sections"][0]["rows"]
    assert len(filas) == MAX_FILAS_LISTA
    assert all(len(f["title"]) <= MAX_TITULO_FILA for f in filas)


def test_los_menus_reales_caben():
    for identificado in (True, False):
        filas = menu_cliente(identificado).to_payload()["sections"][0]["rows"]
        assert 0 < len(filas) <= MAX_FILAS_LISTA
        assert all(len(f["title"]) <= MAX_TITULO_FILA for f in filas)


def test_todo_id_de_menu_tiene_accion():
    """Un botón cuyo id el router no reconoce manda al cliente a la
    clasificación por modelo: contesta cualquier cosa menos lo que pidió."""
    for identificado in (True, False):
        for opcion in menu_cliente(identificado).opciones:
            assert opcion.id in ACCIONES, opcion.id


# --------------------------------- Envío ---------------------------------- #
def test_reply_de_texto_se_manda_como_texto():
    wa = WhatsAppClient()
    enviado = asyncio.run(wa.send_reply("521", Reply("hola")))
    assert enviado["payload"]["type"] == "text"


def test_reply_con_botones_se_manda_como_interactive():
    wa = WhatsAppClient()
    enviado = asyncio.run(wa.send_reply("521", Reply("hola", botones=[Boton("a", "A")])))
    interactivo = enviado["payload"]["interactive"]
    assert enviado["payload"]["type"] == "interactive"
    assert interactivo["type"] == "button"
    assert interactivo["action"]["buttons"][0]["reply"]["id"] == "a"


def test_reply_con_lista_se_manda_como_lista():
    wa = WhatsAppClient()
    reply = Reply("elija", lista=menu_cliente(True))
    interactivo = asyncio.run(wa.send_reply("521", reply))["payload"]["interactive"]
    assert interactivo["type"] == "list"
    assert interactivo["action"]["button"] == "Ver opciones"


def test_si_el_interactivo_truena_se_manda_el_texto(monkeypatch):
    """Perder los botones es un mal menor; perder la respuesta, no."""
    wa = WhatsAppClient()

    async def _truena(*args, **kwargs):
        raise RuntimeError("Meta rechazó el interactivo")

    monkeypatch.setattr(wa, "send_buttons", _truena)
    reply = Reply("el saldo es $100", botones=[Boton("a", "A")])
    enviado = asyncio.run(wa.send_reply("521", reply))
    assert enviado["payload"]["type"] == "text"
    assert enviado["payload"]["text"]["body"] == "el saldo es $100"


def test_un_string_pelado_sigue_funcionando():
    wa = WhatsAppClient()
    enviado = asyncio.run(wa.send_reply("521", "texto de siempre"))
    assert enviado["payload"]["text"]["body"] == "texto de siempre"


# -------------------------------- Recepción -------------------------------- #
def test_toque_de_boton_se_lee_por_id():
    mensaje = {
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": SALDO, "title": "💰 Mi saldo"},
        },
    }
    assert main._texto_interactivo(mensaje) == SALDO


def test_toque_de_lista_se_lee_por_id():
    mensaje = {
        "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": MENU, "title": "Menú"}},
    }
    assert main._texto_interactivo(mensaje) == MENU


def test_interactivo_sin_respuesta_no_revienta():
    assert main._texto_interactivo({"type": "interactive", "interactive": {}}) is None


def test_el_toque_llega_al_router(monkeypatch):
    ruteados, enviados = [], []

    async def fake_route(phone, content, store_text=None):
        ruteados.append(content)
        return Reply("listo")

    async def fake_send_reply(to, reply):
        enviados.append(reply)
        return {}

    monkeypatch.setattr(main.router, "route", fake_route)
    monkeypatch.setattr(main.wa, "send_reply", fake_send_reply)
    monkeypatch.setattr(main, "dedup", InMemoryDedupStore(ttl_seconds=60))

    asyncio.run(
        main._process_message(
            {
                "id": "wamid.BTN",
                "from": "5215512345678",
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": SALDO, "title": "💰 Mi saldo"},
                },
            }
        )
    )
    # Al router llega el ID, no el título: el título es texto de pantalla y
    # cambia al reescribir un menú; el id es el contrato.
    assert ruteados == [SALDO]
    assert len(enviados) == 1


def test_toque_vacio_pide_reintentar(monkeypatch):
    ruteados, enviados = [], []

    async def fake_route(phone, content, store_text=None):
        ruteados.append(content)
        return Reply("listo")

    async def fake_send_text(to, text):
        enviados.append(text)
        return {}

    monkeypatch.setattr(main.router, "route", fake_route)
    monkeypatch.setattr(main.wa, "send_text", fake_send_text)
    monkeypatch.setattr(main, "dedup", InMemoryDedupStore(ttl_seconds=60))

    asyncio.run(
        main._process_message(
            {
                "id": "wamid.VACIO",
                "from": "5215512345678",
                "type": "interactive",
                "interactive": {"type": "button_reply"},
            }
        )
    )
    assert ruteados == []
    assert len(enviados) == 1
