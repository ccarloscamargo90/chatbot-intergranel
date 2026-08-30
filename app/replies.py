"""Respuestas del bot: texto y, cuando ayuda, botones.

WhatsApp no solo manda texto. Para una consulta de autoservicio ("¿qué le
debo?", "¿dónde va mi pedido?") escribir la pregunta es fricción: el cliente
tiene que adivinar cómo se dice. Un botón elimina esa adivinanza y, de paso,
nos da una intención EXACTA en vez de una frase que hay que clasificar.

Meta ofrece dos controles y cada uno tiene su límite duro:

- `button` — hasta 3 botones de respuesta rápida, título de 20 caracteres.
- `list`   — hasta 10 filas repartidas en secciones, título de 24 caracteres.

Esos límites no son negociables: si se pasan, Meta rechaza el mensaje entero y
el cliente no recibe NADA. Por eso `Reply` los recorta aquí, al construir, y no
en el momento del envío: más vale un título truncado que un mensaje perdido.

Un agente devuelve `Reply`; `WhatsAppClient.send_reply` decide el tipo de
mensaje. `Reply.coerce` acepta un string pelón, así que un agente que solo
conteste texto no tiene que saber que esto existe.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Límites de la Cloud API de Meta (documentados arriba).
MAX_BOTONES = 3
MAX_TITULO_BOTON = 20
MAX_FILAS_LISTA = 10
MAX_TITULO_FILA = 24
MAX_DESCRIPCION_FILA = 72
MAX_TITULO_BOTON_LISTA = 20
MAX_CUERPO = 1024
MAX_ID = 256


def _recortar(texto: str, largo: int) -> str:
    texto = " ".join(texto.split())
    return texto if len(texto) <= largo else texto[: largo - 1].rstrip() + "…"


@dataclass(frozen=True)
class Boton:
    """Botón de respuesta rápida. `id` es lo que vuelve en el webhook."""

    id: str
    titulo: str

    def to_payload(self) -> dict:
        return {
            "type": "reply",
            "reply": {"id": self.id[:MAX_ID], "title": _recortar(self.titulo, MAX_TITULO_BOTON)},
        }


@dataclass(frozen=True)
class OpcionLista:
    """Fila de un menú de lista."""

    id: str
    titulo: str
    descripcion: str = ""

    def to_payload(self) -> dict:
        fila = {"id": self.id[:MAX_ID], "title": _recortar(self.titulo, MAX_TITULO_FILA)}
        if self.descripcion:
            fila["description"] = _recortar(self.descripcion, MAX_DESCRIPCION_FILA)
        return fila


@dataclass(frozen=True)
class MenuLista:
    """Menú de lista: el botón que lo abre y sus filas."""

    boton: str
    opciones: list[OpcionLista]
    seccion: str = "Opciones"

    def to_payload(self) -> dict:
        return {
            "button": _recortar(self.boton, MAX_TITULO_BOTON_LISTA),
            "sections": [
                {
                    "title": _recortar(self.seccion, MAX_TITULO_FILA),
                    "rows": [o.to_payload() for o in self.opciones[:MAX_FILAS_LISTA]],
                }
            ],
        }


@dataclass
class Reply:
    """Lo que un agente devuelve: texto y, opcionalmente, botones o un menú.

    `botones` y `lista` son excluyentes; si vienen los dos gana el menú, que es
    el control con más capacidad.
    """

    texto: str
    botones: list[Boton] = field(default_factory=list)
    lista: MenuLista | None = None
    # Encabezado y pie del mensaje interactivo (se ignoran en texto pelón).
    encabezado: str = ""
    pie: str = ""

    def __post_init__(self) -> None:
        self.botones = list(self.botones)[:MAX_BOTONES]

    @property
    def es_interactiva(self) -> bool:
        return bool(self.lista or self.botones)

    @classmethod
    def coerce(cls, valor: Reply | str) -> Reply:
        """Normaliza un string a `Reply` (compatibilidad con código que solo
        devuelve texto)."""
        return valor if isinstance(valor, Reply) else cls(texto=str(valor))

    def con(self, *, botones: list[Boton] | None = None, lista: MenuLista | None = None) -> Reply:
        """Copia con botones o menú, dejando el texto tal cual."""
        return Reply(
            texto=self.texto,
            botones=botones if botones is not None else self.botones,
            lista=lista if lista is not None else self.lista,
            encabezado=self.encabezado,
            pie=self.pie,
        )
