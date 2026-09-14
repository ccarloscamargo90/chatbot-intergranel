"""El PDF de la cotización: lo único que el cliente se queda en la mano.

Lo que se prueba aquí no es "se ve bonito", es que el archivo salga —y salga
con lo que hace falta para decidir— y que dos condiciones comerciales que el
bot NO decide (IVA y vigencia) no aparezcan solas.
"""

from datetime import date

import pytest

from app.cotizacion_pdf import (
    DatosCotizacion,
    _notas_al_pie,
    _renglones_del_total,
    construir,
    dinero,
    fecha_del_dato,
    fecha_larga,
    nombre_archivo,
    sanear,
    vigencia,
)

FOLIO = "COT-20260914-064512-5678"


def _datos(**extra) -> DatosCotizacion:
    base = {
        "folio": FOLIO,
        "cliente": "Molinos del Bajío S.A. de C.V.",
        "producto": "Maíz blanco",
        "cantidad_ton": 50.0,
        "precio_ton": 6169.56,
        "empresa": "Intergranel",
        "telefono": "5215512345678",
        "precio_actualizado_el": "2026-09-08T06:00:00.000Z",
        "emitida": date(2026, 9, 14),
    }
    return DatosCotizacion(**{**base, **extra})


# --- El archivo sale ------------------------------------------------------- #


def test_es_un_pdf_de_verdad():
    contenido = construir(_datos())
    assert contenido.startswith(b"%PDF-")
    assert contenido.rstrip().endswith(b"%%EOF")


def test_un_nombre_de_cliente_con_caracteres_raros_no_tumba_el_archivo():
    """Lo que el cliente escribe por WhatsApp llega como sea.

    Las fuentes base de un PDF codifican latin-1; un emoji o una comilla de
    imprenta reventaría la generación, y con ella la cotización.
    """
    contenido = construir(_datos(cliente="Alimentos “El Sol” — Planta 2 🌽"))
    assert contenido.startswith(b"%PDF-")


def test_los_acentos_se_conservan_y_lo_que_no_cabe_se_sustituye():
    assert sanear("Maíz blanco, señor") == "Maíz blanco, señor"
    assert sanear("precio — alto") == "precio - alto"
    assert sanear("“comillas”") == '"comillas"'


# --- El nombre con el que le queda guardado -------------------------------- #


def test_el_archivo_lleva_el_folio_en_el_nombre():
    """`documento.pdf` se pierde entre los otros documento.pdf."""
    assert nombre_archivo(FOLIO) == f"Cotizacion-{FOLIO}.pdf"


def test_un_folio_con_basura_no_se_convierte_en_una_ruta():
    assert nombre_archivo("../../etc/passwd") == "Cotizacion-etcpasswd.pdf"
    assert nombre_archivo("") == "Cotizacion-sin-folio.pdf"


# --- Las dos cifras que el bot no inventa ---------------------------------- #


def test_sin_iva_configurado_el_pdf_no_menciona_impuestos():
    """Ni "+16%" ni "IVA $0.00": las dos son afirmaciones fiscales."""
    renglones = _renglones_del_total(_datos())
    assert len(renglones) == 1
    etiqueta, valor, _ = renglones[0]
    assert etiqueta == "Total MXN:"
    assert valor == "$308,478.00"
    assert not any("IVA" in r[0] for r in renglones)


def test_con_iva_configurado_se_separa_y_el_total_lo_incluye():
    datos = _datos(tasa_iva=0.16)
    renglones = _renglones_del_total(datos)
    assert [r[0] for r in renglones] == ["Subtotal:", "IVA (16 %):", "Total MXN:"]
    assert datos.subtotal == 308_478.00
    assert datos.iva == 49_356.48
    assert datos.total == 357_834.48


def test_sin_vigencia_configurada_no_se_inventa_una_fecha():
    assert vigencia(0, date(2026, 9, 14)) is None
    notas = " ".join(_notas_al_pie(_datos()))
    # En su lugar se dice la verdad: hay que confirmar.
    assert "no lleva fecha de vigencia" in notas


def test_con_vigencia_configurada_se_cuenta_desde_la_emision():
    assert vigencia(5, date(2026, 9, 14)) == date(2026, 9, 19)
    notas = " ".join(_notas_al_pie(_datos(vigencia_hasta=date(2026, 9, 19))))
    assert "no lleva fecha de vigencia" not in notas


# --- De cuándo es el precio ------------------------------------------------ #


def test_el_pie_dice_de_cuando_es_el_precio():
    """Un precio sin fecha invita a confiar en él lleve tres días parado."""
    notas = " ".join(_notas_al_pie(_datos()))
    assert "Precio vigente al 8 de septiembre de 2026" in notas


def test_una_fecha_que_no_se_entiende_no_se_imprime_en_crudo():
    assert fecha_del_dato("el martes") is None
    assert fecha_del_dato(None) is None
    notas = " ".join(_notas_al_pie(_datos(precio_actualizado_el="el martes")))
    assert "Precio vigente" not in notas


# --- Formato --------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [(6169.56, "$6,169.56"), (308478.0, "$308,478.00"), (0.0, "$0.00")],
)
def test_el_dinero_lleva_separador_de_miles(valor, esperado):
    assert dinero(valor) == esperado


def test_la_fecha_va_en_español_sin_depender_del_locale():
    assert fecha_larga(date(2026, 9, 14)) == "14 de septiembre de 2026"
