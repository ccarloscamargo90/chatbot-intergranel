"""La referencia con la que llega quien escribe desde la página web.

Dos contratos que no se pueden romper sin que nadie se entere: el formato que
genera la página (`intergranel-web/src/lib/atribucion.ts`) y el que lee el CRM
(`contact-ref.ts`). Si el bot deja de reconocer la referencia, el prospecto
vuelve a caer en Soporte y entra al CRM sin campaña; si la reconoce donde no
la hay, a alguien que pregunta por un contrato se le contesta con un saludo.
"""

import asyncio

import pytest

from app.atribucion import (
    REFERENCIA_TTL_SECONDS,
    TEXTOS_DE_LA_PAGINA,
    ReferenciasWeb,
    es_texto_de_la_pagina,
    extraer_referencia,
    sin_referencia,
)
from app.bus import InMemoryEventBus

# Los dos mensajes que manda la página, tal cual quedan con la referencia.
DEL_BOTON_FLOTANTE = (
    "Hola Intergranel, quisiera información sobre sus granos y servicios. "
    "(ref: IG-SOC-4M2P6X)"
)
DE_UN_ENLACE_SIN_TEXTO = "Hola, me gustaría recibir más información (ref: IG-ADS-K7Q9RW)"


# --- Leer la referencia --------------------------------------------------- #


@pytest.mark.parametrize(
    ("mensaje", "esperada"),
    [
        (DEL_BOTON_FLOTANTE, "IG-SOC-4M2P6X"),
        (DE_UN_ENLACE_SIN_TEXTO, "IG-ADS-K7Q9RW"),
        # Las referencias de 4 que ya andan circulando siguen valiendo.
        ("Hola (ref: IG-DIR-7F3K)", "IG-DIR-7F3K"),
        # Se normaliza: es la llave con la que el CRM busca la visita.
        ("hola (ref: ig-meta-4m2p6x)", "IG-META-4M2P6X"),
    ],
)
def test_reconoce_la_referencia_de_la_pagina(mensaje, esperada):
    assert extraer_referencia(mensaje) == esperada


@pytest.mark.parametrize(
    "mensaje",
    [
        "Quiero ver el contrato CONT-2026-0001",
        "¿Cómo va mi factura FACT-2026-0031?",
        # Otra marca del grupo: no es la página de este número.
        "Hola (ref: MC-ADS-7F3K)",
        # Muy corto para ser un folio.
        "Hola (ref: IG-SOC-4M2)",
        # O y 0, I y 1 no existen en el alfabeto del folio.
        "Hola (ref: IG-SOC-O0I1AB)",
        "",
        None,
    ],
)
def test_no_ve_referencias_donde_no_las_hay(mensaje):
    assert extraer_referencia(mensaje) is None


def test_un_folio_mas_largo_de_la_cuenta_no_se_recorta():
    """Recortarlo a 8 buscaría el boleto de otra visita y le colgaría a este
    prospecto la campaña de otra persona."""
    assert extraer_referencia("(ref: IG-SOC-ABCDEFGHJ)") is None


def test_al_agente_le_llega_el_mensaje_sin_la_referencia():
    assert sin_referencia(DEL_BOTON_FLOTANTE) == (
        "Hola Intergranel, quisiera información sobre sus granos y servicios."
    )
    assert sin_referencia("Quiero 40 t de maíz (ref: IG-SOC-4M2P6X)") == "Quiero 40 t de maíz"


# --- ¿Solo tocó el botón, o escribió algo? ---------------------------------- #


@pytest.mark.parametrize(
    "texto", [*TEXTOS_DE_LA_PAGINA, "", "hola, me gustaria recibir mas informacion"]
)
def test_el_texto_por_omision_de_la_pagina_se_reconoce(texto):
    assert es_texto_de_la_pagina(texto) is True


@pytest.mark.parametrize(
    "texto",
    ["Necesito 40 toneladas de maíz blanco en Celaya", "¿A cómo está el maíz?", "Hola"],
)
def test_lo_que_la_persona_escribio_no_se_tapa_con_un_saludo(texto):
    assert es_texto_de_la_pagina(texto) is False


# --- Recordarla para el CRM ------------------------------------------------ #


def test_la_referencia_se_recuerda_por_telefono():
    referencias = ReferenciasWeb(InMemoryEventBus())

    async def escenario():
        assert await referencias.leer("521") is None
        await referencias.guardar("521", "IG-SOC-4M2P6X")
        # Una visita nueva trae su propio boleto: manda la última.
        await referencias.guardar("521", "IG-ADS-K7Q9RW")
        return await referencias.leer("521"), await referencias.leer("522")

    assert asyncio.run(escenario()) == ("IG-ADS-K7Q9RW", None)


def test_se_recuerda_lo_mismo_que_la_pagina_recuerda_el_primer_toque():
    """En grano el ciclo de compra es largo: quien pidió información hoy puede
    cotizar dentro de un mes, y esa cotización también es de la campaña."""
    assert REFERENCIA_TTL_SECONDS == 90 * 24 * 60 * 60
