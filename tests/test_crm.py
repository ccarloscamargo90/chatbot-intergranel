"""Pruebas del cliente del CRM: la fuente de precio del bot.

El HTTP se ejercita con un transporte inyectado (sin red): un mock que acepta
cualquier cosa no probaría que mandamos la llave ni que leemos bien el JSON
del CRM.
"""

import asyncio
import base64
import json
from datetime import date

import httpx
import pytest

from app.crm import (
    AGENT_KEY_HEADER,
    CotizacionAunNoRegistrada,
    CRMNoDisponible,
    HTTPCRMClient,
    MockCRMClient,
    buscar_producto,
    normalizar,
)
from app.models import CatalogoCRM, ProductoCRM

CATALOGO_JSON = {
    "items": [
        {
            "sku": "MAIZ-BL",
            "name": "Maíz blanco",
            "unit": "TNE",
            "unitPrice": "6169.56",
            "currency": "MXN",
            "availability": "stock",
            "stockQuantity": "300",
        },
        {
            "sku": "SORGO",
            "name": "Sorgo dulce",
            "unit": "TNE",
            "unitPrice": None,
            "currency": "MXN",
            "availability": "stock",
            "stockQuantity": "95",
        },
    ],
    "lastSyncAt": "2026-09-08T06:00:00.000Z",
    "stale": False,
}


def _cliente(handler) -> HTTPCRMClient:
    return HTTPCRMClient(
        "https://crm.example.com/api",
        agent_key="llave-de-intergranel",
        transport=httpx.MockTransport(handler),
    )


# --- Buscar lo que el cliente escribió ------------------------------------- #


def test_normalizar_quita_acentos_y_mayusculas():
    assert normalizar("  MAÍZ Blanco ") == "maiz blanco"


def _catalogo() -> CatalogoCRM:
    return CatalogoCRM(
        productos=[
            ProductoCRM(sku="MAIZ-BL", nombre="Maíz blanco", precio_unitario=6169.56),
            ProductoCRM(sku="MAIZ-AM", nombre="Maíz amarillo", precio_unitario=5890.0),
        ]
    )


def test_encuentra_por_nombre_sin_acentos():
    p = buscar_producto(_catalogo(), "maiz blanco")
    assert p is not None and p.sku == "MAIZ-BL"


def test_encuentra_por_sku():
    p = buscar_producto(_catalogo(), "MAIZ-AM")
    assert p is not None and p.nombre == "Maíz amarillo"


def test_no_entrega_el_amarillo_cuando_piden_el_blanco():
    """Comparten la palabra "maíz"; entregar el otro sería cotizar un grano por
    otro, con su precio."""
    p = buscar_producto(_catalogo(), "maíz blanco")
    assert p is not None and p.sku == "MAIZ-BL"


def test_lo_que_no_esta_devuelve_nada():
    assert buscar_producto(_catalogo(), "café") is None


def test_consulta_vacia_no_devuelve_el_primero():
    assert buscar_producto(_catalogo(), "   ") is None


# --- El catálogo por HTTP -------------------------------------------------- #


def test_catalogo_manda_la_llave_y_mapea_el_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ingest/catalog"
        assert request.headers[AGENT_KEY_HEADER] == "llave-de-intergranel"
        return httpx.Response(200, json=CATALOGO_JSON)

    catalogo = asyncio.run(_cliente(handler).catalogo())

    assert catalogo.desactualizado is False
    assert catalogo.ultima_sync == "2026-09-08T06:00:00.000Z"
    assert len(catalogo.productos) == 2
    maiz = catalogo.productos[0]
    assert maiz.nombre == "Maíz blanco"
    assert maiz.precio_unitario == 6169.56
    assert maiz.existencia == 300.0
    # El que no tiene precio llega con None, no con cero: cero es un precio.
    assert catalogo.productos[1].precio_unitario is None


def test_catalogo_conserva_la_bandera_de_dato_viejo():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**CATALOGO_JSON, "stale": True})

    catalogo = asyncio.run(_cliente(handler).catalogo())
    assert catalogo.desactualizado is True


def test_un_error_del_crm_no_pasa_por_catalogo_vacio():
    """Un 500 devuelto como lista vacía haría que el bot dijera "no manejamos
    nada". Tiene que ser distinguible de "no hay productos"."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(CRMNoDisponible):
        asyncio.run(_cliente(handler).catalogo())


def test_llave_invalida_es_crm_no_disponible():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Clave de ingesta inválida."})

    with pytest.raises(CRMNoDisponible) as exc:
        asyncio.run(_cliente(handler).catalogo())
    assert "inválida" in str(exc.value)


def test_crm_sin_red_es_crm_no_disponible():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(CRMNoDisponible):
        asyncio.run(_cliente(handler).catalogo())


# --- Registrar la cotización ----------------------------------------------- #


def test_registrar_cotizacion_arma_el_cuerpo_que_el_crm_espera():
    enviado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        assert request.url.path == "/api/ingest/quotes"
        assert request.headers[AGENT_KEY_HEADER] == "llave-de-intergranel"
        enviado.update(_json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "quoteId": "q-1",
                "prospectId": "p-1",
                "quotingMode": "automatic",
                "updated": False,
                "assignedTo": {
                    "userId": "u-1",
                    "fullName": "Ana Ruiz",
                    "whatsappPhone": "5215500000000",
                },
            },
        )

    cot = asyncio.run(
        _cliente(handler).registrar_cotizacion(
            folio="COT-20260908-064512-5678",
            nombre_cliente="Molinos del Bajío",
            telefono="5215512345678",
            producto="Maíz blanco",
            cantidad_ton=10,
            precio_ton=6169.56,
        )
    )

    # La EMPRESA no viaja en el cuerpo: la decide la llave.
    assert "company" not in enviado and "empresa" not in enviado
    assert enviado["prospect"]["name"] == "Molinos del Bajío"
    assert enviado["prospect"]["phone"] == "5215512345678"
    assert enviado["contactMethod"] == "whatsapp"
    assert enviado["lines"][0]["description"] == "Maíz blanco"

    assert cot.total == 61695.60
    assert cot.asignado_a == "Ana Ruiz"
    assert cot.modo_cotizacion == "automatic"


def test_cotizacion_rechazada_por_el_crm_no_se_da_por_registrada():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "folio inválido"})

    with pytest.raises(CRMNoDisponible):
        asyncio.run(
            _cliente(handler).registrar_cotizacion(
                folio="x",
                nombre_cliente="Molinos",
                telefono="5215512345678",
                producto="Maíz blanco",
                cantidad_ton=10,
                precio_ton=6169.56,
            )
        )


# --- El PDF viaja con la cotización ---------------------------------------- #


def _respuesta_de_cotizacion() -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "quoteId": "q-1",
            "prospectId": "p-1",
            "quotingMode": "automatic",
            "updated": False,
            "assignedTo": None,
        },
    )


def test_el_pdf_viaja_en_base64_sin_el_prefijo_data():
    """Es el MISMO archivo que recibió el cliente. Va en la misma llamada para
    que no exista el hueco en el que el cliente tiene un documento que el
    vendedor no puede ver."""
    enviado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        enviado.update(json.loads(request.content))
        return _respuesta_de_cotizacion()

    asyncio.run(
        _cliente(handler).registrar_cotizacion(
            folio="COT-1",
            nombre_cliente="Molinos",
            telefono="5215512345678",
            producto="Maíz blanco",
            cantidad_ton=10,
            precio_ton=6169.56,
            pdf=b"%PDF-1.3 fake",
            pdf_nombre="Cotizacion-COT-1.pdf",
        )
    )

    assert not enviado["pdfBase64"].startswith("data:")
    assert base64.standard_b64decode(enviado["pdfBase64"]) == b"%PDF-1.3 fake"
    assert enviado["pdfFilename"] == "Cotizacion-COT-1.pdf"


def test_sin_pdf_no_se_manda_el_campo_vacio():
    enviado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        enviado.update(json.loads(request.content))
        return _respuesta_de_cotizacion()

    asyncio.run(
        _cliente(handler).registrar_cotizacion(
            folio="COT-1",
            nombre_cliente="Molinos",
            telefono="5215512345678",
            producto="Maíz blanco",
            cantidad_ton=10,
            precio_ton=6169.56,
        )
    )
    assert "pdfBase64" not in enviado
    assert "taxRate" not in enviado
    assert "validUntil" not in enviado


def test_el_iva_y_la_vigencia_solo_viajan_si_estan_configurados():
    """La vigencia va al FIN DEL DÍA: el CRM la guarda como instante, y
    "2026-09-19" pelón se vería en México como el día 18."""
    enviado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        enviado.update(json.loads(request.content))
        return _respuesta_de_cotizacion()

    asyncio.run(
        _cliente(handler).registrar_cotizacion(
            folio="COT-1",
            nombre_cliente="Molinos",
            telefono="5215512345678",
            producto="Maíz blanco",
            cantidad_ton=10,
            precio_ton=6169.56,
            tasa_iva=0.16,
            vigencia_hasta=date(2026, 9, 19),
        )
    )
    assert enviado["taxRate"] == "0.16"
    assert enviado["validUntil"] == "2026-09-19T23:59:59Z"


# --- El resumen como nota del prospecto ------------------------------------ #


def test_la_nota_se_cuelga_del_folio():
    enviado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ingest/quote-notes"
        assert request.headers[AGENT_KEY_HEADER] == "llave-de-intergranel"
        enviado.update(json.loads(request.content))
        return httpx.Response(201, json={"prospectId": "p-1", "noteId": "n-1", "updated": False})

    nota = asyncio.run(
        _cliente(handler).registrar_nota_cotizacion(
            folio="COT-1", resumen="- Es para su planta en Celaya."
        )
    )

    assert enviado == {"folio": "COT-1", "summary": "- Es para su planta en Celaya."}
    assert nota.prospecto_id == "p-1"
    assert nota.actualizada is False


def test_un_409_significa_todavia_no_y_se_puede_reintentar():
    """El CRM contesta 409 —y no 404— cuando aún no ve la cotización. Tratarlo
    como "no existe" tiraría el resumen."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "La cotización COT-1 no está ingestada."})

    with pytest.raises(CotizacionAunNoRegistrada):
        asyncio.run(_cliente(handler).registrar_nota_cotizacion(folio="COT-1", resumen="x"))


def test_un_error_de_verdad_del_crm_no_se_confunde_con_todavia_no():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(CRMNoDisponible):
        asyncio.run(_cliente(handler).registrar_nota_cotizacion(folio="COT-1", resumen="x"))


def test_un_409_en_la_cotizacion_no_se_lee_como_todavia_no():
    """Ese 409 solo significa "todavía no" en la nota. En otra ruta sería un
    error como cualquier otro, y mandar a reintentar lo que nunca va a
    funcionar es peor que decir que falló."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "conflicto"})

    with pytest.raises(CRMNoDisponible):
        asyncio.run(
            _cliente(handler).registrar_cotizacion(
                folio="COT-1",
                nombre_cliente="Molinos",
                telefono="5215512345678",
                producto="Maíz blanco",
                cantidad_ton=10,
                precio_ton=6169.56,
            )
        )


# --- La canalización con un asesor ----------------------------------------- #


def test_la_canalizacion_manda_el_resumen_y_lo_que_liga_al_prospecto():
    enviado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ingest/handoffs"
        enviado.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "prospectId": "p-9",
                "noteId": "n-9",
                "prospectCreated": True,
                "updated": False,
                "assignedTo": {
                    "userId": "u-1",
                    "fullName": "Ana Ruiz",
                    "whatsappPhone": "5215500000000",
                },
            },
        )

    resultado = asyncio.run(
        _cliente(handler).registrar_canalizacion(
            id_externo="handoff-5215512345678-20260914-0645",
            nombre_cliente="Molinos del Bajío S.A. de C.V.",
            telefono="5215512345678",
            resumen="- Pidió hablar con una persona.",
            motivo="reclamo por merma",
            folio_cotizacion="COT-1",
            rfc="MBA950101AB1",
        )
    )

    assert "company" not in enviado and "empresa" not in enviado
    assert enviado["externalId"] == "handoff-5215512345678-20260914-0645"
    assert enviado["prospect"]["taxId"] == "MBA950101AB1"
    assert enviado["quoteFolio"] == "COT-1"
    assert enviado["contactMethod"] == "whatsapp"
    assert resultado.prospecto_creado is True
    assert resultado.asignado_a == "Ana Ruiz"


def test_sin_rfc_el_campo_no_viaja():
    enviado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        enviado.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={"prospectId": "p-9", "noteId": "n-9", "prospectCreated": True,
                  "updated": False, "assignedTo": None},
        )

    asyncio.run(
        _cliente(handler).registrar_canalizacion(
            id_externo="h-1",
            nombre_cliente="WhatsApp 5215512345678",
            telefono="5215512345678",
            resumen="- Pidió un asesor.",
        )
    )
    assert "taxId" not in enviado["prospect"]
    assert "quoteFolio" not in enviado


# --- El CRM simulado ------------------------------------------------------- #


def test_el_mock_trae_un_producto_sin_precio_a_proposito():
    """Para que las pruebas puedan ejercitar la diferencia entre "no lo
    vendemos" y "sí, pero no tiene precio"."""
    catalogo = asyncio.run(MockCRMClient().catalogo())
    assert any(p.precio_unitario is None for p in catalogo.productos)
