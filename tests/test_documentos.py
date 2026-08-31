"""El bot le manda documentos al cliente: facturas, contratos, estado de cuenta.

Lo que se prueba no es "llama a Meta", es el candado: que un folio ajeno no
salga, que sin sesión no salga nada, y que el archivo llegue con el nombre con
el que el cliente lo va a guardar en su teléfono.
"""

import asyncio
import json

import pytest

from app.agents.soporte import TIPOS_DOCUMENTO, TOOLS, TOOLS_CON_SESION
from app.menus import ACCIONES, ESTADO_CUENTA, menu_cliente
from app.whatsapp import WhatsAppClient

PHONE = "5215512345678"


@pytest.fixture
def agente(soporte):
    """Soporte con un WhatsApp que registra lo que se manda en vez de mandarlo."""
    enviados: list[dict] = []

    async def _upload(contenido, filename, mime_type):
        enviados.append(
            {"paso": "upload", "nombre": filename, "mime": mime_type, "bytes": contenido}
        )
        return f"media-{filename}"

    async def _send(to, media_id, filename, caption=""):
        enviados.append({"paso": "send", "to": to, "media_id": media_id, "nombre": filename})
        return {}

    soporte._wa = type(
        "WA",
        (),
        {"upload_media": staticmethod(_upload), "send_document": staticmethod(_send)},
    )()
    soporte.enviados = enviados
    return soporte


def _run(agent, name, payload=None, phone=PHONE):
    return json.loads(asyncio.run(agent.run_tool(name, payload or {}, phone)))


def _identificar(agent):
    return _run(
        agent, "identificar_cliente", {"nombre": "Molinos del Bajío", "rfc": "MBA950101AB1"}
    )


# ------------------------------- El candado ------------------------------- #
def test_sin_identificarse_no_sale_ningun_documento(agente):
    data = _run(agente, "enviar_mi_documento", {"tipo": "factura", "folio": "FACT-2026-0031"})
    assert data["identificado"] is False
    assert agente.enviados == []


def test_esta_declarada_como_tool_con_sesion():
    """Si alguien la sacara de la lista, quedaría abierta."""
    assert "enviar_mi_documento" in TOOLS_CON_SESION


def test_un_folio_ajeno_no_se_manda(agente):
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "factura", "folio": "FACT-2026-9999"})

    assert data["enviado"] is False
    assert data["motivo"] == "no_esta_en_su_cuenta"
    assert "NO sugieras" in data["instruccion"]
    assert agente.enviados == []


# -------------------------------- El envío -------------------------------- #
def test_manda_la_factura_con_su_nombre(agente):
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "factura", "folio": "FACT-2026-0031"})

    assert data["enviado"] is True
    upload, send = agente.enviados
    assert upload["nombre"] == "FACT-2026-0031.pdf"
    assert upload["mime"] == "application/pdf"
    # El nombre es lo que le queda guardado al cliente en el teléfono.
    assert send["nombre"] == "FACT-2026-0031.pdf"
    assert send["to"] == PHONE


def test_el_xml_es_otro_documento(agente):
    _identificar(agente)
    _run(agente, "enviar_mi_documento", {"tipo": "factura_xml", "folio": "FACT-2026-0031"})
    assert agente.enviados[0]["nombre"] == "FACT-2026-0031.xml"
    assert agente.enviados[0]["mime"] == "application/xml"


def test_el_contrato_tambien(agente):
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "contrato", "folio": "CONT-2026-0001"})
    assert data["enviado"] is True
    assert agente.enviados[0]["nombre"] == "CONT-2026-0001.pdf"


def test_el_estado_de_cuenta_no_pide_folio(agente):
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "estado_de_cuenta"})
    assert data["enviado"] is True
    assert "estado-de-cuenta" in agente.enviados[0]["nombre"]


def test_pide_el_folio_cuando_hace_falta(agente):
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "factura"})
    assert data["enviado"] is False
    assert data["motivo"] == "falta_folio"
    assert agente.enviados == []


def test_un_tipo_inventado_no_llega_al_erp(agente):
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "nomina_del_director", "folio": "X"})
    assert data["enviado"] is False
    assert data["motivo"] == "tipo_desconocido"
    assert agente.enviados == []


# ------------------------- Coherencia de los tres lados -------------------- #
def test_los_tipos_del_enum_y_de_la_tool_son_los_mismos():
    """El enum de la tool, la constante y el ERP tienen que coincidir: si se
    agrega un tipo en uno solo, el modelo pedirá algo que nadie sirve."""
    tool = next(t for t in TOOLS if t["name"] == "enviar_mi_documento")
    assert set(tool["input_schema"]["properties"]["tipo"]["enum"]) == TIPOS_DOCUMENTO


def test_el_estado_de_cuenta_esta_en_el_menu():
    ids = {o.id for o in menu_cliente(True).opciones}
    assert ESTADO_CUENTA in ids
    assert ESTADO_CUENTA in ACCIONES


# ------------------------------ La capa de Meta ---------------------------- #
def test_en_modo_desarrollo_el_upload_no_sale_a_la_red():
    wa = WhatsAppClient()
    media_id = asyncio.run(wa.upload_media(b"%PDF", "FACT-2026-0031.pdf", "application/pdf"))
    assert media_id.startswith("dev-media-")


def test_el_documento_viaja_por_media_id_no_por_url():
    """Nunca se publica un enlace desde el que bajar la factura de un cliente."""
    wa = WhatsAppClient()
    payload = asyncio.run(wa.send_document(PHONE, "media-123", "FACT-2026-0031.pdf"))["payload"]

    assert payload["type"] == "document"
    assert payload["document"] == {"id": "media-123", "filename": "FACT-2026-0031.pdf"}
    assert "link" not in json.dumps(payload)


def test_el_caption_va_cuando_lo_hay():
    wa = WhatsAppClient()
    payload = asyncio.run(
        wa.send_document(PHONE, "m1", "a.pdf", caption="Su factura de mayo")
    )["payload"]
    assert payload["document"]["caption"] == "Su factura de mayo"
