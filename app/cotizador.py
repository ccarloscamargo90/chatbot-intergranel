"""Cotizar con botones: el flujo guiado del guion de ventas.

Antes, cotizar dependía de que el modelo se acordara de poner la marca de
botones en cada pregunta. En la pregunta más importante —¿qué grano?— no había
marca que poner, así que el cliente recibía la lista de granos como TEXTO, con
[📋 Menú] [👤 Asesor] abajo, y tenía que escribir lo que quería.

Ahora la cotización es un flujo definido. Cada respuesta llena un dato y el
flujo pregunta el siguiente que falte, siempre con sus botones:

    grano ─► toneladas ─► presentación ─► costal ─► dónde lo recibe ─► nombre
      │        (maíz blanco: 40 t / hasta 6 t / otra)       │
      │                                                     ▼
      └─ lista del catálogo del CRM            resumen [✅ Generar] [✏️ Cambiar]
                                                            │
                                  PDF ◄─────────────────────┘
                                   └─► [🤝 Cerrar pedido] [🔁 Otra cotización]

Tres reglas:

1. **Las opciones salen de los datos, no del código.** La lista de granos se
   arma con el catálogo del CRM en el momento; un grano sin precio o un espejo
   viejo se dicen como tales, igual que en la herramienta del modelo.
2. **Lo que no es respuesta al paso, va al modelo.** Una duda a media
   cotización ("¿el flete cuánto?") la contesta el agente de Ventas, y su
   respuesta sale con los botones del paso pendiente para retomar con un toque.
3. **El guion manda las reglas de volumen.** Unidad de 40 t, entregas de hasta
   6 t solo en Querétaro, Irapuato, Celaya y León, y lo demás con un asesor.
   Son del maíz blanco (`reglas_de`): a otro grano no se le extienden.

La cotización se registra por el MISMO camino que la herramienta
`generar_cotizacion` (CRM, PDF por WhatsApp y nota al vendedor): dos caminos
que registran distinto terminarían en dos cotizaciones que no cuadran.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .bus import EventBus
from .crm import CRMNoDisponible, buscar_producto, normalizar
from .menus import (
    ASESOR,
    BOTONES_COTIZACION,
    CAMBIOS_COTIZACION,
    COT_CAMBIAR,
    COT_CAMBIAR_ENTREGA,
    COT_CAMBIAR_GRANO,
    COT_CAMBIAR_NOMBRE,
    COT_CAMBIAR_PRESENTACION,
    COT_CAMBIAR_TONELADAS,
    COT_CERRAR,
    COT_CON_MARCA,
    COT_COSTAL_25,
    COT_COSTAL_50,
    COT_ENTREGA_CHICA,
    COT_GENERAR,
    COT_OTRA_CANTIDAD,
    COT_OTRA_COTIZACION,
    COT_SIN_MARCA,
    COT_TRANSPARENTE,
    COT_UNIDAD_COMPLETA,
    COT_ZONA_OTRA,
    COTIZAR,
    MENU,
    PREFIJO_PRODUCTO,
    ZONAS_ENTREGA_CHICA,
)
from .models import CatalogoCRM, ProductoCRM
from .replies import Boton, MenuLista, OpcionLista, Reply
from .sesiones import SesionClienteStore

if TYPE_CHECKING:
    from .agents.ventas import VentasAgent

logger = logging.getLogger(__name__)

#: Cuánto vive una cotización a medias. Quien fue a preguntar cuántas toneladas
#: le caben en la bodega vuelve en una hora, no en una semana.
ESTADO_TTL_SECONDS = 6 * 3600

#: Tope de toneladas que se acepta escribir: más que eso es un dedo de más.
MAX_TONELADAS = 10_000

UNIDAD_COMPLETA_TON = 40.0
ENTREGA_CHICA_MAX_TON = 6.0

BOTON_ASESOR = Boton(ASESOR, "👤 Asesor")
BOTON_MENU = Boton(MENU, "📋 Menú")
BOTON_GENERAR = Boton(COT_GENERAR, "✅ Generar")
BOTON_CAMBIAR = Boton(COT_CAMBIAR, "✏️ Cambiar algo")
BOTON_CERRAR = Boton(COT_CERRAR, "🤝 Cerrar pedido")
BOTON_OTRA = Boton(COT_OTRA_COTIZACION, "🔁 Otra cotización")
BOTON_OTRA_CANTIDAD = Boton(COT_OTRA_CANTIDAD, "✏️ Otra cantidad")
BOTON_UNIDAD_COMPLETA = Boton(COT_UNIDAD_COMPLETA, "🚛 Camión de 40 t")

DISPONIBILIDAD = {
    "stock": "Disponible",
    "en_transito": "En tránsito",
    "sobre_pedido": "Sobre pedido",
}

PRESENTACIONES = {COT_COSTAL_25: "costal de 25 kg", COT_COSTAL_50: "costal de 50 kg"}
COSTALES = {
    COT_CON_MARCA: "con_marca",
    COT_SIN_MARCA: "sin_marca",
    COT_TRANSPARENTE: "transparente",
}
COSTAL_LEGIBLE = {
    "con_marca": "con marca",
    "sin_marca": "sin marca",
    "transparente": "transparente",
}

#: Lo que NO es una respuesta: saludos y evasivas que no pueden ser un nombre ni
#: un lugar de entrega.
_NO_ES_RESPUESTA = {
    "hola", "buenas", "buen dia", "buenos dias", "buenas tardes", "buenas noches", "gracias",
    "ok", "si", "no", "no se", "nose", "luego", "despues", "ahorita", "mande", "va",
}
_INTERROGATIVAS = (
    "cuanto", "cuanta", "cual", "cuales", "que ", "como", "donde", "cuando", "por que",
    "porque", "tienen", "hay ", "puedo", "pueden", "hacen", "manejan", "incluye",
)
_CP = re.compile(r"\b(\d{5})\b")
_LIGA = re.compile(r"https?://\S+")
_NUMERO = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class Reglas:
    """Lo que el guion de ventas pide preguntar de un producto."""

    #: Volumen con los botones del guion (40 t / hasta 6 t / otra) y sus reglas.
    volumen_del_guion: bool = False
    #: Presentación (costal de 25/50 kg) y tipo de costal.
    costal: bool = False


def reglas_de(nombre_producto: str) -> Reglas:
    """Las reglas del guion para un producto.

    El guion (v1.1) es del maíz blanco: presentación, costal, la unidad de 40 t
    y las entregas chicas por zona. A otro grano NO se le extienden —el prompt
    del agente tiene la misma prohibición—; si mañana el guion cubre otro, se
    agrega aquí y no en cada pregunta.
    """
    if "maiz blanco" in normalizar(nombre_producto):
        return Reglas(volumen_del_guion=True, costal=True)
    return Reglas()


def es_pregunta(texto: str) -> bool:
    """¿Esto es una duda (para el modelo) y no la respuesta al paso?

    Las ligas no cuentan: la de Google Maps que acompaña una ubicación
    compartida lleva un "?" y no es ninguna pregunta.
    """
    sin_ligas = _LIGA.sub("", texto or "")
    if "?" in sin_ligas or "¿" in sin_ligas:
        return True
    return normalizar(sin_ligas).startswith(_INTERROGATIVAS)


def leer_toneladas(texto: str) -> float | None:
    """Toneladas escritas: "30", "30 t", "12.5 toneladas". Un solo número."""
    numeros = _NUMERO.findall(texto or "")
    if len(numeros) != 1:
        return None
    crudo = numeros[0]
    # "1,000" son mil; "12,5" es doce y medio.
    if re.fullmatch(r"\d{1,3},\d{3}", crudo):
        crudo = crudo.replace(",", "")
    try:
        valor = float(crudo.replace(",", "."))
    except ValueError:
        return None
    return valor if 0 < valor <= MAX_TONELADAS else None


def _plano(texto: str) -> str:
    sin = unicodedata.normalize("NFKD", (texto or "").lower())
    sin = "".join(c for c in sin if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", sin).split())


def pesos(valor: float | None) -> str:
    return "sin precio" if valor is None else f"${valor:,.2f}"


def toneladas_legibles(valor: float) -> str:
    return f"{valor:,.0f} t" if float(valor).is_integer() else f"{valor:,.2f} t"


class CotizadorGuiado:
    """La cotización paso a paso de un teléfono, sobre el bus.

    El estado (`bus:ventas:cotizador:{tel}`) guarda lo que ya se contestó; el
    paso siguiente se CALCULA (`siguiente`), no se guarda. Así un toque que
    llega fuera de orden —un botón de un mensaje de hace tres pasos— llena su
    dato y el flujo sigue desde donde falte, en vez de quedarse atorado.
    """

    def __init__(self, ventas: VentasAgent, bus: EventBus) -> None:
        self._ventas = ventas
        self._bus = bus

    # --- Estado ------------------------------------------------------------ #
    @staticmethod
    def _clave(telefono: str) -> str:
        return f"bus:ventas:cotizador:{telefono}"

    async def estado(self, telefono: str) -> dict:
        return await self._bus.read(self._clave(telefono)) or {}

    async def _guardar(self, telefono: str, estado: dict) -> None:
        await self._bus.publish(self._clave(telefono), estado, ttl=ESTADO_TTL_SECONDS)

    async def olvidar(self, telefono: str) -> None:
        await self._bus.publish(self._clave(telefono), {}, ttl=1)

    async def registrar_cotizada(
        self, telefono: str, producto: str, toneladas: float, folio: str
    ) -> None:
        """La cotización salió (por el flujo o porque la generó el modelo).

        Queda en paso "cotizada" para que lo siguiente que conteste Ventas lleve
        [🤝 Cerrar pedido] [🔁 Otra cotización] y no los botones de un paso que
        ya se contestó.
        """
        actual = await self.estado(telefono)
        await self._guardar(
            telefono,
            {
                **actual,
                "empezada": True,
                "producto": producto,
                "toneladas": toneladas,
                "folio": folio,
            },
        )

    # --- Entrada ----------------------------------------------------------- #
    async def atender(
        self, telefono: str, entrada: str, texto_libre: bool = True
    ) -> Reply | None:
        """Atiende el mensaje si es de la cotización guiada; None si no.

        Los toques (`cli_cotizar`, `cot_*`) se atienden siempre. El texto libre
        solo si hay una cotización en curso Y `texto_libre` —el router lo pasa
        en True solo cuando Ventas es el agente activo—: el RFC que alguien
        teclea en Soporte no puede terminar como el nombre de una cotización
        que dejó a medias hace tres horas.
        """
        entrada = (entrada or "").strip()
        if not entrada:
            return None
        estado = await self.estado(telefono)

        if entrada in (COTIZAR, COT_OTRA_COTIZACION):
            return await self._empezar(telefono, entrada)
        if entrada.startswith(PREFIJO_PRODUCTO):
            return await self._elegir_producto(
                telefono, estado, sku=entrada[len(PREFIJO_PRODUCTO) :], dijo=entrada
            )
        if entrada == COT_CERRAR:
            # Cerrar es con un asesor: lo lleva el router a Soporte.
            return None

        if not estado.get("sku"):
            # Ya tocó "Cotizar" y escribe el grano en vez de elegirlo de la lista.
            if estado.get("empezada") and texto_libre and not _es_id(entrada):
                return await self._texto(telefono, estado, entrada)
            # Un toque de paso sin cotización en curso (caducó, o el botón lo
            # puso el modelo en la plática libre): que lo conteste Ventas.
            return None

        toque = await self._toque(telefono, estado, entrada)
        if toque is not None:
            return toque
        if _es_id(entrada) or not texto_libre:
            return None
        return await self._texto(telefono, estado, entrada)

    # --- Lo que el agente de Ventas le pide al cotizador ------------------- #
    async def lista_de_granos(self, texto: str) -> Reply:
        """La pregunta "¿qué grano?" con la lista del catálogo, para `[[botones:producto]]`."""
        catalogo, problema = await self._catalogo()
        if problema is not None:
            return problema
        return Reply(texto=texto, lista=self._menu_granos(catalogo))

    async def botones_para(self, telefono: str) -> Reply | None:
        """Los botones del paso pendiente, para colgárselos a una respuesta del
        modelo; None si no hay cotización en curso."""
        estado = await self.estado(telefono)
        if not estado.get("sku") and not estado.get("empezada"):
            return None
        paso = siguiente(estado)
        return await self._controles(paso, estado)

    # --- Internos: empezar y elegir ---------------------------------------- #
    async def _empezar(self, telefono: str, dijo: str) -> Reply:
        estado: dict = {"empezada": True}
        # Si ya se identificó en Soporte, la cotización va a su nombre: no se le
        # pregunta lo que ya dijo.
        sesion = await SesionClienteStore(self._bus).leer(telefono)
        if sesion is not None and sesion.cliente:
            estado["nombre"] = sesion.cliente
        await self._guardar(telefono, estado)
        catalogo, problema = await self._catalogo()
        if problema is not None:
            return await self._responder(telefono, dijo, problema)
        reply = Reply(
            texto=(
                "Con gusto le preparo su cotización. 🌾\n"
                "¿Qué grano necesita? Toque «Ver granos» para elegirlo."
            ),
            lista=self._menu_granos(catalogo),
        )
        return await self._responder(telefono, dijo, reply)

    async def _elegir_producto(
        self, telefono: str, estado: dict, sku: str, dijo: str
    ) -> Reply:
        catalogo, problema = await self._catalogo()
        if problema is not None:
            return await self._responder(telefono, dijo, problema)
        producto = next((p for p in catalogo.productos if p.sku == sku), None)
        if producto is None:
            reply = Reply(
                texto="Ese grano ya no está en la lista. ¿Cuál de estos le cotizo?",
                lista=self._menu_granos(catalogo),
            )
            return await self._responder(telefono, dijo, reply)
        return await self._fijar_producto(telefono, estado, producto, dijo)

    async def _fijar_producto(
        self, telefono: str, estado: dict, producto: ProductoCRM, dijo: str
    ) -> Reply:
        if producto.precio_unitario is None:
            # Sí lo manejamos, no tiene precio: no es "no lo vendemos".
            await self._guardar(telefono, {"empezada": True, "nombre": estado.get("nombre")})
            reply = Reply(
                texto=(
                    f"Sí manejamos *{producto.nombre}*, pero ahorita no tengo su precio "
                    "cargado. ¿Le paso con un asesor para cotizárselo?"
                ),
                botones=[BOTON_ASESOR, Boton(COTIZAR, "🌾 Otro grano")],
            )
            return await self._responder(telefono, f"Quiero {producto.nombre}.", reply)

        cambio_de_grano = estado.get("sku") not in (None, producto.sku)
        nuevo = {
            **estado,
            "empezada": True,
            "sku": producto.sku,
            "producto": producto.nombre,
            "folio": None,
        }
        if cambio_de_grano:
            # Otro grano, otras reglas: lo que se contestó del anterior (volumen,
            # costal) no aplica. Dónde lo recibe y a nombre de quién, sí.
            for campo in ("volumen", "toneladas", "presentacion", "tipo_costal"):
                nuevo.pop(campo, None)
        await self._guardar(telefono, nuevo)
        return await self._preguntar(telefono, nuevo, dijo=f"Quiero {producto.nombre}.")

    # --- Internos: los toques ---------------------------------------------- #
    async def _toque(self, telefono: str, estado: dict, boton: str) -> Reply | None:
        nuevo = dict(estado)
        dijo: str
        reglas = reglas_de(estado.get("producto", ""))

        if boton == COT_UNIDAD_COMPLETA:
            nuevo.update(volumen="completa", toneladas=UNIDAD_COMPLETA_TON)
            dijo = "Una unidad completa, de 40 toneladas."
        elif boton == COT_ENTREGA_CHICA:
            nuevo.update(volumen="chica", toneladas=None)
            nuevo.pop("lugar", None)
            dijo = "Una entrega chica, de hasta 6 toneladas."
        elif boton == COT_OTRA_CANTIDAD:
            nuevo.update(volumen="otra", toneladas=None)
            dijo = "Otra cantidad."
        elif boton in PRESENTACIONES:
            nuevo["presentacion"] = PRESENTACIONES[boton]
            dijo = f"En {PRESENTACIONES[boton]}."
        elif boton in COSTALES:
            nuevo["tipo_costal"] = COSTALES[boton]
            dijo = f"Costal {COSTAL_LEGIBLE[COSTALES[boton]]}."
        elif boton in ZONAS_ENTREGA_CHICA:
            nuevo.update(lugar=ZONAS_ENTREGA_CHICA[boton], cp=None)
            dijo = f"Lo recibo en {ZONAS_ENTREGA_CHICA[boton]}."
        elif boton == COT_ZONA_OTRA:
            nuevo.update(volumen=None, toneladas=None)
            await self._guardar(telefono, nuevo)
            reply = Reply(
                texto=(
                    "Las entregas de hasta 6 t solo llegan a Querétaro, Irapuato, Celaya y "
                    "León. Para otra ciudad hay dos caminos: una unidad completa de 40 t, o "
                    "que un asesor lo coordine con usted. ¿Qué prefiere?"
                ),
                botones=[BOTON_UNIDAD_COMPLETA, BOTON_ASESOR],
            )
            return await self._responder(telefono, "Lo necesito en otra ciudad.", reply)
        elif boton == COT_GENERAR:
            return await self._generar(telefono, estado)
        elif boton == COT_CAMBIAR:
            reply = Reply(
                texto="Claro. ¿Qué quiere cambiar?",
                lista=MenuLista(
                    boton="Elegir",
                    seccion="Cambiar",
                    opciones=[
                        o
                        for o in CAMBIOS_COTIZACION
                        if o.id != COT_CAMBIAR_PRESENTACION or reglas.costal
                    ],
                ),
            )
            return await self._responder(telefono, "Quiero cambiar algo.", reply)
        elif boton == COT_CAMBIAR_GRANO:
            catalogo, problema = await self._catalogo()
            if problema is not None:
                return await self._responder(telefono, "Quiero cambiar el grano.", problema)
            reply = Reply(texto="¿Qué grano le cotizo?", lista=self._menu_granos(catalogo))
            return await self._responder(telefono, "Quiero cambiar el grano.", reply)
        elif boton == COT_CAMBIAR_TONELADAS:
            for campo in ("volumen", "toneladas"):
                nuevo.pop(campo, None)
            dijo = "Quiero cambiar las toneladas."
        elif boton == COT_CAMBIAR_PRESENTACION:
            for campo in ("presentacion", "tipo_costal"):
                nuevo.pop(campo, None)
            dijo = "Quiero cambiar la presentación."
        elif boton == COT_CAMBIAR_ENTREGA:
            for campo in ("lugar", "cp"):
                nuevo.pop(campo, None)
            dijo = "Quiero cambiar dónde lo recibo."
        elif boton == COT_CAMBIAR_NOMBRE:
            nuevo.pop("nombre", None)
            dijo = "Quiero cambiar el nombre."
        else:
            return None

        nuevo["folio"] = None
        await self._guardar(telefono, nuevo)
        return await self._preguntar(telefono, nuevo, dijo=dijo)

    # --- Internos: lo que se escribe --------------------------------------- #
    async def _texto(self, telefono: str, estado: dict, texto: str) -> Reply | None:
        """Lo escrito, si contesta el paso pendiente; None si es otra cosa."""
        paso = siguiente(estado)
        if es_pregunta(texto) or paso in ("confirmar", "cotizada"):
            return None
        nuevo = dict(estado)

        if paso == "grano":
            catalogo, _ = await self._catalogo()
            producto = buscar_producto(catalogo, texto) if catalogo else None
            if producto is None:
                return None
            return await self._fijar_producto(telefono, estado, producto, texto)

        if paso in ("volumen", "toneladas"):
            toneladas = leer_toneladas(texto)
            if toneladas is None:
                return None
            fuera = self._aplicar_toneladas(nuevo, toneladas)
            if fuera is not None:
                await self._guardar(telefono, nuevo)
                return await self._responder(telefono, texto, fuera)
        elif paso == "presentacion":
            plano = _plano(texto)
            if "25" in plano:
                nuevo["presentacion"] = PRESENTACIONES[COT_COSTAL_25]
            elif "50" in plano:
                nuevo["presentacion"] = PRESENTACIONES[COT_COSTAL_50]
            else:
                return None
        elif paso == "costal":
            plano = _plano(texto)
            if "sin marca" in plano:
                nuevo["tipo_costal"] = "sin_marca"
            elif "transparente" in plano:
                nuevo["tipo_costal"] = "transparente"
            elif "marca" in plano:
                nuevo["tipo_costal"] = "con_marca"
            else:
                return None
        elif paso == "zona":
            plano = _plano(texto)
            zona = next(
                (z for z in ZONAS_ENTREGA_CHICA.values() if _plano(z) in plano), None
            )
            if zona is None:
                return None
            nuevo.update(lugar=zona, cp=None)
        elif paso == "entrega":
            lugar, cp = _lugar_de(texto)
            if lugar is None and cp is None:
                return None
            nuevo.update(lugar=lugar, cp=cp)
        elif paso == "nombre":
            nombre = _nombre_de(texto)
            if nombre is None:
                return None
            nuevo["nombre"] = nombre
        else:
            return None

        nuevo["folio"] = None
        await self._guardar(telefono, nuevo)
        return await self._preguntar(telefono, nuevo, dijo=texto)

    def _aplicar_toneladas(self, estado: dict, toneladas: float) -> Reply | None:
        """Acomoda las toneladas según las reglas; un `Reply` si se salen de ellas."""
        reglas = reglas_de(estado.get("producto", ""))
        if not reglas.volumen_del_guion:
            if toneladas > UNIDAD_COMPLETA_TON:
                estado["toneladas"] = None
                return _con_asesor(
                    f"Más de {UNIDAD_COMPLETA_TON:.0f} t se coordina en varias unidades con un "
                    "asesor, para que le cuadren fechas y fletes."
                )
            estado["toneladas"] = toneladas
            return None

        volumen = estado.get("volumen")
        if volumen == "chica" and toneladas > ENTREGA_CHICA_MAX_TON:
            estado["toneladas"] = None
            return Reply(
                texto=(
                    f"Las entregas chicas son de hasta {ENTREGA_CHICA_MAX_TON:.0f} t. "
                    "¿Cuántas toneladas, de 1 a 6?"
                ),
                botones=[BOTON_UNIDAD_COMPLETA, BOTON_ASESOR],
            )
        if toneladas <= ENTREGA_CHICA_MAX_TON:
            estado.update(volumen="chica", toneladas=toneladas)
            return None
        if toneladas == UNIDAD_COMPLETA_TON:
            estado.update(volumen="completa", toneladas=toneladas)
            return None
        estado.update(volumen=None, toneladas=None)
        if toneladas > UNIDAD_COMPLETA_TON:
            return _con_asesor(
                "Más de 40 t se coordina en varias unidades con un asesor, para que le "
                "cuadren fechas y fletes."
            )
        return _con_asesor(
            "Entre 6 y 40 t no es entrega local ni unidad completa: eso se coordina caso "
            "por caso con un asesor."
        )

    # --- Internos: preguntar, generar -------------------------------------- #
    async def _preguntar(self, telefono: str, estado: dict, dijo: str) -> Reply:
        paso = siguiente(estado)
        controles = await self._controles(paso, estado)
        return await self._responder(telefono, dijo, controles)

    async def _controles(self, paso: str, estado: dict) -> Reply:
        """La pregunta del paso, con sus botones (o su lista, o su ubicación)."""
        producto = estado.get("producto", "")
        if paso == "grano":
            catalogo, problema = await self._catalogo()
            if problema is not None:
                return problema
            return Reply(texto="¿Qué grano le cotizo?", lista=self._menu_granos(catalogo))
        if paso == "volumen":
            return Reply(
                texto=f"¿Cuántas toneladas de *{producto}* necesita?",
                botones=list(BOTONES_COTIZACION["volumen"]),
            )
        if paso == "toneladas":
            if estado.get("volumen") == "chica":
                pregunta = "¿Cuántas toneladas, de 1 a 6? Escríbame el número."
            else:
                pregunta = (
                    f"¿Cuántas toneladas de *{producto}* necesita? Escríbame el número "
                    "(por ejemplo, 30)."
                )
            return Reply(texto=pregunta, botones=[BOTON_ASESOR])
        if paso == "presentacion":
            return Reply(
                texto="¿Lo quiere en costal de 25 o de 50 kg?",
                botones=list(BOTONES_COTIZACION["presentacion"]),
            )
        if paso == "costal":
            return Reply(
                texto="¿El costal con nuestra marca, sin marca o transparente? Es el mismo precio.",
                botones=list(BOTONES_COTIZACION["costal"]),
            )
        if paso == "zona":
            return Reply(
                texto=(
                    "Las entregas de hasta 6 t llegan a Querétaro, Irapuato, Celaya y León. "
                    "¿En cuál lo recibe?"
                ),
                lista=MenuLista(
                    boton="Elegir ciudad",
                    seccion="Entrega local",
                    opciones=[
                        *(OpcionLista(i, c) for i, c in ZONAS_ENTREGA_CHICA.items()),
                        OpcionLista(COT_ZONA_OTRA, "Otra ciudad", "Se coordina con un asesor"),
                    ],
                ),
            )
        if paso == "entrega":
            return Reply(
                texto=(
                    "¿Dónde lo recibe? Toque «Enviar ubicación» o escríbame su código postal o "
                    "su ciudad. Con eso se le cotiza el flete, que va aparte."
                ),
                pedir_ubicacion=True,
            )
        if paso == "nombre":
            return Reply(
                texto=(
                    "¿A nombre de quién hago la cotización? Escríbame su nombre o el de su "
                    "empresa."
                ),
                botones=[BOTON_ASESOR],
            )
        if paso == "cotizada":
            # Tras la cotización, lo que conteste el modelo lleva "Asesor" y no
            # "Menú": es la salida del guion para todo lo que el bot no autoriza.
            return Reply(
                texto="¿Cerramos el pedido?",
                botones=[BOTON_CERRAR, BOTON_OTRA, BOTON_ASESOR],
            )
        return await self._resumen(estado)

    async def _resumen(self, estado: dict) -> Reply:
        """El resumen antes de generar: lo que se va a imprimir en el PDF."""
        catalogo, problema = await self._catalogo()
        if problema is not None:
            return problema
        producto = buscar_producto(catalogo, estado.get("producto", ""))
        precio = producto.precio_unitario if producto else None
        renglones = [
            "Así queda su cotización:",
            f"• {estado.get('producto')}: {toneladas_legibles(estado['toneladas'])}",
        ]
        if estado.get("presentacion"):
            costal = COSTAL_LEGIBLE.get(estado.get("tipo_costal") or "", "")
            renglones.append(f"• {estado['presentacion']}{f', {costal}' if costal else ''}")
        renglones.append(f"• Entrega en: {_entrega(estado)}")
        renglones.append(f"• A nombre de: {estado.get('nombre')}")
        if precio is not None:
            renglones.append(
                f"• Precio LAB: {pesos(precio)} por tonelada (el grano cargado en nuestra "
                "sucursal; el flete va aparte y se paga en destino)"
            )
        renglones.append("\n¿La genero y se la envío en PDF?")
        return Reply(
            texto="\n".join(renglones), botones=[BOTON_GENERAR, BOTON_CAMBIAR, BOTON_ASESOR]
        )

    async def _generar(self, telefono: str, estado: dict) -> Reply:
        if siguiente(estado) != "confirmar":
            return await self._preguntar(telefono, estado, dijo="Sí, genere mi cotización.")

        tool_input = {
            "producto": estado["producto"],
            "cantidad_ton": estado["toneladas"],
            "nombre_cliente": estado["nombre"],
        }
        if estado.get("presentacion"):
            tool_input["presentacion"] = estado["presentacion"]
        if estado.get("tipo_costal"):
            tool_input["tipo_costal"] = estado["tipo_costal"]
        if estado.get("lugar"):
            tool_input["lugar_de_entrega"] = estado["lugar"]
        if estado.get("cp"):
            tool_input["codigo_postal"] = estado["cp"]

        resultado = await self._ventas.cotizar(tool_input, telefono)
        dijo = "Sí, genere mi cotización."
        if not resultado.get("disponible"):
            reply = _sin_precio(resultado.get("motivo"), estado.get("producto", ""))
            return await self._responder(telefono, dijo, reply)

        folio = resultado.get("folio", "")
        toneladas = toneladas_legibles(float(resultado.get("cantidad_ton") or estado["toneladas"]))
        if resultado.get("pdf_enviado"):
            texto = (
                f"📄 Le acabo de enviar su cotización *{folio}*: {resultado.get('producto')}, "
                f"{toneladas} a {pesos(resultado.get('precio_ton'))} por tonelada, precio LAB. "
                "El flete no está incluido: se cotiza aparte según su ubicación y se paga "
                "en destino.\n\n¿Cerramos el pedido?"
            )
        elif resultado.get("motivo_pdf") == "requiere_revision_de_vendedor":
            texto = (
                f"Registré su cotización *{folio}*. Un asesor la revisa y se la hace llegar "
                "formalmente. ¿Le ayudo en algo más?"
            )
        else:
            texto = (
                f"Su cotización quedó registrada con el folio *{folio}*, pero no pude "
                "enviarle el archivo. Un asesor se lo hace llegar. ¿Le ayudo en algo más?"
            )
        reply = Reply(texto=texto, botones=[BOTON_CERRAR, BOTON_OTRA, BOTON_MENU])
        return await self._responder(telefono, dijo, reply)

    # --- Internos: catálogo, historial ------------------------------------- #
    async def _catalogo(self) -> tuple[CatalogoCRM | None, Reply | None]:
        """El catálogo, o la respuesta que explica por qué no se puede cotizar."""
        try:
            catalogo = await self._ventas.catalogo()
        except CRMNoDisponible as exc:
            logger.warning("CRM no disponible en la cotización guiada: %s", exc)
            return None, _sin_precio("crm_no_disponible", "")
        if catalogo.desactualizado:
            return None, _sin_precio("datos_no_confiables", "")
        if not catalogo.productos:
            return None, _sin_precio("crm_no_disponible", "")
        return catalogo, None

    @staticmethod
    def _menu_granos(catalogo: CatalogoCRM) -> MenuLista:
        """La lista de granos, del catálogo. Sin precio a propósito: el guion
        pide calificar antes de dar precio, y el precio va en el resumen."""
        opciones = []
        for p in catalogo.productos[:10]:
            detalle = DISPONIBILIDAD.get(p.disponibilidad, "")
            if p.precio_unitario is None:
                detalle = f"{detalle} · precio con asesor" if detalle else "Precio con asesor"
            opciones.append(OpcionLista(f"{PREFIJO_PRODUCTO}{p.sku}", p.nombre, detalle))
        return MenuLista(boton="Ver granos", seccion="Granos", opciones=opciones)

    async def _responder(self, telefono: str, dijo: str, reply: Reply) -> Reply:
        """Deja el intercambio en el historial de Ventas y devuelve la respuesta.

        Sin esto, si a media cotización el cliente pregunta algo, el modelo no
        sabría qué grano eligió ni cuántas toneladas pidió, y volvería a
        preguntarlo.
        """
        try:
            await self._ventas.anotar_turno(telefono, dijo, reply.texto)
        except Exception:  # noqa: BLE001 - sin historial la cotización sigue
            logger.exception("No se pudo anotar el paso de la cotización de %s", telefono)
        return reply


def siguiente(estado: dict) -> str:
    """El paso que falta, en el orden del guion."""
    if estado.get("folio"):
        return "cotizada"
    if not estado.get("sku"):
        return "grano"
    reglas = reglas_de(estado.get("producto", ""))
    if reglas.volumen_del_guion:
        if not estado.get("volumen"):
            return "volumen"
        if estado.get("toneladas") is None:
            return "toneladas"
    elif estado.get("toneladas") is None:
        return "toneladas"
    if reglas.costal:
        if not estado.get("presentacion"):
            return "presentacion"
        if not estado.get("tipo_costal"):
            return "costal"
    if not estado.get("lugar") and not estado.get("cp"):
        chica = reglas.volumen_del_guion and estado.get("volumen") == "chica"
        return "zona" if chica else "entrega"
    if not estado.get("nombre"):
        return "nombre"
    return "confirmar"


def _es_id(entrada: str) -> bool:
    """¿Es un comando o el id de un botón (de este flujo o de otro menú)?"""
    return entrada.startswith(("cli_", "cot_", "prov_", "ps_", "/"))


def _entrega(estado: dict) -> str:
    cp = f"C.P. {estado['cp']}" if estado.get("cp") else None
    partes = [p for p in (estado.get("lugar"), cp) if p]
    return ", ".join(partes) or "por confirmar"


def _lugar_de(texto: str) -> tuple[str | None, str | None]:
    """(lugar, código postal) de lo que escribió o compartió."""
    limpio = " ".join((texto or "").split())
    if limpio.startswith("📍"):
        # La ubicación de WhatsApp, ya vuelta texto por `main.py`: se guarda
        # completa, con coordenadas y liga, para que el vendedor la abra.
        return limpio.replace("📍 Esta es mi ubicación para la entrega:", "").strip(), None
    cp = _CP.search(limpio)
    sin_cp = _CP.sub("", limpio).strip(" ,.-")
    if _plano(sin_cp) in _NO_ES_RESPUESTA:
        sin_cp = ""
    lugar = sin_cp if len(re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", sin_cp)) >= 3 else None
    if lugar and len(lugar) > 120:
        return None, cp.group(1) if cp else None
    return lugar, (cp.group(1) if cp else None)


def _nombre_de(texto: str) -> str | None:
    limpio = " ".join((texto or "").split()).strip(" .,")
    if _plano(limpio) in _NO_ES_RESPUESTA or len(limpio) < 2 or len(limpio) > 80:
        return None
    if not re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", limpio):
        return None
    return limpio


def _con_asesor(texto: str) -> Reply:
    return Reply(
        texto=f"{texto} ¿Le paso con un asesor?",
        botones=[BOTON_ASESOR, BOTON_OTRA_CANTIDAD, BOTON_MENU],
    )


def _sin_precio(motivo: str | None, producto: str) -> Reply:
    """Por qué no se puede cotizar, con la causa real y ninguna otra."""
    if motivo == "sin_precio_publicado":
        texto = (
            f"Sí manejamos {producto}, pero ahorita no tengo su precio cargado. "
            "¿Le paso con un asesor para cotizárselo?"
        )
    elif motivo == "no_esta_en_catalogo":
        texto = f"{producto} no lo manejamos. ¿Le cotizo otro grano?"
    elif motivo == "datos_no_confiables":
        texto = (
            "Disculpe: los precios que tengo en este momento pueden estar desactualizados "
            "y no quiero darle una cifra equivocada. ¿Le paso con un asesor?"
        )
    else:
        texto = (
            "En este momento no puedo consultar los precios. ¿Le paso con un asesor para "
            "que le cotice?"
        )
    return Reply(texto=texto, botones=[BOTON_ASESOR, BOTON_MENU])
