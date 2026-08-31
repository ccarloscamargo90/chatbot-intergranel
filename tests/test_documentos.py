"""El bot le manda documentos al cliente: facturas, contratos, estado de cuenta.

Lo que se prueba no es "llama a Meta", es el candado: que un folio ajeno no
salga, que sin sesión no salga nada, y que el archivo llegue con el nombre con
el que el cliente lo va a guardar en su teléfono.
"""

import asyncio
import json

import httpx
import pytest

from app.agents.soporte import TIPOS_DOCUMENTO, TOOLS, TOOLS_CON_SESION
from app.erp import DocumentoNoRecuperable
from app.errores import AUTO, detalle_http
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


# --------------------- Lo que se rompió en producción ---------------------- #
#
# Un cliente pidió sus dos facturas (PDF y XML de cada una) y no le llegó
# ninguna. El bot le contestó "es el módulo de envío el que no las está
# entregando", que era falso: nadie lo estaba entregando mal, pero tampoco
# había forma de saber qué pasó. Dos causas y un agravante:
#
#   1. Meta no acepta `application/xml`, así que el XML nunca pudo salir.
#   2. Una factura sin archivo cargado devolvía el mismo 404 que una factura
#      ajena, y el bot le decía "no aparece en su cuenta" a alguien que
#      acababa de verla en su lista.
#   3. Cualquier otro fallo llegaba como `{"error": str(exc)}`, sin causa.
#
# Las pruebas de entonces no podían verlo: mockeaban a Meta —y un mock acepta
# cualquier MIME— y el ERP simulado solo tenía facturas con archivo.


def test_el_xml_no_se_sube_como_application_xml():
    """La lista de MIME de Meta es cerrada y `application/xml` no está en ella.

    Se sube como `text/plain` (que es lo que un XML es) conservando el nombre
    `.xml`, que es con lo que le queda guardado al cliente.
    """
    wa = WhatsAppClient()
    wa._token = "un-token"  # forzamos la ruta real, no el modo desarrollo
    subidas: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        subidas.append({"body": request.content, "url": str(request.url)})
        return httpx.Response(200, json={"id": "media-xml-1"})

    wa._transport = httpx.MockTransport(handler)
    media_id = asyncio.run(
        wa.upload_media(b"<cfdi/>", "FACT-2026-0031.xml", "application/xml")
    )

    assert media_id == "media-xml-1"
    cuerpo = subidas[0]["body"].decode("latin-1")
    assert "application/xml" not in cuerpo
    assert "text/plain" in cuerpo
    # El nombre no se toca: es lo que decide la extensión en el teléfono.
    assert "FACT-2026-0031.xml" in cuerpo


def test_el_pdf_conserva_su_mime():
    wa = WhatsAppClient()
    wa._token = "un-token"
    subidas: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        subidas.append(request.content)
        return httpx.Response(200, json={"id": "media-pdf-1"})

    wa._transport = httpx.MockTransport(handler)
    asyncio.run(wa.upload_media(b"%PDF", "FACT-2026-0031.pdf", "application/pdf"))
    assert "application/pdf" in subidas[0].decode("latin-1")


def test_una_factura_SUYA_sin_archivo_no_se_confunde_con_una_ajena(agente):
    """El cliente acaba de verla en su lista: decirle que no aparece lo
    contradice de frente, y el modelo resuelve la contradicción inventando."""
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "factura", "folio": "FACT-2026-0044"})

    assert data["enviado"] is False
    assert data["motivo"] == "sin_archivo_cargado"
    assert "NO digas que no aparece en su cuenta" in data["instruccion"]
    # Y sigue siendo distinto de un folio ajeno, que es lo único opaco.
    ajena = _run(agente, "enviar_mi_documento", {"tipo": "factura", "folio": "FACT-2026-9999"})
    assert ajena["motivo"] == "no_esta_en_su_cuenta"
    assert agente.enviados == []


def test_si_el_archivo_no_se_puede_bajar_no_se_le_echa_al_cliente(agente):
    _identificar(agente)

    async def _revienta(token, tipo, folio=""):
        raise DocumentoNoRecuperable("ERP 503: No pudimos recuperar el archivo")

    agente._erp.get_customer_document = _revienta
    data = _run(agente, "enviar_mi_documento", {"tipo": "factura", "folio": "FACT-2026-0031"})

    assert data["enviado"] is False
    assert data["motivo"] == "archivo_no_recuperable"
    # No es el 404 opaco, y no se arregla reintentando: la instrucción prohíbe
    # las dos salidas fáciles (decirle que no es suya, o que insista).
    assert "NO le digas que no aparece en su cuenta" in data["instruccion"]
    assert "ni que vuelva a intentarlo" in data["instruccion"]
    assert data["detalle"].startswith("ERP 503")


def test_un_fallo_de_meta_llega_con_su_motivo_no_como_error_generico(agente):
    """Lo que devuelve la tool es lo único que el modelo tiene para contestar.

    Con `{"error": "Server error '500'"}` el modelo rellena el hueco; el hueco
    que rellenó en producción fue una causa inventada.
    """
    _identificar(agente)

    async def _upload_falla(contenido, filename, mime_type):
        request = httpx.Request("POST", "https://graph.facebook.com/v21.0/1/media")
        respuesta = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "Unsupported post request", "code": 100}},
        )
        raise httpx.HTTPStatusError("400", request=request, response=respuesta)

    agente._wa.upload_media = _upload_falla
    data = _run(agente, "enviar_mi_documento", {"tipo": "factura", "folio": "FACT-2026-0031"})

    assert data["enviado"] is False
    assert data["motivo"] == "fallo_al_enviar"
    assert "Unsupported post request" in data["detalle"]
    assert "Meta 400" in data["detalle"]
    # Y solo aquí se puede decir que falló el envío: el documento sí existía.
    assert data["documento"] == "FACT-2026-0031.pdf"


def test_el_origen_del_fallo_no_se_etiqueta_a_ciegas():
    """El `except` general de una tool cubre ERP, Chatwoot y Meta a la vez.

    Etiquetar todo como "ERP" mandaría a revisar el sistema que no era, que es
    la misma clase de error que causó el reporte original.
    """
    def _error(host: str, cuerpo: dict) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", f"https://{host}/algo")
        respuesta = httpx.Response(503, request=request, json=cuerpo)
        return httpx.HTTPStatusError("503", request=request, response=respuesta)

    del_erp = detalle_http(_error("erp.intergranel.mx", {"message": "BD sin conexión"}), AUTO)
    assert del_erp == "erp.intergranel.mx 503: BD sin conexión"

    de_meta = detalle_http(
        _error("graph.facebook.com", {"error": {"message": "Rate limit"}}), AUTO
    )
    assert de_meta == "graph.facebook.com 503: Rate limit"

    # Sin respuesta (timeout, DNS) no hay host que reportar, pero sí un tipo.
    assert detalle_http(httpx.ConnectTimeout("agotado"), AUTO) == "ConnectTimeout: agotado"


# ------------------------------ Cotizaciones ------------------------------- #
def test_manda_la_cotizacion_en_pdf(agente):
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "cotizacion", "folio": "COT-2026-0007"})

    assert data["enviado"] is True
    upload, _send = agente.enviados
    assert upload["nombre"] == "COT-2026-0007.pdf"
    assert upload["mime"] == "application/pdf"


def test_una_cotizacion_ajena_no_se_manda(agente):
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "cotizacion", "folio": "COT-2026-9999"})
    assert data["enviado"] is False
    assert data["motivo"] == "no_esta_en_su_cuenta"
    assert agente.enviados == []


def test_la_cotizacion_exige_folio(agente):
    _identificar(agente)
    data = _run(agente, "enviar_mi_documento", {"tipo": "cotizacion"})
    assert data["motivo"] == "falta_folio"


def test_listar_cotizaciones_marca_las_vencidas(agente):
    """`vencida` viaja masticado: deducirlo de una fecha es cómo el modelo
    termina ofreciendo un precio que ya caducó."""
    _identificar(agente)
    data = _run(agente, "listar_mis_cotizaciones")

    assert data["identificado"] is True
    por_folio = {c["id"]: c for c in data["cotizaciones"]}
    assert por_folio["COT-2026-0007"]["vencida"] is False
    assert por_folio["COT-2026-0003"]["vencida"] is True
    # La aceptada trae el contrato que salió de ella.
    assert por_folio["COT-2026-0003"]["contrato"] == "CONT-2026-0002"


def test_las_cotizaciones_exigen_sesion(agente):
    data = _run(agente, "listar_mis_cotizaciones")
    assert data["identificado"] is False
    assert "listar_mis_cotizaciones" in TOOLS_CON_SESION
