"""El PDF de la cotización: lo único que el cliente se queda en la mano.

El chat se va hacia arriba y se pierde entre otras conversaciones; el archivo
se queda en el teléfono, se reenvía al jefe de compras y se imprime. Por eso
el PDF lleva TODO lo que hace falta para decidir —folio, a nombre de quién,
grano, toneladas, precio por tonelada, importe— y nada que no venga del CRM.

Se arma con `fpdf2`, que es Python puro: no hace falta un navegador ni una
librería del sistema en el contenedor de Railway.

**Dos cifras que este módulo nunca inventa.** El IVA y la vigencia se
imprimen solo si alguien los configuró (`COTIZACION_IVA_TASA`,
`COTIZACION_VIGENCIA_DIAS`). Sin configurar, no aparecen: un "vigente 5 días"
o un "+16% IVA" puestos por omisión serían una condición comercial que nadie
autorizó, escrita en un documento que el cliente va a tratar como una oferta.
Y cuando el IVA sí está configurado, la misma tasa viaja al CRM, para que el
total del tablero sea el mismo número que el del PDF.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from fpdf import FPDF
from fpdf.enums import XPos, YPos

#: Los meses en español, sin depender del `locale` del contenedor (que en
#: Railway es el de fábrica y daría "September").
MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

#: Lo que las comillas y los guiones de imprenta tienen que volverse para que
#: quepan en latin-1, que es lo que codifican las fuentes base de PDF. Sin
#: esto, un texto con "—" tumba la generación del archivo entero.
REEMPLAZOS = {
    "—": "-",
    "–": "-",
    "‑": "-",
    "“": '"',
    "”": '"',
    "„": '"',
    "‘": "'",
    "’": "'",
    "…": "...",
    "•": "-",
    " ": " ",
    " ": " ",
    " ": " ",
}


@dataclass(frozen=True)
class DatosCotizacion:
    """Lo que se imprime. Vive aquí y no en `models.py` a propósito: no cruza
    ninguna red ni se guarda en ningún lado, es la entrada de una función."""

    folio: str
    cliente: str
    producto: str
    cantidad_ton: float
    precio_ton: float
    empresa: str
    moneda: str = "MXN"
    telefono: str = ""
    #: Cuándo el CRM copió este precio del ERP (ISO). Va impreso: un precio sin
    #: fecha invita a confiar en él igual esté fresco o lleve tres días parado.
    precio_actualizado_el: str | None = None
    emitida: date | None = None
    vigencia_hasta: date | None = None
    #: Fracción: 0.16 = 16 %. En 0 el PDF no menciona impuestos.
    tasa_iva: float = 0.0

    @property
    def subtotal(self) -> float:
        return round(self.cantidad_ton * self.precio_ton, 2)

    @property
    def iva(self) -> float:
        return round(self.subtotal * self.tasa_iva, 2)

    @property
    def total(self) -> float:
        return round(self.subtotal + self.iva, 2)


def sanear(texto: str) -> str:
    """El texto como lo puede escribir una fuente base de PDF.

    Los acentos y la ñ sí caben en latin-1 y se conservan tal cual —"Maíz" se
    imprime "Maíz"—; lo que no cabe se sustituye por su equivalente de
    máquina de escribir, y lo que no tiene equivalente por "?", que es
    preferible a que no salga el archivo.
    """
    for original, llano in REEMPLAZOS.items():
        texto = texto.replace(original, llano)
    return texto.encode("latin-1", "replace").decode("latin-1")


def dinero(valor: float) -> str:
    """`6169.56` -> `"$6,169.56"`. Con separador de miles, que es la
    diferencia entre leer un precio y tener que contar dígitos."""
    return f"${valor:,.2f}"


def fecha_larga(dia: date) -> str:
    """`date(2026, 9, 14)` -> `"14 de septiembre de 2026"`."""
    return f"{dia.day} de {MESES[dia.month - 1]} de {dia.year}"


def fecha_del_dato(iso: str | None) -> str | None:
    """La fecha con la que el CRM marcó el precio, en legible.

    Si viene en un formato que no se entiende, se devuelve None y el PDF
    simplemente no imprime ese renglón: mejor callarlo que imprimir un
    `2026-09-08T06:00:00.000Z` en un documento que va a leer un cliente.
    """
    if not iso:
        return None
    try:
        momento = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return fecha_larga(momento.date())


def nombre_archivo(folio: str) -> str:
    """`"COT-20260914-064512-5678"` -> `"Cotizacion-COT-20260914-064512-5678.pdf"`.

    Es lo que le queda guardado al cliente en el teléfono. `documento.pdf` se
    pierde entre los otros documento.pdf; el folio se puede buscar y es el
    mismo que el del CRM, así que cuando el cliente lo mencione, el vendedor
    sabe de cuál habla.
    """
    limpio = "".join(c for c in folio if c.isalnum() or c in "-_") or "sin-folio"
    return f"Cotizacion-{limpio}.pdf"


def vigencia(dias: int, desde: date) -> date | None:
    """La fecha hasta la que vale el precio, o None si nadie la configuró."""
    return desde + timedelta(days=dias) if dias > 0 else None


def construir(datos: DatosCotizacion) -> bytes:
    """Los bytes del PDF, listos para subir a Meta y para mandar al CRM."""
    emitida = datos.emitida or datetime.now(UTC).date()

    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_title(sanear(f"Cotizacion {datos.folio}"))
    pdf.set_author(sanear(datos.empresa))
    pdf.add_page()
    ancho = pdf.w - pdf.l_margin - pdf.r_margin

    # --- Encabezado --- #
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 9, sanear(datos.empresa), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(
        0,
        5,
        sanear("Comercializadora de granos y commodities a granel"),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.ln(4)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, sanear(f"COTIZACIÓN {datos.folio}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- A nombre de quién, y de cuándo --- #
    pdf.set_font("Helvetica", "", 10)
    for etiqueta, valor in _datos_del_cliente(datos, emitida):
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(32, 5.5, sanear(etiqueta))
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 5.5, sanear(valor), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # --- La partida --- #
    col_producto = ancho - 100
    pdf.set_fill_color(235, 235, 235)
    pdf.set_font("Helvetica", "B", 10)
    for etiqueta, w, alineacion in (
        ("Producto", col_producto, "L"),
        ("Toneladas", 26, "R"),
        ("Precio / ton", 34, "R"),
        ("Importe", 40, "R"),
    ):
        pdf.cell(w, 8, sanear(etiqueta), border=1, align=alineacion, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 10)
    for valor, w, alineacion in (
        (datos.producto, col_producto, "L"),
        (f"{datos.cantidad_ton:,.3f}", 26, "R"),
        (dinero(datos.precio_ton), 34, "R"),
        (dinero(datos.subtotal), 40, "R"),
    ):
        pdf.cell(w, 8, sanear(valor), border=1, align=alineacion)
    pdf.ln()

    # --- Totales --- #
    etiquetas = ancho - 40
    for etiqueta, valor, negrita in _renglones_del_total(datos):
        pdf.set_font("Helvetica", "B" if negrita else "", 11 if negrita else 10)
        pdf.cell(etiquetas, 7, sanear(etiqueta), align="R")
        pdf.cell(40, 7, sanear(valor), align="R", border="T" if negrita else 0)
        pdf.ln()

    # --- Lo que el cliente tiene que saber --- #
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(70, 70, 70)
    for nota in _notas_al_pie(datos):
        pdf.multi_cell(0, 4.6, sanear(nota), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)

    return bytes(pdf.output())


def _datos_del_cliente(datos: DatosCotizacion, emitida: date) -> list[tuple[str, str]]:
    renglones = [("Cliente:", datos.cliente), ("Fecha:", fecha_larga(emitida))]
    if datos.telefono:
        renglones.insert(1, ("Teléfono:", datos.telefono))
    if datos.vigencia_hasta:
        renglones.append(("Vigencia:", fecha_larga(datos.vigencia_hasta)))
    return renglones


def _renglones_del_total(datos: DatosCotizacion) -> list[tuple[str, str, bool]]:
    """El total, y el IVA solo si alguien lo configuró.

    Con la tasa en cero no se imprime "IVA $0.00": un cero puede leerse como
    "no causa impuesto", que es una afirmación fiscal que este módulo no está
    en posición de hacer. Simplemente no se menciona.
    """
    if datos.tasa_iva <= 0:
        return [(f"Total {datos.moneda}:", dinero(datos.total), True)]
    return [
        ("Subtotal:", dinero(datos.subtotal), False),
        (f"IVA ({datos.tasa_iva * 100:g} %):", dinero(datos.iva), False),
        (f"Total {datos.moneda}:", dinero(datos.total), True),
    ]


def _notas_al_pie(datos: DatosCotizacion) -> list[str]:
    notas = [
        f"Precios en {datos.moneda} por tonelada. Sujetos a confirmación y a "
        "disponibilidad de producto al momento de cerrar el pedido.",
    ]
    fecha = fecha_del_dato(datos.precio_actualizado_el)
    if fecha:
        notas.append(f"Precio vigente al {fecha}.")
    if not datos.vigencia_hasta:
        # Sin vigencia configurada NO se calla el asunto: se dice que hay que
        # confirmar. El grano se mueve de precio y el cliente hace cuentas con
        # lo que se le diga.
        notas.append(
            "Esta cotización no lleva fecha de vigencia: confirme el precio con "
            "su asesor antes de cerrar."
        )
    notas.append(
        f"Documento generado automáticamente por el asistente de WhatsApp de "
        f"{datos.empresa}. Folio {datos.folio}."
    )
    return notas
