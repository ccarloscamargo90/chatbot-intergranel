"""Cotizar con botones: el flujo guiado del guion de ventas.

Sin red: CRM y ERP simulados, bus e historial en memoria, WhatsApp de mentiras
y sin ANTHROPIC_API_KEY (la nota al vendedor sale con los hechos solos). El
modelo nunca se llama: lo que el flujo no entiende lo devuelve como `None`.
"""

import asyncio

import pytest

from app import tareas
from app.agents.ventas import VentasAgent
from app.bus import InMemoryEventBus
from app.cotizador import (
    es_pregunta,
    leer_toneladas,
    reglas_de,
    siguiente,
)
from app.crm import CRMNoDisponible, MockCRMClient
from app.erp import MockERPClient
from app.history import InMemoryHistoryStore
from app.menus import (
    ACCIONES,
    ASESOR,
    CAMBIOS_COTIZACION,
    COT_CAMBIAR,
    COT_CAMBIAR_TONELADAS,
    COT_CERRAR,
    COT_COSTAL_25,
    COT_COSTAL_50,
    COT_ENTREGA_CHICA,
    COT_GENERAR,
    COT_OTRA_CANTIDAD,
    COT_OTRA_COTIZACION,
    COT_SIN_MARCA,
    COT_UNIDAD_COMPLETA,
    COT_ZONA_CELAYA,
    COT_ZONA_OTRA,
    COTIZAR,
    MENU,
    PREFIJO_PRODUCTO,
    ZONAS_ENTREGA_CHICA,
)
from app.router import Router
from app.sesiones import SesionClienteStore

PHONE = "5215512345678"
UBICACION = (
    "📍 Esta es mi ubicación para la entrega: Bodega Norte (20.52,-100.81) "
    "https://maps.google.com/?q=20.52,-100.81"
)


class WhatsAppDeMentiras:
    def __init__(self) -> None:
        self.enviados: list[dict] = []

    async def upload_media(self, contenido, filename, mime_type):
        return f"media-{filename}"

    async def send_document(self, to, media_id, filename, caption=""):
        self.enviados.append({"to": to, "nombre": filename})
        return {}


@pytest.fixture
def ventas() -> VentasAgent:
    a = VentasAgent.__new__(VentasAgent)
    a._erp = MockERPClient()
    a._crm = MockCRMClient()
    a._bus = InMemoryEventBus()
    a._history_store = InMemoryHistoryStore()
    a._wa = WhatsAppDeMentiras()
    return a


def conversar(ventas, *entradas, texto_libre=True):
    """Manda los toques/textos en orden y devuelve TODAS las respuestas."""

    async def escenario():
        respuestas = []
        for e in entradas:
            respuestas.append(
                await ventas.cotizador.atender(PHONE, e, texto_libre=texto_libre)
            )
        await tareas.esperar_todo(timeout=5)
        return respuestas

    return asyncio.run(escenario())


def ids(reply) -> list[str]:
    return [b.id for b in reply.botones]


def filas(reply) -> list[str]:
    return [o.id for o in reply.lista.opciones]


# ── Reglas sueltas ──────────────────────────────────────────────────────── #


def test_las_reglas_del_guion_son_del_maiz_blanco():
    assert reglas_de("Maíz blanco grado 1").costal
    assert reglas_de("MAIZ BLANCO").volumen_del_guion
    assert not reglas_de("Maíz amarillo grado 2").costal
    assert not reglas_de("Trigo cristalino").volumen_del_guion


@pytest.mark.parametrize(
    ("texto", "toneladas"),
    [("30", 30.0), ("30 t", 30.0), ("12.5 toneladas", 12.5), ("1,000", 1000.0)],
)
def test_lee_toneladas(texto, toneladas):
    assert leer_toneladas(texto) == toneladas


def test_sin_numero_o_con_dos_no_son_toneladas():
    assert leer_toneladas("unas cuantas") is None
    assert leer_toneladas("entre 20 y 30") is None
    assert leer_toneladas("0") is None


def test_una_duda_no_se_confunde_con_la_respuesta():
    assert es_pregunta("¿Cuánto cuesta el flete?")
    assert es_pregunta("cuanto sale a leon")
    assert not es_pregunta("Tortillería La Güera")
    assert not es_pregunta("38000")


def test_el_orden_de_los_pasos_es_el_del_guion():
    base = {"sku": "MAIZ-BL", "producto": "Maíz blanco"}
    assert siguiente({}) == "grano"
    assert siguiente(base) == "volumen"
    assert siguiente({**base, "volumen": "completa", "toneladas": 40}) == "presentacion"
    assert siguiente({**base, "volumen": "chica"}) == "toneladas"
    trigo = {"sku": "TRIGO-CR", "producto": "Trigo cristalino"}
    assert siguiente(trigo) == "toneladas"
    assert siguiente({**trigo, "toneladas": 30}) == "entrega"


# ── El grano: la lista sale del catálogo ─────────────────────────────────── #


def test_cotizar_ofrece_los_granos_del_catalogo_como_lista(ventas):
    (reply,) = conversar(ventas, COTIZAR)

    assert reply.lista is not None
    # Solo lo que HAY: el trigo del mock viene en tránsito y no se anuncia.
    assert filas(reply) == [
        f"{PREFIJO_PRODUCTO}MAIZ-BL",
        f"{PREFIJO_PRODUCTO}MAIZ-AM",
        f"{PREFIJO_PRODUCTO}SORGO",
    ]
    detalles = {o.titulo: o.descripcion for o in reply.lista.opciones}
    assert detalles["Maíz blanco"] == "Disponible"
    # Sin precio no es "no lo vendemos": se dice que el precio lo da un asesor.
    assert "precio con asesor" in detalles["Sorgo dulce"]
    # El guion pide calificar antes de dar precio: la lista no lo trae.
    assert "$" not in "".join(o.descripcion for o in reply.lista.opciones)


def _todo_sobre_pedido(ventas) -> None:
    for p in ventas._crm._productos:
        p.disponibilidad = "sobre_pedido"


def test_el_menu_no_anuncia_lo_que_va_sobre_pedido(ventas):
    """Un grano sobre pedido no está en el menú, pero se sigue cotizando si el
    cliente lo escribe: no se esconde, solo no se anuncia."""
    ventas._crm._productos[1].disponibilidad = "sobre_pedido"

    menu, reply = conversar(ventas, COTIZAR, "maíz amarillo")

    assert f"{PREFIJO_PRODUCTO}MAIZ-AM" not in filas(menu)
    assert "¿Cuántas toneladas de *Maíz amarillo*" in reply.texto


def test_sin_nada_en_existencia_lo_dice_y_no_manda_una_lista_vacia(ventas):
    """Meta rechaza una lista sin filas: el mensaje entero no llegaría."""
    _todo_sobre_pedido(ventas)

    (reply,) = conversar(ventas, COTIZAR)

    assert reply.lista is None
    assert "sobre pedido" in reply.texto
    assert ids(reply) == [ASESOR, MENU]


def test_sin_existencia_la_lista_del_modelo_conserva_su_texto(ventas):
    _todo_sobre_pedido(ventas)

    reply = asyncio.run(ventas.cotizador.lista_de_granos("Manejamos maíz. ¿Cuál le interesa?"))

    assert reply.texto == "Manejamos maíz. ¿Cuál le interesa?"
    assert reply.lista is None
    assert ids(reply) == [ASESOR, MENU]


def test_un_grano_sin_precio_se_ofrece_con_asesor(ventas):
    _, reply = conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}SORGO")
    assert "Sí manejamos *Sorgo dulce*" in reply.texto
    assert ASESOR in ids(reply)


def test_el_grano_tambien_se_puede_escribir(ventas):
    _, reply = conversar(ventas, COTIZAR, "maíz amarillo")
    # No es maíz blanco: no hay botones de volumen del guion, se escribe el número.
    assert "¿Cuántas toneladas de *Maíz amarillo*" in reply.texto


def test_con_el_crm_caido_no_se_cotiza_y_se_ofrece_asesor(ventas):
    class CRMCaido(MockCRMClient):
        async def catalogo(self):
            raise CRMNoDisponible("connection refused")

    ventas._crm = CRMCaido()
    (reply,) = conversar(ventas, COTIZAR)
    assert "no puedo consultar los precios" in reply.texto
    assert ids(reply) == [ASESOR, MENU]


def test_con_precios_viejos_no_se_cotiza(ventas, monkeypatch):
    original = ventas._crm.catalogo

    async def viejo():
        catalogo = await original()
        return catalogo.model_copy(update={"desactualizado": True})

    monkeypatch.setattr(ventas._crm, "catalogo", viejo)
    (reply,) = conversar(ventas, COTIZAR)
    assert "desactualizados" in reply.texto
    assert reply.lista is None


# ── Maíz blanco de punta a punta, con botones ────────────────────────────── #


def test_maiz_blanco_unidad_completa_hasta_el_pdf(ventas):
    respuestas = conversar(
        ventas,
        COTIZAR,
        f"{PREFIJO_PRODUCTO}MAIZ-BL",
        COT_UNIDAD_COMPLETA,
        COT_COSTAL_50,
        COT_SIN_MARCA,
        UBICACION,
        "Tortillería La Güera",
        COT_GENERAR,
    )
    grano, volumen, presentacion, costal, entrega, nombre, resumen, final = respuestas

    assert ids(volumen) == [COT_UNIDAD_COMPLETA, COT_ENTREGA_CHICA, COT_OTRA_CANTIDAD]
    assert ids(presentacion) == [COT_COSTAL_25, COT_COSTAL_50]
    assert len(costal.botones) == 3
    assert entrega.pedir_ubicacion is True
    assert "¿A nombre de quién" in nombre.texto

    assert "Maíz blanco: 40 t" in resumen.texto
    assert "costal de 50 kg, sin marca" in resumen.texto
    assert "Bodega Norte" in resumen.texto
    assert "A nombre de: Tortillería La Güera" in resumen.texto
    assert "Precio LAB: $6,169.56 por tonelada" in resumen.texto
    assert ids(resumen) == [COT_GENERAR, COT_CAMBIAR, ASESOR]

    # Se registró por el mismo camino que la herramienta: CRM + PDF.
    enviado = ventas._crm.enviado[0]
    assert enviado["nombre_cliente"] == "Tortillería La Güera"
    assert ventas._crm.cotizaciones[0].cantidad_ton == 40
    assert enviado["presentacion"] == "costal de 50 kg, sin marca"
    assert "Bodega Norte" in enviado["notas"]
    assert ventas._wa.enviados and ventas._wa.enviados[0]["nombre"].endswith(".pdf")

    assert "Le acabo de enviar su cotización *COT-" in final.texto
    assert "precio LAB" in final.texto
    assert "flete no está incluido" in final.texto
    assert ids(final) == [COT_CERRAR, COT_OTRA_COTIZACION, MENU]


def test_entrega_chica_pregunta_la_ciudad_de_la_zona(ventas):
    respuestas = conversar(
        ventas,
        COTIZAR,
        f"{PREFIJO_PRODUCTO}MAIZ-BL",
        COT_ENTREGA_CHICA,
        "4",
        COT_COSTAL_25,
        COT_SIN_MARCA,
        COT_ZONA_CELAYA,
        "Juan Pérez",
    )
    toneladas, zona, resumen = respuestas[2], respuestas[5], respuestas[-1]

    assert "de 1 a 6" in toneladas.texto  # la pregunta tras tocar "Hasta 6 t"
    assert filas(zona)[: len(ZONAS_ENTREGA_CHICA)] == list(ZONAS_ENTREGA_CHICA)
    assert COT_ZONA_OTRA in filas(zona)
    assert "Maíz blanco: 4 t" in resumen.texto
    assert "Entrega en: Celaya" in resumen.texto


def test_entrega_chica_de_mas_de_6_toneladas_no_se_acepta(ventas):
    *_, reply = conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}MAIZ-BL", COT_ENTREGA_CHICA, "10")
    assert "hasta 6 t" in reply.texto
    assert COT_UNIDAD_COMPLETA in ids(reply)


def test_otra_ciudad_para_entrega_chica_se_va_con_asesor(ventas):
    *_, reply = conversar(
        ventas,
        COTIZAR,
        f"{PREFIJO_PRODUCTO}MAIZ-BL",
        COT_ENTREGA_CHICA,
        "3",
        COT_COSTAL_25,
        COT_SIN_MARCA,
        COT_ZONA_OTRA,
    )
    assert "solo llegan a Querétaro, Irapuato, Celaya y León" in reply.texto
    assert ids(reply) == [COT_UNIDAD_COMPLETA, ASESOR]


def test_entre_6_y_40_toneladas_es_con_asesor(ventas):
    *_, reply = conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}MAIZ-BL", COT_OTRA_CANTIDAD, "20")
    assert "Entre 6 y 40 t" in reply.texto
    assert ids(reply) == [ASESOR, COT_OTRA_CANTIDAD, MENU]


def test_otra_cantidad_que_cae_en_la_regla_sigue_el_flujo(ventas):
    *_, reply = conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}MAIZ-BL", COT_OTRA_CANTIDAD, "40")
    assert ids(reply) == [COT_COSTAL_25, COT_COSTAL_50]


# ── Otro grano: sin las reglas del maíz blanco ───────────────────────────── #


def test_trigo_no_pregunta_costal_y_acepta_codigo_postal(ventas):
    *_, entrega, nombre, resumen = conversar(
        ventas, COTIZAR, f"{PREFIJO_PRODUCTO}TRIGO-CR", "30", "38000", "Molinos del Sur"
    )
    assert entrega.pedir_ubicacion is True
    assert "¿A nombre de quién" in nombre.texto
    assert "Trigo cristalino: 30 t" in resumen.texto
    assert "C.P. 38000" in resumen.texto
    assert "costal" not in resumen.texto


def test_mas_de_40_toneladas_de_cualquier_grano_es_con_asesor(ventas):
    *_, reply = conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}TRIGO-CR", "80")
    assert "Más de 40 t" in reply.texto
    assert ASESOR in ids(reply)


# ── Fuera de guion: el modelo contesta, los botones del paso regresan ───── #


def test_una_duda_a_media_cotizacion_va_al_modelo(ventas):
    *_, duda = conversar(
        ventas, COTIZAR, f"{PREFIJO_PRODUCTO}TRIGO-CR", "¿cuánto cuesta el flete?"
    )
    assert duda is None


def test_la_respuesta_del_modelo_trae_los_botones_del_paso_pendiente(ventas):
    conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}MAIZ-BL")
    reply = asyncio.run(ventas.decorate(PHONE, "El flete se cotiza aparte, con su ubicación."))
    assert ids(reply) == [COT_UNIDAD_COMPLETA, COT_ENTREGA_CHICA, COT_OTRA_CANTIDAD]


def test_si_el_modelo_manda_con_el_asesor_el_boton_esta_aunque_haya_paso_pendiente(ventas):
    conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}MAIZ-BL")
    reply = asyncio.run(
        ventas.decorate(PHONE, "Déjeme validarlo: toque «👤 Asesor» abajo de este mensaje.")
    )
    assert ids(reply) == [MENU, ASESOR]


def test_despues_de_cotizar_el_modelo_ofrece_cerrar_otra_o_asesor(ventas):
    conversar(
        ventas, COTIZAR, f"{PREFIJO_PRODUCTO}TRIGO-CR", "30", "38000", "Molinos", COT_GENERAR
    )
    reply = asyncio.run(ventas.decorate(PHONE, "Con gusto. ¿Algo más sobre su cotización?"))
    assert ids(reply) == [COT_CERRAR, COT_OTRA_COTIZACION, ASESOR]


def test_tras_listar_productos_el_grano_se_elige_de_la_lista(ventas):
    from app.agents.base import Herramienta

    listado = asyncio.run(ventas.run_tool("listar_productos", {}, PHONE))
    reply = asyncio.run(
        ventas.decorate(
            PHONE,
            "Manejamos maíz blanco, maíz amarillo y trigo. ¿Cuál le interesa?",
            [Herramienta("listar_productos", {}, listado)],
        )
    )
    assert reply.lista is not None
    assert f"{PREFIJO_PRODUCTO}MAIZ-BL" in filas(reply)


def test_sin_cotizacion_en_curso_el_modelo_sigue_con_menu_y_asesor(ventas):
    reply = asyncio.run(ventas.decorate(PHONE, "Con gusto. ¿Para qué uso lo necesita?"))
    assert ids(reply) == [MENU, ASESOR]


def test_la_marca_de_producto_trae_la_lista_del_catalogo(ventas):
    reply = asyncio.run(
        ventas.decorate(PHONE, "¿Qué grano necesita?\n[[botones:producto]]")
    )
    assert reply.texto == "¿Qué grano necesita?"
    assert f"{PREFIJO_PRODUCTO}MAIZ-BL" in filas(reply)


def test_el_texto_libre_no_cuenta_si_ventas_no_es_el_agente_activo(ventas):
    conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}TRIGO-CR")
    (reply,) = conversar(ventas, "30", texto_libre=False)
    assert reply is None


def test_los_toques_de_otros_menus_no_son_de_la_cotizacion(ventas):
    conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}TRIGO-CR")
    assert conversar(ventas, "cli_saldo", "/menu", COT_CERRAR) == [None, None, None]


def test_un_toque_de_paso_sin_cotizacion_en_curso_lo_contesta_ventas(ventas):
    (reply,) = conversar(ventas, COT_COSTAL_25)
    assert reply is None


def test_los_pasos_quedan_en_el_historial_del_agente(ventas):
    conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}MAIZ-BL")
    historial = asyncio.run(ventas._history_store.load(f"{PHONE}:ventas"))
    assert {"role": "user", "content": "Quiero Maíz blanco."} in historial


# ── Cambiar antes de generar ─────────────────────────────────────────────── #


def test_cambiar_las_toneladas_regresa_al_resumen_con_lo_demas(ventas):
    respuestas = conversar(
        ventas,
        COTIZAR,
        f"{PREFIJO_PRODUCTO}TRIGO-CR",
        "30",
        "38000",
        "Molinos del Sur",
        COT_CAMBIAR,
        COT_CAMBIAR_TONELADAS,
        "25",
    )
    cambiar, resumen = respuestas[5], respuestas[-1]
    assert COT_CAMBIAR_TONELADAS in filas(cambiar)
    # Trigo no tiene presentación que cambiar.
    assert "cot_cambiar_pres" not in filas(cambiar)
    assert "Trigo cristalino: 25 t" in resumen.texto
    assert "Molinos del Sur" in resumen.texto


def test_el_cliente_identificado_no_repite_su_nombre(ventas):
    asyncio.run(
        SesionClienteStore(ventas._bus).abrir(
            PHONE, token="t", cliente="Molinos del Bajío", rfc="MBA950101AB1", ttl_segundos=600
        )
    )
    *_, resumen = conversar(ventas, COTIZAR, f"{PREFIJO_PRODUCTO}TRIGO-CR", "30", "38000")
    assert "A nombre de: Molinos del Bajío" in resumen.texto


# ── Por el router ────────────────────────────────────────────────────────── #


def test_el_router_manda_cotizar_al_flujo_guiado_y_deja_activo_a_ventas(ventas):
    router = Router(agents={"ventas": ventas}, bus=ventas._bus)

    async def escenario():
        reply = await router.route(PHONE, COTIZAR)
        activo = await ventas._bus.get_active_agent(PHONE)
        siguiente_paso = await router.route(PHONE, f"{PREFIJO_PRODUCTO}TRIGO-CR")
        toneladas = await router.route(PHONE, "30")
        return reply, activo, siguiente_paso, toneladas

    reply, activo, siguiente_paso, toneladas = asyncio.run(escenario())
    assert reply.lista is not None
    assert activo == "ventas"
    assert "¿Cuántas toneladas" in siguiente_paso.texto
    assert toneladas.pedir_ubicacion is True


# ── Los botones caben en Meta y tienen a dónde ir ────────────────────────── #


def test_los_botones_fijos_del_flujo_tienen_accion_y_caben():
    from app import cotizador

    fijos = [
        v for k, v in vars(cotizador).items() if k.startswith("BOTON_")
    ]
    for b in fijos:
        assert b.id == MENU or b.id in ACCIONES, b.id
        assert len(b.titulo) <= 20, b.titulo
    for o in CAMBIOS_COTIZACION:
        assert o.id in ACCIONES, o.id
        assert len(o.titulo) <= 24, o.titulo
    for boton_id in ZONAS_ENTREGA_CHICA:
        assert boton_id in ACCIONES
    # Cerrar es con una persona: va a Soporte, que es quien escala.
    assert ACCIONES[COT_CERRAR].agente == "soporte"
