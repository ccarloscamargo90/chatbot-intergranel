"""Cliente del CRM: de dónde salen los precios y a dónde van las cotizaciones.

El bot **no consulta el ERP para precios**. El ERP publica su catálogo al CRM,
el CRM lo espeja, y el bot le pregunta al CRM. Un solo sentido, y por eso el
bot no guarda ninguna credencial del ERP.

    GET  {CRM_BASE_URL}/ingest/catalog   -> catálogo con precio y frescura
    POST {CRM_BASE_URL}/ingest/quotes    -> registra la cotización del bot

La autenticación es la llave de agente del CRM (`X-Agent-Key`), y esa llave
decide a qué empresa entra y de qué empresa se lee. La empresa NUNCA viaja en
el cuerpo: quien tiene la llave de un bot no puede leer los precios de otra
empresa cambiando un campo del JSON.

Cuando el CRM no contesta, este módulo NO cae al ERP. Levanta
`CRMNoDisponible` y el agente ofrece un asesor. Un atajo al ERP el día que el
CRM está caído es exactamente el día en que la regla de dirección deja de ser
verdad, y nadie se entera hasta que dos sistemas dicen precios distintos.
"""

from __future__ import annotations

import abc
import unicodedata
from datetime import UTC, datetime

import httpx

from .config import get_settings
from .errores import detalle_http, detalle_respuesta
from .models import CatalogoCRM, CotizacionCRM, ProductoCRM

#: Header por el que viaja la llave del agente hacia el CRM.
AGENT_KEY_HEADER = "X-Agent-Key"


class CRMNoDisponible(RuntimeError):
    """El CRM no contestó, o contestó un error.

    Es su propia excepción para que el agente pueda decir la verdad —"ahora no
    puedo consultarlo"— en vez de confundirlo con "ese producto no existe".
    """


def normalizar(texto: str) -> str:
    """Minúsculas y sin acentos, para comparar lo que escribe un cliente.

    Quien pregunta por WhatsApp escribe "maiz", "MAÍZ" o "Maíz Blanco", y el
    catálogo dice "Maíz blanco". Sin esto, el bot le diría que no manejamos
    maíz.
    """
    sin_acentos = unicodedata.normalize("NFD", texto.strip().lower())
    return "".join(c for c in sin_acentos if unicodedata.category(c) != "Mn")


def buscar_producto(catalogo: CatalogoCRM, consulta: str) -> ProductoCRM | None:
    """El producto que mejor corresponde a lo que el cliente escribió.

    Se prefiere la coincidencia exacta del nombre o del SKU; si no, la primera
    que contenga TODAS las palabras de la consulta. "maíz blanco" no debe
    devolver "maíz amarillo" solo porque comparten una palabra.
    """
    objetivo = normalizar(consulta)
    if not objetivo:
        return None

    for p in catalogo.productos:
        if normalizar(p.nombre) == objetivo or normalizar(p.sku) == objetivo:
            return p

    palabras = objetivo.split()
    for p in catalogo.productos:
        nombre = normalizar(p.nombre)
        if all(palabra in nombre for palabra in palabras):
            return p
    return None


class CRMClient(abc.ABC):
    """Lo que el bot necesita del CRM: leer precios y depositar cotizaciones."""

    @abc.abstractmethod
    async def catalogo(self) -> CatalogoCRM:
        """El catálogo con precio de la empresa de la llave, con su frescura."""

    @abc.abstractmethod
    async def registrar_cotizacion(
        self,
        *,
        folio: str,
        nombre_cliente: str,
        telefono: str,
        producto: str,
        cantidad_ton: float,
        precio_ton: float,
        moneda: str = "MXN",
        notas: str | None = None,
    ) -> CotizacionCRM:
        """Deja la cotización en el CRM y devuelve lo que el CRM decidió."""


class HTTPCRMClient(CRMClient):
    """El CRM real, por HTTP."""

    def __init__(
        self,
        base_url: str,
        agent_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_key = agent_key
        self._transport = transport  # inyectable en pruebas

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=15,
            headers={AGENT_KEY_HEADER: self._agent_key},
            transport=self._transport,
        )

    async def catalogo(self) -> CatalogoCRM:
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._base_url}/ingest/catalog")
                if resp.status_code >= 400:
                    raise CRMNoDisponible(detalle_respuesta(resp, origen="CRM"))
                datos = resp.json()
        except CRMNoDisponible:
            raise
        except httpx.HTTPError as exc:
            raise CRMNoDisponible(detalle_http(exc, origen="CRM")) from exc

        return CatalogoCRM(
            productos=[
                ProductoCRM(
                    sku=item.get("sku", ""),
                    nombre=item.get("name", ""),
                    unidad=item.get("unit", "TNE"),
                    precio_unitario=(
                        float(item["unitPrice"]) if item.get("unitPrice") is not None else None
                    ),
                    moneda=item.get("currency", "MXN"),
                    disponibilidad=item.get("availability", "sobre_pedido"),
                    existencia=float(item.get("stockQuantity") or 0),
                )
                for item in datos.get("items", [])
            ],
            ultima_sync=datos.get("lastSyncAt"),
            desactualizado=bool(datos.get("stale", False)),
        )

    async def registrar_cotizacion(
        self,
        *,
        folio: str,
        nombre_cliente: str,
        telefono: str,
        producto: str,
        cantidad_ton: float,
        precio_ton: float,
        moneda: str = "MXN",
        notas: str | None = None,
    ) -> CotizacionCRM:
        cuerpo = {
            "folio": folio,
            "currency": moneda,
            "prospect": {
                "name": nombre_cliente,
                "phone": telefono,
                "productInterest": f"{producto} — {cantidad_ton:,.3f} t",
            },
            "lines": [
                {
                    "description": producto,
                    "quantity": f"{cantidad_ton}",
                    "unitPrice": f"{precio_ton}",
                }
            ],
            "contactMethod": "whatsapp",
            **({"notes": notas} if notas else {}),
        }
        try:
            async with self._client() as client:
                resp = await client.post(f"{self._base_url}/ingest/quotes", json=cuerpo)
                if resp.status_code >= 400:
                    raise CRMNoDisponible(detalle_respuesta(resp, origen="CRM"))
                datos = resp.json()
        except CRMNoDisponible:
            raise
        except httpx.HTTPError as exc:
            raise CRMNoDisponible(detalle_http(exc, origen="CRM")) from exc

        asignado = datos.get("assignedTo") or {}
        return CotizacionCRM(
            folio=folio,
            cotizacion_id=datos.get("quoteId", ""),
            prospecto_id=datos.get("prospectId", ""),
            producto=producto,
            cantidad_ton=cantidad_ton,
            precio_ton=precio_ton,
            total=round(cantidad_ton * precio_ton, 2),
            moneda=moneda,
            modo_cotizacion=datos.get("quotingMode", "automatic"),
            asignado_a=asignado.get("fullName"),
        )


class MockCRMClient(CRMClient):
    """CRM simulado para desarrollo y pruebas, sin red.

    Los precios son los que daría la calculadora del ERP con el ejemplo del
    contrato ($5,000/ton de costo + flete + merma + almacenaje + financiero +
    12% de margen = $6,169.56).
    """

    def __init__(self) -> None:
        self._productos = [
            ProductoCRM(
                sku="MAIZ-BL",
                nombre="Maíz blanco",
                unidad="TNE",
                precio_unitario=6169.56,
                disponibilidad="stock",
                existencia=300.0,
            ),
            ProductoCRM(
                sku="MAIZ-AM",
                nombre="Maíz amarillo",
                unidad="TNE",
                precio_unitario=5890.00,
                disponibilidad="stock",
                existencia=180.0,
            ),
            ProductoCRM(
                sku="TRIGO-CR",
                nombre="Trigo cristalino",
                unidad="TNE",
                precio_unitario=7420.50,
                disponibilidad="en_transito",
                existencia=0.0,
            ),
            # Se vende, pero nadie le ha puesto precio: el bot tiene que poder
            # decir "sí lo manejo, no tengo precio ahora" y no "no lo manejo".
            ProductoCRM(
                sku="SORGO",
                nombre="Sorgo dulce",
                unidad="TNE",
                precio_unitario=None,
                disponibilidad="stock",
                existencia=95.0,
            ),
        ]
        self.cotizaciones: list[CotizacionCRM] = []
        #: Lo enciende una prueba para ver qué hace el bot con datos viejos.
        self.desactualizado = False

    async def catalogo(self) -> CatalogoCRM:
        return CatalogoCRM(
            productos=list(self._productos),
            ultima_sync=datetime.now(UTC).isoformat(),
            desactualizado=self.desactualizado,
        )

    async def registrar_cotizacion(
        self,
        *,
        folio: str,
        nombre_cliente: str,
        telefono: str,
        producto: str,
        cantidad_ton: float,
        precio_ton: float,
        moneda: str = "MXN",
        notas: str | None = None,
    ) -> CotizacionCRM:
        cotizacion = CotizacionCRM(
            folio=folio,
            cotizacion_id=f"q-{len(self.cotizaciones) + 1}",
            prospecto_id=f"p-{telefono}",
            producto=producto,
            cantidad_ton=cantidad_ton,
            precio_ton=precio_ton,
            total=round(cantidad_ton * precio_ton, 2),
            moneda=moneda,
            modo_cotizacion="automatic",
            asignado_a="Vendedor de guardia",
        )
        self.cotizaciones.append(cotizacion)
        return cotizacion


def get_crm_client() -> CRMClient:
    """El CRM real si está configurado; el simulado si no."""
    settings = get_settings()
    if settings.use_mock_crm:
        return MockCRMClient()
    return HTTPCRMClient(settings.crm_base_url, settings.crm_agent_key)
