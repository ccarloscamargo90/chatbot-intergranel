"""La referencia con la que llega quien escribe desde la página web.

El botón de WhatsApp de intergranel.com abre la conversación con un texto ya
escrito y, al final, una referencia corta:

    Hola Intergranel, quisiera información sobre sus granos y servicios. (ref: IG-SOC-4M2P6X)

Ese `(ref: …)` es lo ÚNICO que sobrevive el salto del navegador a WhatsApp —ahí
se pierden cookies, almacenamiento y parámetros de la URL—, y le sirve a dos
cosas distintas:

1. **Al bot, para saber cómo recibir.** Quien toca el botón de la página casi
   siempre viene a comprar. Antes el mensaje se clasificaba como "duda
   general", caía en Soporte y se le ofrecían contratos y facturas a alguien
   que todavía no ha comprado nada. Ahora se le recibe como prospecto: con la
   bienvenida de ventas y un botón para cotizar.
2. **Al CRM, para saber de qué campaña vino.** La página deja guardada la
   visita completa contra ese folio; el CRM la recupera cuando el bot le manda
   la referencia (`contactRef`) al registrar la cotización o la canalización.
   Sin eso, el prospecto de WhatsApp entraba al CRM sin atribución.

El formato lo fija el contrato de atribución del grupo, del lado de la página
(`intergranel-web/src/lib/atribucion.ts`) y del CRM (`contact-ref.ts`):
`<PREFIJO>-<ETIQUETA>-<FOLIO>`. Aquí solo se reconoce el prefijo `IG`: es la
única página que apunta a este número. El canal NO se deduce aquí; la etiqueta
es un resumen y la clasificación la sigue haciendo el CRM con la evidencia
completa.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

from .bus import EventBus

#: El alfabeto del folio, sin letras ambiguas (ni O ni 0, ni I ni 1). Es el
#: mismo que valida el CRM: cambiarlo de un solo lado rompe el contrato.
ALFABETO_FOLIO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: `IG-SOC-4M2P6X`. El lookahead final impide recortar un folio más largo de
#: la cuenta a sus primeros 8 caracteres: eso buscaría el boleto de otra visita.
PATRON_REFERENCIA = re.compile(
    rf"\bIG-([A-Z]+)-([{ALFABETO_FOLIO}]{{4,8}})(?![{ALFABETO_FOLIO}])", re.IGNORECASE
)

#: El envoltorio completo tal como lo escribe la página: `(ref: IG-…)`.
_ENVOLTORIO = re.compile(
    rf"\(\s*ref\s*:\s*IG-[A-Z]+-[{ALFABETO_FOLIO}]{{4,8}}\s*\)", re.IGNORECASE
)

#: Los textos que la página pone por omisión. Si lo que queda al quitar la
#: referencia es uno de estos, la persona no escribió nada propio: solo tocó
#: el botón, y lo que corresponde es la bienvenida. Si escribió otra cosa, esa
#: pregunta se contesta — no se le tapa con un saludo.
#:
#: Si la página cambia el texto, no se rompe nada: el mensaje va al agente de
#: Ventas como cualquier pregunta, que es de todos modos a donde tiene que ir.
TEXTOS_DE_LA_PAGINA = (
    # El botón flotante (`WhatsAppFab.tsx`).
    "Hola Intergranel, quisiera información sobre sus granos y servicios.",
    # Los enlaces sin texto propio (`AtribucionWhatsApp.tsx`, TEXTO_POR_OMISION).
    "Hola, me gustaría recibir más información",
)

#: Cuánto se recuerda la referencia. Los mismos 90 días que la página conserva
#: el primer toque: en grano el ciclo de compra es largo, y quien pidió
#: información hoy puede cotizar dentro de un mes.
REFERENCIA_TTL_SECONDS = 90 * 24 * 60 * 60

REFERENCIA_PREFIX = "bus:web:referencia:"


def extraer_referencia(texto: str | None) -> str | None:
    """`"… (ref: ig-soc-4m2p6x)"` -> `"IG-SOC-4M2P6X"`, o None si no trae.

    Se normaliza a mayúsculas porque es la llave con la que el CRM busca el
    boleto de la visita: normalizar distinto aquí que allá lo dejaría huérfano.
    """
    if not texto:
        return None
    hallazgo = PATRON_REFERENCIA.search(texto)
    if hallazgo is None:
        return None
    return f"IG-{hallazgo.group(1).upper()}-{hallazgo.group(2).upper()}"


def sin_referencia(texto: str) -> str:
    """El mensaje sin el `(ref: …)`: lo que de verdad dijo la persona.

    Es lo que ve el agente. La referencia no le sirve de nada y, con un folio a
    la vista, un modelo podría tomarla por el de un pedido.
    """
    limpio = _ENVOLTORIO.sub(" ", texto)
    limpio = PATRON_REFERENCIA.sub(" ", limpio)
    return " ".join(limpio.split())


def _normalizar(texto: str) -> str:
    sin_acentos = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in sin_acentos if c.isalnum())


_TEXTOS_NORMALIZADOS = {_normalizar(t) for t in TEXTOS_DE_LA_PAGINA}


def es_texto_de_la_pagina(texto: str) -> bool:
    """True si la persona solo tocó el botón y no escribió nada propio."""
    normalizado = _normalizar(texto)
    return not normalizado or normalizado in _TEXTOS_NORMALIZADOS


class ReferenciasWeb:
    """La última referencia con la que escribió cada teléfono, sobre el bus.

    Se guarda la ÚLTIMA y no la primera: la página ya aplica "primer toque"
    dentro de cada visita, y el folio que trae el mensaje es el que tiene su
    boleto registrado del lado del CRM. Uno viejo apuntaría a otra visita.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def guardar(self, telefono: str, referencia: str) -> None:
        await self._bus.publish(
            f"{REFERENCIA_PREFIX}{telefono}",
            {"ref": referencia, "recibida_en": datetime.now(UTC).isoformat()},
            ttl=REFERENCIA_TTL_SECONDS,
        )

    async def leer(self, telefono: str) -> str | None:
        datos = await self._bus.read(f"{REFERENCIA_PREFIX}{telefono}")
        return (datos or {}).get("ref") or None
