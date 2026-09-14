"""Pruebas del agente de Ventas.

Los precios y las cotizaciones vienen del CRM simulado; los contratos y las
solicitudes, del ERP simulado. Ninguna toca la red (regla 6).
"""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app import tareas
from app.agents.ventas import VentasAgent, folio_cotizacion
from app.bus import InMemoryEventBus
from app.crm import CRMNoDisponible, MockCRMClient
from app.erp import MockERPClient
from app.history import InMemoryHistoryStore

PHONE = "5215512345678"


class WhatsAppDeMentiras:
    """Apunta lo que se le manda en vez de mandarlo a Meta."""

    def __init__(self) -> None:
        self.subidos: list[dict] = []
        self.enviados: list[dict] = []

    async def upload_media(self, contenido, filename, mime_type):
        self.subidos.append({"bytes": contenido, "nombre": filename, "mime": mime_type})
        return f"media-{filename}"

    async def send_document(self, to, media_id, filename, caption=""):
        self.enviados.append({"to": to, "media_id": media_id, "nombre": filename})
        return {}


class WhatsAppQueFalla(WhatsAppDeMentiras):
    async def upload_media(self, contenido, filename, mime_type):
        raise RuntimeError("Meta: (#131030) Recipient phone number not in allowed list")


@pytest.fixture
def ventas() -> VentasAgent:
    a = VentasAgent.__new__(VentasAgent)
    a._erp = MockERPClient()
    a._crm = MockCRMClient()
    a._bus = InMemoryEventBus()
    a._history_store = InMemoryHistoryStore()
    a._wa = WhatsAppDeMentiras()
    return a


class CRMCaido(MockCRMClient):
    """Un CRM que no contesta, para ver qué dice el bot cuando no puede consultar."""

    async def catalogo(self):
        raise CRMNoDisponible("CRM: connection refused")

    async def registrar_cotizacion(self, **kwargs):
        raise CRMNoDisponible("CRM: connection refused")


def _run(agent, name, payload):
    return json.loads(asyncio.run(agent.run_tool(name, payload, PHONE)))


# --- Precio ---------------------------------------------------------------- #


def test_consultar_precio_lee_del_crm(ventas):
    data = _run(ventas, "consultar_precio", {"producto": "maíz blanco"})
    assert data["disponible"] is True
    assert data["precio_ton"] == 6169.56
    assert data["moneda"] == "MXN"
    # De cuándo es el dato: sin esto el bot no puede decir "precio de hoy".
    assert data["actualizado_el"]


def test_consultar_precio_tolera_acentos_y_mayusculas(ventas):
    sin_acento = _run(ventas, "consultar_precio", {"producto": "MAIZ BLANCO"})
    assert sin_acento["disponible"] is True
    assert sin_acento["producto"] == "Maíz blanco"


def test_consultar_precio_no_confunde_blanco_con_amarillo(ventas):
    data = _run(ventas, "consultar_precio", {"producto": "maíz amarillo"})
    assert data["producto"] == "Maíz amarillo"
    assert data["precio_ton"] == 5890.00


def test_producto_que_no_vendemos_dice_que_no_esta_en_catalogo(ventas):
    data = _run(ventas, "consultar_precio", {"producto": "café"})
    assert data["disponible"] is False
    assert data["motivo"] == "no_esta_en_catalogo"


def test_producto_sin_precio_no_se_confunde_con_uno_que_no_vendemos(ventas):
    """El motivo tiene que distinguirlos: si no, el bot le dice a un cliente
    que no manejamos algo que sí manejamos."""
    data = _run(ventas, "consultar_precio", {"producto": "sorgo dulce"})
    assert data["disponible"] is False
    assert data["motivo"] == "sin_precio_publicado"
    assert data["producto"] == "Sorgo dulce"


def test_espejo_desactualizado_no_da_ninguna_cifra(ventas):
    ventas._crm.desactualizado = True
    data = _run(ventas, "consultar_precio", {"producto": "maíz blanco"})
    assert data["disponible"] is False
    assert data["motivo"] == "datos_no_confiables"
    assert "precio_ton" not in data


def test_crm_caido_no_cae_al_erp(ventas):
    """La regla de dirección no tiene atajos: sin CRM no hay precio, aunque el
    ERP esté ahí. Un fallback al ERP el día que el CRM falla es el día en que
    dos sistemas dicen precios distintos."""
    ventas._crm = CRMCaido()
    data = _run(ventas, "consultar_precio", {"producto": "maíz blanco"})
    assert data["disponible"] is False
    assert data["motivo"] == "crm_no_disponible"
    assert "precio_ton" not in data


# --- Catálogo -------------------------------------------------------------- #


def test_listar_productos_devuelve_el_catalogo_del_crm(ventas):
    data = _run(ventas, "listar_productos", {})
    assert data["disponible"] is True
    nombres = [p["producto"] for p in data["productos"]]
    assert "Maíz blanco" in nombres
    # El que no tiene precio también sale, con precio en null.
    sorgo = next(p for p in data["productos"] if p["producto"] == "Sorgo dulce")
    assert sorgo["precio_ton"] is None


def test_listar_productos_calla_si_el_espejo_esta_viejo(ventas):
    ventas._crm.desactualizado = True
    data = _run(ventas, "listar_productos", {})
    assert data["disponible"] is False
    assert data["motivo"] == "datos_no_confiables"


# --- Cotización ------------------------------------------------------------ #


def test_generar_cotizacion_registra_en_el_crm_y_publica_en_el_bus(ventas):
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "maíz blanco", "cantidad_ton": 10, "nombre_cliente": "Molinos del Bajío"},
    )
    assert data["disponible"] is True
    assert data["total"] == 61695.60
    assert data["precio_ton"] == 6169.56
    # Quedó en el CRM, que es donde un vendedor la va a trabajar.
    assert len(ventas._crm.cotizaciones) == 1
    assert ventas._crm.cotizaciones[0].folio == data["folio"]
    # Y en el bus, para el resto de los agentes.
    evento = asyncio.run(ventas._bus.read(f"bus:ventas:cotizacion:{PHONE}"))
    assert evento["total"] == 61695.60


PEDIDO = {
    "producto": "maíz blanco",
    "cantidad_ton": 10,
    "nombre_cliente": "Molinos del Bajío",
}


def _cotizar(agent, payload=None, phone=PHONE):
    """Cotiza y espera el trabajo de segundo plano (la nota al vendedor)."""

    async def escenario():
        crudo = await agent.run_tool("generar_cotizacion", payload or PEDIDO, phone)
        await tareas.esperar_todo(timeout=5)
        return json.loads(crudo)

    return asyncio.run(escenario())


# --- El PDF que se lleva el cliente ---------------------------------------- #


def test_al_cotizar_le_llega_el_pdf_por_whatsapp(ventas):
    data = _cotizar(ventas)

    assert data["pdf_enviado"] is True
    assert len(ventas._wa.enviados) == 1
    # El nombre es lo que le queda guardado en el teléfono: lleva el folio.
    assert ventas._wa.enviados[0]["nombre"] == f"Cotizacion-{data['folio']}.pdf"
    assert ventas._wa.enviados[0]["to"] == PHONE
    subido = ventas._wa.subidos[0]
    assert subido["mime"] == "application/pdf"
    assert subido["bytes"].startswith(b"%PDF-")


def test_el_pdf_del_cliente_y_el_del_vendedor_son_el_mismo_archivo(ventas):
    """Si se generara dos veces, el vendedor podría estar viendo una versión
    y el cliente otra."""
    data = _cotizar(ventas)
    en_el_crm = ventas._crm.enviado[0]
    assert en_el_crm["pdf"] == ventas._wa.subidos[0]["bytes"]
    assert en_el_crm["pdf_nombre"] == f"Cotizacion-{data['folio']}.pdf"


def test_en_modo_manual_el_pdf_va_al_crm_pero_no_al_cliente(ventas):
    """Es justo lo que ese modo existe para evitar: que el cliente vea un
    precio que una persona todavía no revisó."""
    ventas._crm.modo_cotizacion = "manual"
    data = _cotizar(ventas)

    assert data["pdf_enviado"] is False
    assert data["motivo_pdf"] == "requiere_revision_de_vendedor"
    assert ventas._wa.enviados == []
    # Pero el vendedor sí tiene el archivo para revisarlo y mandarlo.
    assert ventas._crm.enviado[0]["pdf"].startswith(b"%PDF-")


def test_si_meta_rechaza_el_archivo_no_se_da_por_enviado(ventas):
    ventas._wa = WhatsAppQueFalla()
    data = _cotizar(ventas)

    assert data["pdf_enviado"] is False
    assert data["motivo_pdf"] == "fallo_al_enviar"
    # El motivo REAL de Meta viaja en la respuesta, no "hubo un error".
    assert "131030" in data["detalle"]
    # Y la cotización no se perdió por eso.
    assert len(ventas._crm.cotizaciones) == 1
    assert "NO digas que ya le llegó" in data["instruccion"]


def test_cada_motivo_por_el_que_no_sale_el_pdf_es_distinto(ventas):
    """Con un solo "no se pudo" para todo, el modelo rellena el hueco y le
    inventa al cliente una causa que nadie le dio."""
    ventas._crm.modo_cotizacion = "manual"
    manual = _cotizar(ventas)

    otro = VentasAgent.__new__(VentasAgent)
    otro._erp, otro._crm = MockERPClient(), MockCRMClient()
    otro._bus, otro._history_store = InMemoryEventBus(), InMemoryHistoryStore()
    otro._wa = WhatsAppQueFalla()
    fallo = _cotizar(otro)

    assert manual["motivo_pdf"] != fallo["motivo_pdf"]


def test_el_total_que_dice_el_bot_es_el_que_dice_el_pdf(ventas, monkeypatch):
    """Si el bot dijera el subtotal y el archivo el total, estaría diciendo una
    cifra y el documento otra en la misma conversación."""
    from app.config import get_settings
    from app.cotizacion_pdf import DatosCotizacion

    monkeypatch.setattr(get_settings(), "cotizacion_iva_tasa", 0.16)
    data = _cotizar(ventas)

    del_pdf = DatosCotizacion(
        folio=data["folio"],
        cliente="Molinos del Bajío",
        producto="Maíz blanco",
        cantidad_ton=10,
        precio_ton=6169.56,
        empresa="Intergranel",
        tasa_iva=0.16,
    )
    assert data["total"] == del_pdf.total == 71566.90
    # Y el CRM recibe la tasa, para que su tablero cuadre con las dos.
    assert ventas._crm.enviado[0]["tasa_iva"] == 0.16


def test_sin_iva_configurado_el_total_es_el_subtotal(ventas):
    data = _cotizar(ventas)
    assert data["total"] == 61695.60
    assert ventas._crm.enviado[0]["tasa_iva"] == 0.0


# --- El resumen que lee el vendedor ---------------------------------------- #


def test_el_resumen_de_la_platica_queda_como_nota_en_el_crm(ventas):
    asyncio.run(
        ventas._history_store.save(
            f"{PHONE}:ventas",
            [
                {"role": "user", "content": "¿A cómo el maíz blanco?"},
                {"role": "assistant", "content": [{"type": "text", "text": "$6,169.56"}]},
            ],
        )
    )
    data = _cotizar(ventas)

    assert len(ventas._crm.notas) == 1
    nota = ventas._crm.notas[0]
    assert nota["folio"] == data["folio"]
    # Los hechos no los redacta un modelo: salen de la cotización.
    assert "Maíz blanco" in nota["resumen"]
    assert "10.000 t" in nota["resumen"]
    assert "Ya recibió el PDF" in nota["resumen"]


def test_la_nota_dice_cuando_el_pdf_no_se_le_envio(ventas):
    ventas._crm.modo_cotizacion = "manual"
    _cotizar(ventas)
    assert "NO se le envió el PDF" in ventas._crm.notas[0]["resumen"]


def test_sin_cotizacion_no_hay_nota(ventas):
    """Si el CRM no la recibió, no hay de dónde colgar el resumen."""
    ventas._crm = CRMCaido()
    data = _cotizar(ventas)
    assert data["disponible"] is False
    assert ventas._crm.notas == []


def test_la_nota_no_hace_esperar_al_cliente(ventas):
    """Se lanza en segundo plano: lo que el cliente espera es su PDF."""

    async def escenario():
        crudo = await ventas.run_tool("generar_cotizacion", PEDIDO, PHONE)
        # La herramienta ya contestó y la nota todavía no está escrita.
        sin_escribir = list(ventas._crm.notas)
        await tareas.esperar_todo(timeout=5)
        return json.loads(crudo), sin_escribir

    data, sin_escribir = asyncio.run(escenario())
    assert data["disponible"] is True
    assert sin_escribir == []
    assert len(ventas._crm.notas) == 1


def test_cotizacion_sin_nombre_no_se_registra(ventas):
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "maíz blanco", "cantidad_ton": 10, "nombre_cliente": "   "},
    )
    assert data["disponible"] is False
    assert data["motivo"] == "falta_nombre_cliente"
    assert ventas._crm.cotizaciones == []


def test_no_se_cotiza_un_producto_sin_precio(ventas):
    """Rechaza por el MISMO motivo que consultar_precio: si no, el bot cotizaría
    lo que acaba de decir que no puede cotizar."""
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "sorgo dulce", "cantidad_ton": 10, "nombre_cliente": "Molinos"},
    )
    assert data["disponible"] is False
    assert data["motivo"] == "sin_precio_publicado"
    assert ventas._crm.cotizaciones == []


def test_no_se_cotiza_con_el_espejo_desactualizado(ventas):
    ventas._crm.desactualizado = True
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "maíz blanco", "cantidad_ton": 10, "nombre_cliente": "Molinos"},
    )
    assert data["disponible"] is False
    assert data["motivo"] == "datos_no_confiables"
    assert ventas._crm.cotizaciones == []


def test_cotizar_con_el_crm_caido_no_promete_nada(ventas):
    ventas._crm = CRMCaido()
    data = _run(
        ventas,
        "generar_cotizacion",
        {"producto": "maíz blanco", "cantidad_ton": 10, "nombre_cliente": "Molinos"},
    )
    assert data["disponible"] is False
    assert data["motivo"] == "crm_no_disponible"
    assert "total" not in data


def test_folio_lleva_segundos_para_no_pisar_otra_cotizacion():
    """El CRM deduplica por folio: dos cotizaciones del mismo cliente en el
    mismo minuto tienen que ser dos, no una encima de la otra."""
    a = folio_cotizacion(PHONE, datetime(2026, 9, 8, 6, 45, 12, tzinfo=UTC))
    b = folio_cotizacion(PHONE, datetime(2026, 9, 8, 6, 45, 49, tzinfo=UTC))
    assert a == "COT-20260908-064512-5678"
    assert a != b


# --- Lo que sigue siendo del ERP ------------------------------------------- #


def test_consultar_contrato_existente(ventas):
    data = _run(ventas, "consultar_contrato", {"folio": "CONT-2026-0001"})
    assert data["encontrado"] is True
    assert data["contrato"]["id"] == "CONT-2026-0001"


def test_listar_contratos_cliente(ventas):
    data = _run(ventas, "listar_contratos_cliente", {})
    assert data["total"] == 2


def test_solicitar_pedido_publica_en_bus(ventas):
    data = _run(ventas, "solicitar_pedido", {"producto": "soya", "cantidad_ton": 5})
    assert data["estado"] == "pendiente"
    evento = asyncio.run(ventas._bus.read(f"bus:ventas:solicitud:{PHONE}"))
    assert evento["producto"] == "soya"


def test_transferir_a_soporte_cambia_agente_activo(ventas):
    data = _run(ventas, "transferir_a_soporte", {"motivo": "reclamo"})
    assert data["transferido"] is True
    assert asyncio.run(ventas._bus.get_active_agent(PHONE)) == "soporte"


# --- Lo que NO se le dice al cliente: cuánto hay --------------------------- #


def test_consultar_precio_no_le_dice_al_modelo_cuanto_hay(ventas):
    """Cuánto grano hay en los silos no sale por WhatsApp. Un prompt se puede
    rodear; un dato que no está no se puede decir."""
    data = _run(ventas, "consultar_precio", {"producto": "maíz blanco"})
    assert data["disponibilidad"] == "stock"
    assert "existencia_ton" not in data
    assert 300.0 not in data.values()


def test_listar_productos_tampoco_lleva_existencias(ventas):
    data = _run(ventas, "listar_productos", {})
    for producto in data["productos"]:
        assert "existencia_ton" not in producto
        assert "existencia" not in producto


# --- El prompt ------------------------------------------------------------- #


def test_el_prompt_prohibe_decir_cuanto_inventario_hay():
    from app.agents.ventas import SYSTEM_PROMPT

    assert "NUNCA digas cuánto producto hay" in SYSTEM_PROMPT
    assert "en tránsito" in SYSTEM_PROMPT


def test_el_prompt_explica_que_el_pdf_lo_manda_la_herramienta():
    from app.agents.ventas import SYSTEM_PROMPT

    assert "pdf_enviado: true" in SYSTEM_PROMPT
    assert "pdf_enviado: false" in SYSTEM_PROMPT


def test_el_prompt_prohibe_mezclar_los_motivos():
    from app.agents.ventas import SYSTEM_PROMPT

    for motivo in (
        "no_esta_en_catalogo",
        "sin_precio_publicado",
        "datos_no_confiables",
        "crm_no_disponible",
    ):
        assert motivo in SYSTEM_PROMPT, f"el prompt no explica el motivo {motivo}"
    assert "PROHIBIDO" in SYSTEM_PROMPT
