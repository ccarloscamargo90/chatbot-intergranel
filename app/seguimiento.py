"""Los botones que siguen a una consulta: salen de lo que se acaba de mostrar.

Antes, después de CUALQUIER respuesta de Soporte venían los mismos tres botones
([📋 Menú] [🧮 Cotizar] [👤 Asesor]). Si el cliente acababa de ver sus cinco
facturas, pedir una tenía que escribirlo —con el folio bien tecleado—, cuando
el bot ya tenía los folios en la mano.

Ahora lo siguiente a un toque depende de lo que se consultó en ese turno:

| Consultó | Lo siguiente |
|---|---|
| sus facturas | una fila por factura: tocarla le manda el PDF y el XML |
| sus cotizaciones | una fila por cotización (las vigentes primero) |
| sus contratos | una fila por contrato |
| su saldo | [📄 Estado de cuenta] [🧾 Mis facturas] [📋 Menú] |
| sus pedidos | [📄 Mis contratos] [🧾 Mis facturas] [📋 Menú] |

Se decide con el RESULTADO de la herramienta, no con el texto del modelo: el
folio que se ofrece es uno que el ERP acaba de listar como de este cliente.
"""

from __future__ import annotations

import json
import logging

from .agents.base import Herramienta
from .menus import (
    CONTRATOS,
    ESTADO_CUENTA,
    FACTURAS,
    MENU,
    PREFIJO_DOC_CONTRATO,
    PREFIJO_DOC_COTIZACION,
    PREFIJO_DOC_FACTURA,
)
from .replies import MAX_FILAS_LISTA, Boton, MenuLista, OpcionLista, Reply

logger = logging.getLogger(__name__)

BOTON_MENU = Boton(MENU, "📋 Menú")
BOTON_ESTADO_CUENTA = Boton(ESTADO_CUENTA, "📄 Estado de cuenta")
BOTON_FACTURAS = Boton(FACTURAS, "🧾 Mis facturas")
BOTON_CONTRATOS = Boton(CONTRATOS, "📄 Mis contratos")

#: Filas de documentos: una menos que el tope, para dejar "📋 Menú" al final.
MAX_DOCUMENTOS = MAX_FILAS_LISTA - 1


def _pesos(valor: float | None, moneda: str = "MXN") -> str:
    return f"${float(valor or 0):,.2f} {moneda}".strip()


def _leer(h: Herramienta) -> dict:
    try:
        datos = json.loads(h.resultado)
    except (TypeError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def _con_menu(opciones: list[OpcionLista], boton: str, seccion: str) -> MenuLista:
    return MenuLista(
        boton=boton,
        seccion=seccion,
        opciones=[*opciones[:MAX_DOCUMENTOS], OpcionLista(MENU, "📋 Menú", "Otras consultas")],
    )


def _facturas(datos: dict) -> Reply | None:
    facturas = datos.get("facturas") or []
    # Primero lo que se debe: es lo que el cliente viene a revisar.
    facturas = sorted(facturas, key=lambda f: float(f.get("saldo") or 0) <= 0)
    opciones = []
    for f in facturas:
        folio = f.get("id")
        if not folio:
            continue
        saldo = float(f.get("saldo") or 0)
        detalle = (
            f"Saldo {_pesos(saldo, f.get('moneda', 'MXN'))}" if saldo > 0 else "Pagada"
        )
        opciones.append(OpcionLista(f"{PREFIJO_DOC_FACTURA}{folio}", f"🧾 {folio}", detalle))
    if not opciones:
        return None
    return Reply(texto="", lista=_con_menu(opciones, "Enviar factura", "Mis facturas"))


def _cotizaciones(datos: dict) -> Reply | None:
    cotizaciones = sorted(datos.get("cotizaciones") or [], key=lambda c: bool(c.get("vencida")))
    opciones = []
    for c in cotizaciones:
        folio = c.get("id")
        if not folio:
            continue
        estado = "vencida" if c.get("vencida") else "vigente"
        detalle = f"{c.get('producto') or ''} · {estado}".strip(" ·")
        opciones.append(OpcionLista(f"{PREFIJO_DOC_COTIZACION}{folio}", f"📑 {folio}", detalle))
    if not opciones:
        return None
    return Reply(texto="", lista=_con_menu(opciones, "Enviar cotización", "Mis cotizaciones"))


def _contratos(datos: dict) -> Reply | None:
    contratos = datos.get("contratos") or []
    if not contratos and isinstance(datos.get("orden"), dict):
        contratos = [datos["orden"]]
    opciones = []
    for c in contratos:
        folio = c.get("id")
        if not folio:
            continue
        opciones.append(
            OpcionLista(
                f"{PREFIJO_DOC_CONTRATO}{folio}", f"📄 {folio}", str(c.get("estado") or "")
            )
        )
    if not opciones:
        return None
    return Reply(texto="", lista=_con_menu(opciones, "Enviar contrato", "Mis contratos"))


def siguiente_a_la_consulta(herramientas: list[Herramienta] | None) -> Reply | None:
    """Los controles que siguen a lo que se consultó en el turno; None si nada aplica.

    Manda la ÚLTIMA consulta con datos: si en un turno vio su saldo y luego sus
    facturas, lo que sigue es escoger una factura.
    """
    for h in reversed(herramientas or []):
        datos = _leer(h)
        if datos.get("identificado") is False:
            # Sin sesión no hay nada suyo que ofrecer: que decida el agente.
            return None
        if h.nombre == "listar_mis_facturas":
            return _facturas(datos)
        if h.nombre == "listar_mis_cotizaciones":
            return _cotizaciones(datos)
        if h.nombre in ("listar_mis_contratos", "consultar_orden"):
            return _contratos(datos)
        if h.nombre in ("consultar_mi_saldo", "resumen_de_mi_cuenta"):
            return Reply(texto="", botones=[BOTON_ESTADO_CUENTA, BOTON_FACTURAS, BOTON_MENU])
        if h.nombre == "listar_mis_pedidos":
            return Reply(texto="", botones=[BOTON_CONTRATOS, BOTON_FACTURAS, BOTON_MENU])
    return None
