"""Cliente del CRM: de dónde salen los precios y a dónde van las cotizaciones.

El bot **no consulta el ERP para precios**. El ERP publica su catálogo al CRM,
el CRM lo espeja, y el bot le pregunta al CRM. Un solo sentido, y por eso el
bot no guarda ninguna credencial del ERP.

    GET  {CRM_BASE_URL}/ingest/catalog      -> catálogo con precio y frescura
    POST {CRM_BASE_URL}/ingest/quotes       -> registra la cotización (con su PDF)
    POST {CRM_BASE_URL}/ingest/quote-notes  -> el resumen de la plática, como nota
    POST {CRM_BASE_URL}/ingest/handoffs     -> el resumen al pedir un asesor

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
import base64
import unicodedata
from datetime import UTC, date, datetime

import httpx

from .config import get_settings
from .errores import detalle_http, detalle_respuesta
from .models import CanalizacionCRM, CatalogoCRM, CotizacionCRM, NotaCRM, ProductoCRM

#: Header por el que viaja la llave del agente hacia el CRM.
AGENT_KEY_HEADER = "X-Agent-Key"


class CRMNoDisponible(RuntimeError):
    """El CRM no contestó, o contestó un error.

    Es su propia excepción para que el agente pueda decir la verdad —"ahora no
    puedo consultarlo"— en vez de confundirlo con "ese producto no existe".
    """


class CotizacionAunNoRegistrada(RuntimeError):
    """El CRM todavía no ve la cotización a la que se le quiere colgar la nota.

    El CRM contesta 409 —y no 404— a propósito: significa "todavía no", no
    "no existe". Es su propia excepción, y NO hereda de `CRMNoDisponible`,
    porque la reacción correcta es distinta: aquí se reintenta en unos
    segundos; ahí se deja de insistir.
    """


def normalizar(texto: str) -> str:
    """Minúsculas y sin acentos, para comparar lo que escribe un cliente.

    Quien pregunta por WhatsApp escribe "maiz", "MAÍZ" o "Maíz Blanco", y el
    catálogo dice "Maíz blanco". Sin esto, el bot le diría que no manejamos
    maíz.
    """
    sin_acentos = unicodedata.normalize("NFD", texto.strip().lower())
    return "".join(c for c in sin_acentos if unicodedata.category(c) != "Mn")


def _total_con_iva(cantidad_ton: float, precio_ton: float, tasa_iva: float) -> float:
    """El total que se le dice al cliente, que es el que dice su PDF.

    Se redondea en los MISMOS pasos que el PDF —subtotal, luego impuesto, luego
    la suma— para que las dos cifras no puedan separarse por un centavo. Con la
    tasa en cero (la de fábrica) es el subtotal pelón, igual que antes de que
    el IVA fuera configurable.
    """
    subtotal = round(cantidad_ton * precio_ton, 2)
    return round(subtotal + round(subtotal * tasa_iva, 2), 2)


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
    """Lo que el bot necesita del CRM: leer precios, depositar cotizaciones con
    su PDF y dejarle al vendedor el resumen de lo que se habló."""

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
        pdf: bytes | None = None,
        pdf_nombre: str | None = None,
        vigencia_hasta: date | None = None,
        tasa_iva: float = 0.0,
    ) -> CotizacionCRM:
        """Deja la cotización en el CRM y devuelve lo que el CRM decidió.

        El `pdf` es el MISMO archivo que recibió el cliente por WhatsApp. Va
        aquí y no en una llamada aparte para que no exista el hueco en el que
        el cliente tiene un documento que el vendedor no puede ver.
        """

    @abc.abstractmethod
    async def registrar_nota_cotizacion(self, *, folio: str, resumen: str) -> NotaCRM:
        """Cuelga el resumen de la plática como nota del prospecto.

        Va en su propia llamada —después de la cotización— porque el resumen
        lo redacta un modelo y eso tarda: metido en la cotización, el cliente
        esperaría por su PDF mientras se escribe una nota que él no va a leer.

        Levanta `CotizacionAunNoRegistrada` si el CRM todavía no ve el folio.
        """

    @abc.abstractmethod
    async def registrar_canalizacion(
        self,
        *,
        id_externo: str,
        nombre_cliente: str,
        telefono: str,
        resumen: str,
        motivo: str | None = None,
        folio_cotizacion: str | None = None,
        rfc: str | None = None,
    ) -> CanalizacionCRM:
        """El bot mandó al cliente con un asesor: deja el resumen como nota.

        Crea el prospecto si no existía, así que sirve también para quien
        pidió un asesor sin llegar a cotizar.
        """


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
            timeout=30,  # con el PDF adentro, la cotización ya no es un JSON chico
            headers={AGENT_KEY_HEADER: self._agent_key},
            transport=self._transport,
        )

    async def _post(
        self, ruta: str, cuerpo: dict, *, pendiente_en_conflicto: bool = False
    ) -> dict:
        """POST a la ingesta, con la llave y un solo motivo de fallo.

        El 409 solo se traduce a "todavía no" donde eso significa algo (la
        nota de una cotización). En las demás rutas un 409 sería un error como
        cualquier otro, y hacerlo pasar por "reintenta en unos segundos"
        mandaría a reintentar lo que nunca va a funcionar.
        """
        try:
            async with self._client() as client:
                resp = await client.post(f"{self._base_url}{ruta}", json=cuerpo)
                if pendiente_en_conflicto and resp.status_code == 409:
                    raise CotizacionAunNoRegistrada(detalle_respuesta(resp, origen="CRM"))
                if resp.status_code >= 400:
                    raise CRMNoDisponible(detalle_respuesta(resp, origen="CRM"))
                return resp.json()
        except (CRMNoDisponible, CotizacionAunNoRegistrada):
            raise
        except httpx.HTTPError as exc:
            raise CRMNoDisponible(detalle_http(exc, origen="CRM")) from exc

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
        pdf: bytes | None = None,
        pdf_nombre: str | None = None,
        vigencia_hasta: date | None = None,
        tasa_iva: float = 0.0,
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
            # El PDF va en base64 y SIN el prefijo `data:`, como pide el CRM.
            **(
                {
                    "pdfBase64": base64.standard_b64encode(pdf).decode(),
                    "pdfFilename": pdf_nombre or f"Cotizacion-{folio}.pdf",
                }
                if pdf
                else {}
            ),
            # La tasa solo viaja si alguien la configuró: así el total del CRM
            # es el mismo número que el del PDF que recibió el cliente.
            **({"taxRate": f"{tasa_iva}"} if tasa_iva > 0 else {}),
            # Fin del día y no la fecha pelona: el CRM la guarda como instante
            # ("2026-09-19" se vuelve la medianoche UTC) y en México eso se
            # vería como el día 18. El PDF del cliente y el tablero del
            # vendedor tienen que decir el mismo día.
            **(
                {"validUntil": f"{vigencia_hasta.isoformat()}T23:59:59Z"}
                if vigencia_hasta
                else {}
            ),
        }
        datos = await self._post("/ingest/quotes", cuerpo)

        asignado = datos.get("assignedTo") or {}
        return CotizacionCRM(
            folio=folio,
            cotizacion_id=datos.get("quoteId", ""),
            prospecto_id=datos.get("prospectId", ""),
            producto=producto,
            cantidad_ton=cantidad_ton,
            precio_ton=precio_ton,
            # Con el IVA dentro, como en el PDF. Este `total` es el número que
            # el modelo le dice al cliente: si aquí fuera el subtotal y en el
            # archivo el total, el bot estaría diciendo una cifra y el
            # documento otra, en la misma conversación.
            total=_total_con_iva(cantidad_ton, precio_ton, tasa_iva),
            moneda=moneda,
            modo_cotizacion=datos.get("quotingMode", "automatic"),
            asignado_a=asignado.get("fullName"),
        )

    async def registrar_nota_cotizacion(self, *, folio: str, resumen: str) -> NotaCRM:
        datos = await self._post(
            "/ingest/quote-notes",
            {"folio": folio, "summary": resumen},
            pendiente_en_conflicto=True,
        )
        return NotaCRM(
            prospecto_id=datos.get("prospectId", ""),
            nota_id=datos.get("noteId", ""),
            actualizada=bool(datos.get("updated", False)),
        )

    async def registrar_canalizacion(
        self,
        *,
        id_externo: str,
        nombre_cliente: str,
        telefono: str,
        resumen: str,
        motivo: str | None = None,
        folio_cotizacion: str | None = None,
        rfc: str | None = None,
    ) -> CanalizacionCRM:
        cuerpo = {
            "externalId": id_externo,
            "summary": resumen,
            "prospect": {
                "name": nombre_cliente,
                "phone": telefono,
                # El RFC solo viaja si el cliente se identificó: es lo que le
                # permite al vendedor reconocer en el CRM a la empresa que ya
                # es cliente, en vez de tratarla como un prospecto nuevo.
                **({"taxId": rfc} if rfc else {}),
            },
            "contactMethod": "whatsapp",
            **({"reason": motivo} if motivo else {}),
            **({"quoteFolio": folio_cotizacion} if folio_cotizacion else {}),
        }
        datos = await self._post("/ingest/handoffs", cuerpo)
        asignado = datos.get("assignedTo") or {}
        return CanalizacionCRM(
            prospecto_id=datos.get("prospectId", ""),
            nota_id=datos.get("noteId", ""),
            prospecto_creado=bool(datos.get("prospectCreated", False)),
            actualizada=bool(datos.get("updated", False)),
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
        #: Lo que se mandó con cada cotización, para poder revisarlo en pruebas:
        #: sin esto no hay forma de comprobar que el PDF viajó al CRM.
        self.enviado: list[dict] = []
        self.notas: list[dict] = []
        self.canalizaciones: list[dict] = []
        #: Lo enciende una prueba para ver qué hace el bot con datos viejos.
        self.desactualizado = False
        #: "manual" = un vendedor revisa antes de que el cliente vea el precio.
        self.modo_cotizacion = "automatic"

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
        pdf: bytes | None = None,
        pdf_nombre: str | None = None,
        vigencia_hasta: date | None = None,
        tasa_iva: float = 0.0,
    ) -> CotizacionCRM:
        cotizacion = CotizacionCRM(
            folio=folio,
            cotizacion_id=f"q-{len(self.cotizaciones) + 1}",
            prospecto_id=f"p-{telefono}",
            producto=producto,
            cantidad_ton=cantidad_ton,
            precio_ton=precio_ton,
            total=_total_con_iva(cantidad_ton, precio_ton, tasa_iva),
            moneda=moneda,
            modo_cotizacion=self.modo_cotizacion,
            asignado_a="Vendedor de guardia",
        )
        self.cotizaciones.append(cotizacion)
        self.enviado.append(
            {
                "folio": folio,
                "nombre_cliente": nombre_cliente,
                "telefono": telefono,
                "pdf": pdf,
                "pdf_nombre": pdf_nombre,
                "vigencia_hasta": vigencia_hasta,
                "tasa_iva": tasa_iva,
                "notas": notas,
            }
        )
        return cotizacion

    async def registrar_nota_cotizacion(self, *, folio: str, resumen: str) -> NotaCRM:
        # Igual que el CRM real: la nota se cuelga de una cotización que ya
        # existe. Si el folio no está, es "todavía no" y se reintenta.
        if not any(c.folio == folio for c in self.cotizaciones):
            raise CotizacionAunNoRegistrada(f"CRM: la cotización {folio} no está (aún)")
        self.notas.append({"folio": folio, "resumen": resumen})
        return NotaCRM(
            prospecto_id=f"p-{folio}", nota_id=f"n-{len(self.notas)}", actualizada=False
        )

    async def registrar_canalizacion(
        self,
        *,
        id_externo: str,
        nombre_cliente: str,
        telefono: str,
        resumen: str,
        motivo: str | None = None,
        folio_cotizacion: str | None = None,
        rfc: str | None = None,
    ) -> CanalizacionCRM:
        self.canalizaciones.append(
            {
                "id_externo": id_externo,
                "nombre_cliente": nombre_cliente,
                "telefono": telefono,
                "resumen": resumen,
                "motivo": motivo,
                "folio_cotizacion": folio_cotizacion,
                "rfc": rfc,
            }
        )
        return CanalizacionCRM(
            prospecto_id=f"p-{telefono}",
            nota_id=f"n-{len(self.canalizaciones)}",
            prospecto_creado=True,
            asignado_a="Vendedor de guardia",
        )


def get_crm_client() -> CRMClient:
    """El CRM real si está configurado; el simulado si no."""
    settings = get_settings()
    if settings.use_mock_crm:
        return MockCRMClient()
    return HTTPCRMClient(settings.crm_base_url, settings.crm_agent_key)
