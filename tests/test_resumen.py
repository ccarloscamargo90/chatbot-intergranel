"""El resumen de lo que se habló, que es lo que lee el vendedor.

Dos cosas se prueban aquí: que la transcripción que se le manda al modelo sea
la conversación y nada más (sin los bloques del bucle de herramientas), y que
los HECHOS sobrevivan aunque el modelo no conteste. Sin red (regla 6).
"""

import asyncio

from app.resumen import redactar, transcripcion

HECHOS = ["- Pidió Maíz blanco, 50.000 t.", "- Ya recibió el PDF."]

HISTORIAL = [
    {"role": "user", "content": "¿A cómo está el maíz blanco?"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Le consulto el precio."},
            {"type": "tool_use", "id": "t1", "name": "consultar_precio", "input": {}},
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": '{"precio_ton": 6169.56}'}
        ],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "Está en $6,169.56."}]},
    {"role": "user", "content": "Necesito 50 toneladas para mi planta en Celaya."},
]


class _Bloque:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Respuesta:
    def __init__(self, texto: str) -> None:
        self.content = [_Bloque(texto)]


class _ClienteFalso:
    """Un Claude de mentiras que apunta lo que se le pidió."""

    def __init__(self, texto: str = "- Es para su planta en Celaya.") -> None:
        self.texto = texto
        self.peticiones: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.peticiones.append(kwargs)
        return _Respuesta(self.texto)


class _ClienteCaido(_ClienteFalso):
    async def create(self, **kwargs):
        raise RuntimeError("overloaded_error")


# --- La transcripción ------------------------------------------------------ #


def test_la_transcripcion_lleva_la_conversacion_y_dice_quien_habla():
    texto = transcripcion(HISTORIAL)
    assert "Cliente: ¿A cómo está el maíz blanco?" in texto
    assert "Asistente: Está en $6,169.56." in texto
    assert "Cliente: Necesito 50 toneladas para mi planta en Celaya." in texto


def test_los_bloques_de_herramientas_no_van_en_la_transcripcion():
    """Al vendedor no le dicen nada, y sin declarar esas mismas herramientas
    la petición al modelo sería inválida."""
    texto = transcripcion(HISTORIAL)
    assert "tool_use" not in texto
    assert "tool_result" not in texto
    assert "6169.56" not in texto  # el JSON crudo de la tool no viaja


def test_solo_los_ultimos_mensajes():
    largo = [{"role": "user", "content": f"mensaje {i}"} for i in range(50)]
    texto = transcripcion(largo, maximo=3)
    assert "mensaje 49" in texto
    assert "mensaje 46" not in texto


# --- La nota --------------------------------------------------------------- #


def test_la_nota_lleva_los_hechos_y_debajo_el_contexto():
    cliente = _ClienteFalso()
    nota = asyncio.run(redactar(hechos=HECHOS, historial=HISTORIAL, client=cliente))
    assert nota.startswith("- Pidió Maíz blanco, 50.000 t.")
    assert "- Es para su planta en Celaya." in nota


def test_si_el_modelo_falla_la_nota_sigue_diciendo_lo_esencial():
    """Media nota es mucho mejor que ninguna: hoy no existe ninguna."""
    nota = asyncio.run(redactar(hechos=HECHOS, historial=HISTORIAL, client=_ClienteCaido()))
    assert nota == "\n".join(HECHOS)


def test_sin_conversacion_no_se_le_pregunta_nada_al_modelo():
    cliente = _ClienteFalso()
    nota = asyncio.run(redactar(hechos=HECHOS, historial=[], client=cliente))
    assert nota == "\n".join(HECHOS)
    assert cliente.peticiones == []


def test_al_modelo_se_le_prohibe_inventar_y_hablar_de_inventario():
    cliente = _ClienteFalso()
    asyncio.run(redactar(hechos=HECHOS, historial=HISTORIAL, client=cliente))
    instrucciones = cliente.peticiones[0]["system"]
    assert "ESCRIBE SOLO lo que aparece en la transcripción" in instrucciones
    assert "NO menciones existencias ni inventario" in instrucciones
