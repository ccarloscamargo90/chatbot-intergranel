"""El resumen de lo que se habló, para que el vendedor no tenga que leer el chat.

Lo que llegaba al CRM de una cotización por WhatsApp era un nombre, un
teléfono y un PDF. El vendedor marcaba a ciegas: no sabía si el cliente
preguntó por el precio de pasada o si tiene una planta parada, si el grano es
para engorda o para harina, ni qué le prometió el bot. Este módulo escribe
esa nota.

**Con el modelo de los agentes, no con el clasificador rápido.** Es el mismo
criterio que la lectura de las respuestas de los transportistas
(`fletes.py`): clasificar mal manda una conversación al agente equivocado y se
corrige al siguiente mensaje; resumir mal deja escrita una frase que un
vendedor va a leer como si fuera lo que dijo el cliente, y va a marcarle con
eso en la cabeza.

**Los HECHOS no los redacta el modelo.** Producto, toneladas, nombre y folio
viajan aparte, tomados de lo que devolvió la herramienta, y se anteponen al
texto. El modelo solo aporta el contexto de la plática. Así, si el modelo se
queda corto, la nota sigue diciendo lo esencial; y lo esencial no puede salir
distinto de lo que se registró en la cotización.
"""

from __future__ import annotations

import logging

import anthropic

from .config import get_settings

logger = logging.getLogger(__name__)

#: Cuántos mensajes del final de la conversación se le pasan al modelo.
MAX_MENSAJES = 30
#: La nota es corta a propósito: un vendedor la lee de reojo antes de marcar.
MAX_TOKENS = 700
#: Tope de caracteres de la transcripción, para no mandar un chat enorme.
MAX_CARACTERES = 12_000

SYSTEM_PROMPT = """\
Preparas notas internas para el vendedor de una comercializadora de granos a \
granel. Recibes la transcripción de una conversación de WhatsApp entre un \
cliente y el asistente automático, y escribes lo que el vendedor necesita \
saber antes de marcarle.

Reglas que no se rompen:
- ESCRIBE SOLO lo que aparece en la transcripción. Si un dato no se dijo, NO \
lo escribas y NO lo supongas. Nada de "probablemente", nada de rellenar \
huecos, nada de recomendaciones tuyas.
- Si el cliente apenas dijo algo, dilo en un renglón: "Solo preguntó precio; \
no dio más datos." Eso es más útil que un párrafo inventado.
- NO repitas el precio, el total ni las toneladas: ya están en la cotización \
que el vendedor tiene enfrente.
- NO menciones existencias ni inventario.
- Sin saludos, sin despedidas, sin firmar, sin encabezado.

Formato: de uno a seis renglones, cada uno empezando con "- ". Cubre, SOLO si \
se dijo: para qué o para quién es el producto, plaza o destino, cuándo lo \
necesita, volumen recurrente o de una sola vez, forma de pago o crédito, \
dudas y objeciones, y qué quedó pendiente o qué se le prometió.

Escribe en español, en tercera persona, corto y en seco.
"""


def transcripcion(historial: list, *, maximo: int = MAX_MENSAJES) -> str:
    """La conversación en texto plano: quién dijo qué.

    El historial guarda además los bloques de `tool_use` y `tool_result` del
    bucle agéntico. No se incluyen: al vendedor no le dicen nada, y volver a
    mandárselos a un modelo sin declarar esas mismas herramientas sería una
    petición inválida.
    """
    renglones: list[str] = []
    for mensaje in historial[-maximo:]:
        quien = "Cliente" if mensaje.get("role") == "user" else "Asistente"
        texto = _texto_del_mensaje(mensaje.get("content"))
        if texto:
            renglones.append(f"{quien}: {texto}")
    return "\n".join(renglones)[-MAX_CARACTERES:]


def _texto_del_mensaje(contenido) -> str:
    if isinstance(contenido, str):
        return contenido.strip()
    if isinstance(contenido, list):
        partes = [
            (b.get("text") or "").strip()
            for b in contenido
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(p for p in partes if p)
    return ""


async def redactar(
    *,
    hechos: list[str],
    historial: list,
    client: anthropic.AsyncAnthropic | None = None,
    model: str | None = None,
) -> str:
    """La nota para el vendedor: los hechos y, debajo, el contexto.

    Nunca levanta: si el modelo falla o no hay con qué llamarlo, devuelve los
    hechos solos. Media nota es mucho mejor que ninguna — y una nota es lo que
    hoy no existe.
    """
    encabezado = "\n".join(hechos)
    charla = transcripcion(historial)
    if not charla.strip():
        return encabezado

    contexto = await _contexto_de_la_platica(charla, client=client, model=model)
    if not contexto:
        return encabezado
    return f"{encabezado}\n\n{contexto}"


async def _contexto_de_la_platica(
    charla: str,
    *,
    client: anthropic.AsyncAnthropic | None,
    model: str | None,
) -> str:
    settings = get_settings()
    if client is None:
        if not settings.anthropic_api_key:
            logger.info("Sin ANTHROPIC_API_KEY: la nota va con los hechos solos")
            return ""
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        respuesta = await client.messages.create(
            model=model or settings.claude_model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Transcripción de la conversación:\n\n{charla}",
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - la nota es accesoria, no puede tumbar nada
        logger.warning("No se pudo redactar el resumen: %s", exc)
        return ""
    return "".join(
        b.text for b in respuesta.content if getattr(b, "type", "") == "text"
    ).strip()
