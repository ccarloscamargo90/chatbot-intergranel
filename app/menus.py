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
"""

from __future__ import annotations

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


def accion(boton_id: str) -> Accion | None:
    """La acción de un id de botón, o None si el id no es de un menú nuestro."""
    return ACCIONES.get((boton_id or "").strip())


# --- Menú principal --------------------------------------------------------- #
_OPCIONES_IDENTIFICADO = [
    OpcionLista(PEDIDOS, "📦 Mis pedidos", "Estado y fecha de entrega"),
    OpcionLista(CONTRATOS, "📄 Mis contratos", "Contratos y avance de entregas"),
    OpcionLista(SALDO, "💰 Mi saldo", "Lo que debo y lo que está vencido"),
    OpcionLista(FACTURAS, "🧾 Mis facturas", "Folios, montos y estado de cobro"),
    OpcionLista(COTIZACIONES, "🧮 Mis cotizaciones", "Precios y vigencia"),
    OpcionLista(ESTADO_CUENTA, "📄 Estado de cuenta", "Se lo mando en PDF"),
    OpcionLista(PRECIOS, "🌾 Precios del día", "Precios vigentes por tonelada"),
    OpcionLista(ASESOR, "👤 Hablar con asesor", "Le pasamos con una persona"),
    OpcionLista(CERRAR_SESION, "🔒 Cerrar sesión", "Deja de mostrar mi información"),
]

_OPCIONES_ANONIMO = [
    OpcionLista(IDENTIFICARME, "🔑 Identificarme", "Con su RFC y el nombre de su empresa"),
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
# natural es que quiera otra consulta o una persona. Máximo 3 (límite de Meta).
BOTONES_SEGUIMIENTO = [
    Boton(MENU, "📋 Menú"),
    Boton(ASESOR, "👤 Asesor"),
]

# Cuando aún no sabemos quién escribe.
BOTONES_ANONIMO = [
    Boton(IDENTIFICARME, "🔑 Identificarme"),
    Boton(PRECIOS, "🌾 Precios"),
    Boton(ASESOR, "👤 Asesor"),
]


def texto_menu(identificado: bool, cliente: str = "") -> str:
    """Cuerpo del mensaje que acompaña al menú."""
    if identificado:
        saludo = f"Listo, {cliente}. " if cliente else ""
        return f"{saludo}¿Qué desea consultar?"
    return (
        "Para consultar sus pedidos, contratos, facturas o saldo necesito "
        "identificarlo primero. ¿Qué desea hacer?"
    )


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
