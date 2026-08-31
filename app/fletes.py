"""Cotización de fletes por WhatsApp: la mitad del bot (ERP · BUG-77).

El ERP arma la solicitud, decide a quién se le pregunta y hace cumplir el tope
por transportista y por día. Aquí pasan las dos cosas que el ERP no puede hacer:
**mandar el mensaje** y **entender la respuesta**.

Dos reglas gobiernan todo lo de abajo:

1. **Al transportista le sale el texto del ERP, tal cual.** Ni plantilla, ni
   firma, ni marca añadida por el bot. El ERP ya lo compuso con la razón social
   y le quitó las marcas reservadas a clientes: cualquier cosa que el bot le
   agregue puede volver a meter lo que allá se quitó a propósito.
2. **El bot interpreta, no decide.** Lo que entiende viaja al ERP JUNTO con el
   texto crudo, y el ERP conserva los dos. Si no se entiende, se deposita igual
   y el ERP lo marca para revisión — una respuesta que llegó es un hecho,
   aunque nadie la haya podido leer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

from .bus import EventBus
from .models import InterpretacionFlete, SolicitudFletePendiente

logger = logging.getLogger(__name__)

# El `tipo` con el que el ERP marca una solicitud de cotización de flete. Es lo
# que la distingue de un aviso interno al equipo, que va por otro camino.
TIPO_SOLICITUD_FLETE = "cotizacion_flete.solicitud"

# Un transportista contesta cuando puede: a las tres horas, o al día siguiente
# cuando se baja del camión. Tres días es lo que dura la pregunta abierta.
PENDIENTE_TTL_SECONDS = 3 * 24 * 3600

_PREFIJO_REFERENCIA = "cotizacion_flete:"

UNIDADES_VALIDAS = {"POR_TONELADA", "POR_VIAJE", "POR_KILOMETRO"}

INTERPRETE_SYSTEM = (
    "Eres un asistente que lee la respuesta de un TRANSPORTISTA mexicano al que se le "
    "pidió precio para un flete, y la convierte en datos.\n\n"
    "Responde SOLO un objeto JSON con estas llaves:\n"
    '  "monto_pesos": number|null  — el precio en PESOS MXN. "28 mil" son 28000.\n'
    '  "unidad": "POR_TONELADA"|"POR_VIAJE"|"POR_KILOMETRO"|null\n'
    '  "capacidad_toneladas": number|null — lo que le cabe a su unidad.\n'
    '  "incluye_maniobras": true|false|null\n'
    '  "incluye_casetas": true|false|null\n'
    '  "disponible_desde": "YYYY-MM-DD"|null\n'
    '  "declina": true|false — dijo que no puede o no le interesa.\n\n'
    "REGLAS, en orden de importancia:\n"
    "1. Lo que NO dijo va en null. NUNCA supongas. Si no aclaró si incluye casetas, "
    "es null, no false: 'no lo dijo' y 'no las incluye' son cosas distintas y cuestan "
    "distinto.\n"
    "2. Si no entiendes el mensaje, o no trae precio, deja monto_pesos y unidad en null. "
    "Es preferible a inventar un número: ese número se compara contra otros y se paga.\n"
    "3. 'el viaje', 'el flete completo', 'por camión' → POR_VIAJE. "
    "'la tonelada', 'por ton' → POR_TONELADA. Si dice un precio sin decir de qué, "
    "deja la unidad en null.\n"
    "4. No expliques nada. Solo el JSON."
)


def referencia_de(solicitud_id: str) -> str:
    return f"{_PREFIJO_REFERENCIA}{solicitud_id}"


def es_solicitud_de_flete(tipo: str) -> bool:
    """¿Este aviso del ERP va hacia un TRANSPORTISTA y no hacia el equipo?"""
    return tipo == TIPO_SOLICITUD_FLETE


class FletesPendientes:
    """A qué teléfono se le preguntó un precio y sigue sin contestar.

    Vive en el bus, con la misma convención que el resto (`bus:{agente}:...`).
    Es lo que permite amarrar un "28 mil" suelto, tres horas después, con la
    cotización que lo estaba esperando — sin eso, el ERP tendría que adivinar.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    @staticmethod
    def _clave(telefono: str) -> str:
        return f"bus:flete:pendiente:{telefono}"

    async def marcar(self, telefono: str, referencia: str | None, aviso_id: str) -> None:
        await self._bus.publish(
            self._clave(telefono),
            {"referencia": referencia, "aviso_id": aviso_id},
            ttl=PENDIENTE_TTL_SECONDS,
        )

    async def obtener(self, telefono: str) -> SolicitudFletePendiente | None:
        datos = await self._bus.read(self._clave(telefono))
        if not datos:
            return None
        return SolicitudFletePendiente(
            telefono=telefono,
            referencia=datos.get("referencia"),
            aviso_id=datos.get("aviso_id", ""),
        )

    async def limpiar(self, telefono: str) -> None:
        """Ya contestó con un precio (o declinó): la pregunta se cierra.

        Se limpia SOLO cuando hay respuesta interpretable. Mientras no la haya,
        el teléfono sigue marcado y lo que escriba se sigue depositando: la
        conversación con un transportista al que le preguntamos un precio no es
        una consulta de cliente, y mandarla al agente de ventas contestaría otra
        cosa.
        """
        await self._bus.publish(self._clave(telefono), {}, ttl=1)


def _a_centavos(pesos: Any) -> int | None:
    """Pesos a centavos, redondeando.

    Se le pide PESOS al modelo y se convierte aquí a propósito: pedirle
    centavos invita a un error de x100 que nadie ve hasta que se paga.
    """
    if pesos is None or isinstance(pesos, bool):
        return None
    try:
        valor = float(pesos)
    except (TypeError, ValueError):
        return None
    if valor < 0:
        return None
    return round(valor * 100)


def _bool_o_none(valor: Any) -> bool | None:
    return valor if isinstance(valor, bool) else None


def _numero_o_none(valor: Any) -> float | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return float(valor)


def _fecha_o_none(valor: Any) -> str | None:
    if not isinstance(valor, str):
        return None
    return valor if re.fullmatch(r"\d{4}-\d{2}-\d{2}", valor.strip()) else None


def parsear_interpretacion(crudo: str) -> InterpretacionFlete | None:
    """Convierte lo que devolvió el modelo en una interpretación utilizable.

    Devuelve None si no se pudo leer. No es una falla: el ERP recibe el texto
    crudo igual y lo marca para revisión de una persona.
    """
    texto = crudo.strip()
    # El modelo a veces envuelve el JSON en ```json ... ```.
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", texto).strip()
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fin == -1 or fin < inicio:
        return None
    try:
        datos = json.loads(texto[inicio : fin + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(datos, dict):
        return None

    unidad = datos.get("unidad")
    unidad = unidad if unidad in UNIDADES_VALIDAS else None
    monto = _a_centavos(datos.get("monto_pesos"))
    capacidad = datos.get("capacidad_toneladas")
    declina = datos.get("declina") is True

    # Un precio sin unidad no se puede comparar contra nada, así que no es un
    # precio: se deja sin monto y el ERP lo manda a revisión.
    if monto is not None and unidad is None:
        monto = None

    return InterpretacionFlete(
        montoCentavos=monto,
        unidad=unidad,
        capacidadToneladas=_numero_o_none(capacidad),
        incluyeManiobras=_bool_o_none(datos.get("incluye_maniobras")),
        incluyeCasetas=_bool_o_none(datos.get("incluye_casetas")),
        disponibleDesde=_fecha_o_none(datos.get("disponible_desde")),
        declina=declina,
    )


async def interpretar_respuesta(
    client: anthropic.AsyncAnthropic, modelo: str, texto: str
) -> InterpretacionFlete | None:
    """Lee la respuesta del transportista y la convierte en datos.

    Usa el modelo de los AGENTES y no el clasificador rápido del router a
    propósito: clasificar mal manda una conversación al agente equivocado y se
    corrige en el siguiente mensaje; leer mal un precio mete un número que se
    compara contra otros, se elige y se paga.

    Nunca lanza. Si algo falla, devuelve None y el mensaje se deposita crudo
    para que lo lea una persona.
    """
    if not texto.strip():
        return None
    try:
        respuesta = await client.messages.create(
            model=modelo,
            max_tokens=400,
            system=INTERPRETE_SYSTEM,
            messages=[{"role": "user", "content": texto}],
        )
        crudo = "".join(
            b.text for b in respuesta.content if getattr(b, "type", "") == "text"
        )
        return parsear_interpretacion(crudo)
    except Exception:  # noqa: BLE001 - una lectura fallida no puede perder el mensaje
        logger.exception("No se pudo interpretar la respuesta de flete")
        return None


def acuse(interpretacion: InterpretacionFlete | None) -> str:
    """Lo que se le contesta al transportista.

    Corto y sin promesas: no sabemos todavía si se le va a dar el viaje, y
    decirle que "lo contactamos" cuando la decisión puede tardar días es la
    forma de quedar mal con quien queremos que nos vuelva a contestar.
    """
    if interpretacion is None:
        return "Gracias. Recibimos su mensaje y lo estamos revisando."
    if interpretacion.declina:
        return "Gracias por avisarnos. Le escribimos en el próximo viaje."
    if interpretacion.montoCentavos is None:
        return "Gracias. Recibimos su mensaje y lo estamos revisando."
    return "Gracias, quedó registrado su precio. Le avisamos en cuanto se decida el viaje."
