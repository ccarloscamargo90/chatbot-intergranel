"""Menús y botones del autoservicio del cliente.

Por qué botones y no solo texto: pedirle a un cliente que escriba "quiero ver
mi estado de cuenta" es pedirle que adivine cómo se dice. Un botón le enseña
qué puede pedir y, de regreso, nos entrega una intención EXACTA — un id — en
lugar de una frase que hay que clasificar y que se puede clasificar mal.

Cómo se cierra el círculo: cada opción tiene un id (`cli_*`) y una frase
canónica. Cuando el cliente toca un botón, `main.py` recoge el id y el router lo
trata como un comando explícito: manda el turno al agente que le toca con la
frase canónica como mensaje. Así el agente no necesita saber que hubo un botón —
recibe "quiero ver mi saldo" y hace lo mismo que si lo hubieran escrito.

Nada de aquí nombra una empresa: los textos son de la relación cliente-proveedor,
no de una marca. El nombre que se muestre viene de `company_name`.

**Vender también es un botón.** El número no solo atiende a quien ya compra:
quien escribe desde la página casi siempre viene a comprar. Por eso "🧮 Cotizar"
está en los dos menús, en los botones de seguimiento y en la bienvenida, y las
preguntas del guion de ventas que tienen respuesta cerrada (volumen,
presentación, tipo de costal, ubicación) se contestan con un toque.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .replies import Boton, MenuLista, OpcionLista

# --- Ids de las acciones ---------------------------------------------------- #
# El prefijo `cli_` marca las del autoservicio del cliente y evita chocar con
# ids que otro flujo agregue después.
MENU = "cli_menu"
PEDIDOS = "cli_pedidos"
CONTRATOS = "cli_contratos"
SALDO = "cli_saldo"
FACTURAS = "cli_facturas"
COTIZACIONES = "cli_cotizaciones"
PRECIOS = "cli_precios"
ASESOR = "cli_asesor"
IDENTIFICARME = "cli_identificarme"
ESTADO_CUENTA = "cli_estado_cuenta"
CERRAR_SESION = "cli_cerrar_sesion"
COTIZAR = "cli_cotizar"

# Las respuestas cerradas del guion de ventas llevan su propio prefijo `cot_`:
# son pasos de UNA cotización, no opciones del menú.
COT_UNIDAD_COMPLETA = "cot_vol_40"
COT_ENTREGA_CHICA = "cot_vol_6"
COT_OTRA_CANTIDAD = "cot_vol_otra"
COT_COSTAL_25 = "cot_pres_25"
COT_COSTAL_50 = "cot_pres_50"
COT_CON_MARCA = "cot_costal_marca"
COT_SIN_MARCA = "cot_costal_sin_marca"
COT_TRANSPARENTE = "cot_costal_transparente"

# Los del flujo guiado de cotización (ver `cotizador.py`). El grano es una fila
# DINÁMICA —`cot_prod:<sku>`, sale del catálogo del CRM— y por eso no está en
# `ACCIONES`: lo atiende siempre el cotizador. Los demás sí tienen frase, para
# que un toque que llega cuando la cotización ya caducó siga llegando a Ventas
# con una intención legible.
PREFIJO_PRODUCTO = "cot_prod:"
COT_ZONA_QRO = "cot_zona_qro"
COT_ZONA_IRAPUATO = "cot_zona_irapuato"
COT_ZONA_CELAYA = "cot_zona_celaya"
COT_ZONA_LEON = "cot_zona_leon"
COT_ZONA_OTRA = "cot_zona_otra"
COT_GENERAR = "cot_generar"
COT_CAMBIAR = "cot_cambiar"
COT_CAMBIAR_GRANO = "cot_cambiar_grano"
COT_CAMBIAR_TONELADAS = "cot_cambiar_ton"
COT_CAMBIAR_PRESENTACION = "cot_cambiar_pres"
COT_CAMBIAR_ENTREGA = "cot_cambiar_entrega"
COT_CAMBIAR_NOMBRE = "cot_cambiar_nombre"
COT_OTRA_COTIZACION = "cot_otra"
# Cerrar no lo hace el bot: el guion manda cerrar con un asesor. Por eso este
# toque va a Soporte, que es quien escala a una persona.
COT_CERRAR = "cot_cerrar"

# Los del PROVEEDOR llevan su propio prefijo `prov_`: son otra audiencia, con
# otra sesión, y mezclar los ids haría que un toque abriera lo que no es.
PROV_SOY_PROVEEDOR = "prov_identificarme"
PROV_PAGOS = "prov_pagos"
PROV_FACTURAS = "prov_facturas"
PROV_ORDENES = "prov_ordenes"
PROV_COMPRADOR = "prov_comprador"
PROV_CERRAR_SESION = "prov_cerrar_sesion"


@dataclass(frozen=True)
class Accion:
    """A qué agente va un toque de botón y con qué frase entra."""

    agente: str
    texto: str


# Un toque = un comando. La frase es lo que el agente ve como mensaje del
# cliente, así que está escrita como la escribiría una persona.
ACCIONES: dict[str, Accion] = {
    PEDIDOS: Accion("soporte", "Quiero ver el estado de mis pedidos."),
    CONTRATOS: Accion("soporte", "Quiero ver mis contratos."),
    SALDO: Accion("soporte", "Quiero ver mi saldo y lo que tengo vencido."),
    ESTADO_CUENTA: Accion(
        "soporte", "Mándame mi estado de cuenta en PDF."
    ),
    FACTURAS: Accion("soporte", "Quiero ver mis facturas."),
    COTIZACIONES: Accion("soporte", "Quiero ver mis cotizaciones."),
    IDENTIFICARME: Accion("soporte", "Quiero identificarme para ver mi información."),
    CERRAR_SESION: Accion("soporte", "Quiero cerrar mi sesión."),
    ASESOR: Accion("soporte", "Quiero hablar con un asesor humano."),
    PRECIOS: Accion("ventas", "¿Cuáles son los precios vigentes?"),
    COTIZAR: Accion("ventas", "Quiero cotizar."),
    # --- Pasos de la cotización (guion de ventas) ---
    COT_UNIDAD_COMPLETA: Accion("ventas", "Necesito una unidad completa, de 40 toneladas."),
    COT_ENTREGA_CHICA: Accion("ventas", "Necesito una entrega chica, de hasta 6 toneladas."),
    COT_OTRA_CANTIDAD: Accion("ventas", "Necesito otra cantidad de toneladas."),
    COT_COSTAL_25: Accion("ventas", "Lo quiero en costal de 25 kg."),
    COT_COSTAL_50: Accion("ventas", "Lo quiero en costal de 50 kg."),
    COT_CON_MARCA: Accion("ventas", "Lo quiero en costal con la marca de ustedes."),
    COT_SIN_MARCA: Accion("ventas", "Lo quiero en costal sin marca."),
    COT_TRANSPARENTE: Accion("ventas", "Lo quiero en costal transparente."),
    # --- Flujo guiado de cotización ---
    COT_ZONA_QRO: Accion("ventas", "Lo recibo en Querétaro."),
    COT_ZONA_IRAPUATO: Accion("ventas", "Lo recibo en Irapuato."),
    COT_ZONA_CELAYA: Accion("ventas", "Lo recibo en Celaya."),
    COT_ZONA_LEON: Accion("ventas", "Lo recibo en León."),
    COT_ZONA_OTRA: Accion("ventas", "Lo necesito en otra ciudad."),
    COT_GENERAR: Accion("ventas", "Sí, genere mi cotización."),
    COT_CAMBIAR: Accion("ventas", "Quiero cambiar algo de mi cotización."),
    COT_CAMBIAR_GRANO: Accion("ventas", "Quiero cambiar el grano."),
    COT_CAMBIAR_TONELADAS: Accion("ventas", "Quiero cambiar las toneladas."),
    COT_CAMBIAR_PRESENTACION: Accion("ventas", "Quiero cambiar la presentación."),
    COT_CAMBIAR_ENTREGA: Accion("ventas", "Quiero cambiar dónde lo recibo."),
    COT_CAMBIAR_NOMBRE: Accion("ventas", "Quiero cambiar el nombre de la cotización."),
    COT_OTRA_COTIZACION: Accion("ventas", "Quiero hacer otra cotización."),
    COT_CERRAR: Accion(
        "soporte", "Quiero cerrar el pedido de mi cotización. Páseme con un asesor, por favor."
    ),
    # --- Proveedor ---
    PROV_SOY_PROVEEDOR: Accion(
        "proveedores", "Soy proveedor y quiero consultar mis pagos."
    ),
    PROV_PAGOS: Accion("proveedores", "¿Cuánto me deben y qué está vencido?"),
    PROV_FACTURAS: Accion("proveedores", "Quiero ver mis facturas y su saldo."),
    PROV_ORDENES: Accion("proveedores", "Quiero ver mis órdenes de compra."),
    PROV_COMPRADOR: Accion("proveedores", "Quiero hablar con mi comprador."),
    PROV_CERRAR_SESION: Accion("proveedores", "Quiero cerrar mi sesión."),
}


# --- Botones que salen de los datos del cliente ----------------------------- #
# Tras listar sus facturas, cotizaciones o contratos, cada una es una fila que
# se toca para recibir el documento. El folio viaja en el id y por eso no están
# en `ACCIONES`: la frase se arma con él. No abre nada que no sea suyo: Soporte
# busca ese folio ENTRE LOS DEL CLIENTE identificado (regla 9), así que un id
# fabricado a mano no sirve para pedir el documento de otro.
PREFIJO_DOC_FACTURA = "doc_factura:"
PREFIJO_DOC_COTIZACION = "doc_cotizacion:"
PREFIJO_DOC_CONTRATO = "doc_contrato:"

_FRASE_DOCUMENTO = {
    PREFIJO_DOC_FACTURA: "Envíame la factura {folio} en PDF y XML.",
    PREFIJO_DOC_COTIZACION: "Envíame la cotización {folio} en PDF.",
    PREFIJO_DOC_CONTRATO: "Envíame el contrato {folio} en PDF.",
}

# Un folio es letras, dígitos y guiones. Cualquier otra cosa no entra a la
# frase: el id llega del teléfono del cliente y termina en el prompt.
_FOLIO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-/]{0,39}$")


def accion(boton_id: str) -> Accion | None:
    """La acción de un id de botón, o None si el id no es de un menú nuestro."""
    limpio = (boton_id or "").strip()
    fija = ACCIONES.get(limpio)
    if fija is not None:
        return fija
    for prefijo, frase in _FRASE_DOCUMENTO.items():
        if limpio.startswith(prefijo):
            folio = limpio[len(prefijo) :]
            return Accion("soporte", frase.format(folio=folio)) if _FOLIO.match(folio) else None
    return None


# --- Menú principal --------------------------------------------------------- #
# Son 10 filas: el tope de Meta. Para agregar una, hay que quitar otra.
_OPCIONES_IDENTIFICADO = [
    # Primero la compra: quien ya es cliente también vuelve a comprar.
    OpcionLista(COTIZAR, "🧮 Cotizar", "Le preparo una cotización en PDF"),
    OpcionLista(PEDIDOS, "📦 Mis pedidos", "Estado y fecha de entrega"),
    OpcionLista(CONTRATOS, "📄 Mis contratos", "Contratos y avance de entregas"),
    OpcionLista(SALDO, "💰 Mi saldo", "Lo que debo y lo que está vencido"),
    OpcionLista(FACTURAS, "🧾 Mis facturas", "Folios, montos y estado de cobro"),
    OpcionLista(COTIZACIONES, "📑 Mis cotizaciones", "Precios y vigencia"),
    OpcionLista(ESTADO_CUENTA, "📄 Estado de cuenta", "Se lo mando en PDF"),
    OpcionLista(PRECIOS, "🌾 Precios del día", "Precios vigentes por tonelada"),
    OpcionLista(ASESOR, "👤 Hablar con asesor", "Le pasamos con una persona"),
    OpcionLista(CERRAR_SESION, "🔒 Cerrar sesión", "Deja de mostrar mi información"),
]

_OPCIONES_ANONIMO = [
    OpcionLista(COTIZAR, "🧮 Cotizar", "Le preparo una cotización en PDF"),
    OpcionLista(IDENTIFICARME, "🔑 Ya soy cliente", "Con su RFC y el nombre de su empresa"),
    OpcionLista(PRECIOS, "🌾 Precios del día", "Precios vigentes por tonelada"),
    # Quien nos vende también escribe a este número. Sin esta puerta, un
    # proveedor cae en el menú de clientes y se le pide identificarse contra un
    # padrón donde no está.
    OpcionLista(PROV_SOY_PROVEEDOR, "🚚 Soy proveedor", "Consultar mis pagos"),
    OpcionLista(ASESOR, "👤 Hablar con asesor", "Le pasamos con una persona"),
]


def menu_cliente(identificado: bool) -> MenuLista:
    """El menú principal. Sin identificar solo se ofrece lo que no expone datos."""
    return MenuLista(
        boton="Ver opciones",
        seccion="Consultas" if identificado else "Para empezar",
        opciones=_OPCIONES_IDENTIFICADO if identificado else _OPCIONES_ANONIMO,
    )


# --- Botones de seguimiento -------------------------------------------------- #
# Van pegados a una respuesta ya dada: el cliente acaba de leer algo y lo
# natural es que quiera otra consulta, comprar o una persona. Máximo 3 (límite
# de Meta).
BOTONES_SEGUIMIENTO = [
    Boton(MENU, "📋 Menú"),
    Boton(COTIZAR, "🧮 Cotizar"),
    Boton(ASESOR, "👤 Asesor"),
]

# Los de Ventas cuando la respuesta no trae un paso del guion. "Asesor" es la
# salida que el guion pide para todo lo que el agente no puede autorizar
# (descuentos, crédito, fletes, volúmenes fuera de regla); "Cotizar" no va
# porque ya se está cotizando.
BOTONES_VENTAS = [
    Boton(MENU, "📋 Menú"),
    Boton(ASESOR, "👤 Asesor"),
]

# Cuando aún no sabemos quién escribe: la bienvenida de quien llega desde la
# página. Primero la venta; "Ya soy cliente" abre el autoservicio de su cuenta.
# No hay botón de precios a propósito: el guion de ventas prohíbe empezar por
# el precio, antes de saber cuánto necesita y dónde.
BOTONES_ANONIMO = [
    Boton(COTIZAR, "🧮 Cotizar"),
    Boton(IDENTIFICARME, "🔑 Ya soy cliente"),
    Boton(ASESOR, "👤 Asesor"),
]


def texto_menu(identificado: bool, cliente: str = "") -> str:
    """Cuerpo del mensaje que acompaña al menú."""
    if identificado:
        saludo = f"Listo, {cliente}. " if cliente else ""
        return f"{saludo}¿Qué desea consultar?"
    return (
        "Puedo prepararle una cotización. Si ya es cliente, también puedo "
        "mostrarle sus pedidos, contratos, facturas y saldo, identificándolo "
        "primero. ¿Qué desea hacer?"
    )


def texto_bienvenida(empresa: str) -> str:
    """El primer mensaje a quien escribe desde la página.

    Sigue el paso 1 del guion de ventas —saludar, presentarse, decir qué
    manejamos y terminar con una pregunta— y NO dice precio ni existencia: el
    precio viene después de calificar, y cuánto hay no se dice nunca. Dice
    "manejamos", no "tenemos disponible": la disponibilidad la confirma el
    catálogo cuando se cotiza, no un saludo escrito de antemano.
    """
    return (
        f"Buen día 👋 Gracias por escribir a {empresa}.\n"
        "Manejamos maíz blanco nacional del Bajío, con triple cribado, y otros "
        "granos a granel.\n"
        "¿Le preparo una cotización?"
    )


# --- Los pasos de la cotización, como botones -------------------------------- #
# Las preguntas del guion que tienen respuesta cerrada. El agente de Ventas
# decide CUÁNDO preguntar (lo marca en su respuesta, ver `leer_marca`); aquí
# se decide CÓMO se ven, con los topes de Meta. Son del maíz blanco: la unidad
# de 40 t, las entregas de hasta 6 t y los costales vienen del guion de ese
# grano.
BOTONES_COTIZACION: dict[str, list[Boton]] = {
    "volumen": [
        Boton(COT_UNIDAD_COMPLETA, "🚛 Camión de 40 t"),
        Boton(COT_ENTREGA_CHICA, "📦 Hasta 6 t"),
        Boton(COT_OTRA_CANTIDAD, "✏️ Otra cantidad"),
    ],
    "presentacion": [
        Boton(COT_COSTAL_25, "Costal de 25 kg"),
        Boton(COT_COSTAL_50, "Costal de 50 kg"),
    ],
    "costal": [
        Boton(COT_CON_MARCA, "Con marca"),
        Boton(COT_SIN_MARCA, "Sin marca"),
        Boton(COT_TRANSPARENTE, "Transparente"),
    ],
}

#: El paso que no es un botón sino la pantalla nativa de WhatsApp para
#: compartir la ubicación (ver `Reply.pedir_ubicacion`).
PASO_UBICACION = "ubicacion"

#: El paso cuyas opciones no están escritas aquí: la lista de granos sale del
#: catálogo del CRM en el momento (ver `cotizador.lista_de_granos`). Escribirla
#: a mano es como el bot terminaría ofreciendo un grano que ya no se vende.
PASO_PRODUCTO = "producto"

PASOS_COTIZACION = frozenset({*BOTONES_COTIZACION, PASO_UBICACION, PASO_PRODUCTO})

#: Las ciudades a las que llega una entrega de hasta 6 t de maíz blanco (guion
#: de ventas). El valor es cómo se escribe en la cotización.
ZONAS_ENTREGA_CHICA: dict[str, str] = {
    COT_ZONA_QRO: "Querétaro",
    COT_ZONA_IRAPUATO: "Irapuato",
    COT_ZONA_CELAYA: "Celaya",
    COT_ZONA_LEON: "León",
}

#: Lo que se puede cambiar antes de generar la cotización.
CAMBIOS_COTIZACION = [
    OpcionLista(COT_CAMBIAR_GRANO, "🌾 El grano"),
    OpcionLista(COT_CAMBIAR_TONELADAS, "⚖️ Las toneladas"),
    OpcionLista(COT_CAMBIAR_PRESENTACION, "🛍️ Presentación y costal"),
    OpcionLista(COT_CAMBIAR_ENTREGA, "📍 Dónde lo recibe"),
    OpcionLista(COT_CAMBIAR_NOMBRE, "👤 El nombre"),
]

# `[[botones:presentacion]]`. Tolerante con espacios, mayúsculas y acentos
# porque lo escribe un modelo; y TODA marca se borra del texto, se reconozca o
# no: una etiqueta a medias en el WhatsApp del cliente se ve peor que un
# mensaje sin botones.
_MARCA = re.compile(r"\[\[\s*botones\s*:\s*([^\]]*?)\s*\]\]", re.IGNORECASE)


def _paso(crudo: str) -> str:
    sin_acentos = unicodedata.normalize("NFKD", crudo.lower())
    return "".join(c for c in sin_acentos if c.isalpha())


def leer_marca(texto: str) -> tuple[str, str | None]:
    """Separa la marca de botones del texto: `(texto_limpio, paso | None)`.

    Por qué una marca en el texto y no una herramienta: una herramienta le
    cuesta al cliente una vuelta más al modelo en CADA pregunta del guion, y
    el cliente está esperando con el teléfono en la mano. Si el modelo olvida
    la marca, lo peor que pasa es que la pregunta llega sin botones.
    """
    pasos = [_paso(m) for m in _MARCA.findall(texto)]
    limpio = _MARCA.sub("", texto)
    limpio = "\n".join(renglon.rstrip() for renglon in limpio.strip().splitlines())
    reconocidos = [p for p in pasos if p in PASOS_COTIZACION]
    return limpio, (reconocidos[-1] if reconocidos else None)


# --- Menú del proveedor ------------------------------------------------------ #
_OPCIONES_PROVEEDOR = [
    OpcionLista(PROV_PAGOS, "💰 Mis pagos", "Lo que se me debe y lo vencido"),
    OpcionLista(PROV_FACTURAS, "🧾 Mis facturas", "Folios, saldo y vencimiento"),
    OpcionLista(PROV_ORDENES, "📦 Mis órdenes", "Órdenes de compra colocadas"),
    OpcionLista(PROV_COMPRADOR, "👤 Mi comprador", "Le pasamos con una persona"),
    OpcionLista(PROV_CERRAR_SESION, "🔒 Cerrar sesión", "Deja de mostrar mi información"),
]

_OPCIONES_PROVEEDOR_ANONIMO = [
    OpcionLista(PROV_SOY_PROVEEDOR, "🔑 Identificarme", "Con el RFC de su empresa"),
    OpcionLista(PROV_COMPRADOR, "👤 Mi comprador", "Le pasamos con una persona"),
    OpcionLista(MENU, "↩️ Soy cliente", "Ir al menú de clientes"),
]


def menu_proveedor(identificado: bool) -> MenuLista:
    """El menú del proveedor. Sin identificar no se ofrece ningún dato suyo."""
    return MenuLista(
        boton="Ver opciones",
        seccion="Mi cuenta" if identificado else "Para empezar",
        opciones=_OPCIONES_PROVEEDOR if identificado else _OPCIONES_PROVEEDOR_ANONIMO,
    )


# Pegados a cada respuesta ya dada. Máximo 3 (límite de Meta).
BOTONES_PROVEEDOR = [
    Boton(PROV_PAGOS, "💰 Mis pagos"),
    Boton(PROV_COMPRADOR, "👤 Mi comprador"),
]
