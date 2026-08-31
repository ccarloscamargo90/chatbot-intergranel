"""El proveedor consulta lo suyo: cuánto se le debe y qué ya venció.

Lo que se prueba no es que conteste bonito, es el candado y la honestidad de la
respuesta: que sin identificarse no salga un peso, que un RFC ajeno no abra
nada, que una factura vencida se anuncie como vencida, y que NADA de lo que
sale mencione una marca propia.
"""

import asyncio
import json

import pytest

from app.agents.proveedores import TOOLS, TOOLS_CON_SESION, ProveedoresAgent
from app.bus import InMemoryEventBus
from app.erp import MockERPClient
from app.history import InMemoryHistoryStore
from app.menus import ACCIONES, PROV_PAGOS, PROV_SOY_PROVEEDOR, menu_proveedor
from app.sesiones import SesionProveedorStore

PHONE = "5215598765432"


@pytest.fixture
def agente():
    """El agente con sus mocks inyectados, sin red ni Claude."""
    a = ProveedoresAgent.__new__(ProveedoresAgent)
    bus = InMemoryEventBus()
    a._erp = MockERPClient()
    a._bus = bus
    a._history = InMemoryHistoryStore()
    a._sesiones = SesionProveedorStore(bus)
    return a


def _run(agent, name, payload=None, phone=PHONE):
    return json.loads(asyncio.run(agent.run_tool(name, payload or {}, phone)))


def _identificar(agent, nombre="Granos del Norte", rfc="GNO900215QT4"):
    return _run(agent, "identificar_proveedor", {"nombre": nombre, "rfc": rfc})


# ------------------------------- El candado -------------------------------- #
def test_sin_identificarse_no_sale_un_peso(agente):
    for tool in sorted(TOOLS_CON_SESION):
        data = _run(agente, tool)
        assert data["identificado"] is False, tool


def test_todas_las_tools_de_datos_estan_declaradas_con_sesion():
    """Si alguien agrega una y olvida la lista, queda abierta."""
    declaradas = {t["name"] for t in TOOLS}
    libres = declaradas - TOOLS_CON_SESION
    # Solo la identificación puede correrse sin sesión.
    assert libres == {"identificar_proveedor"}


def test_un_rfc_que_no_es_suyo_no_abre_nada(agente):
    data = _identificar(agente, rfc="XXX010101XX1")
    assert data["identificado"] is False
    assert data["motivo"] == "no_coincide"


def test_el_fallo_no_dice_cual_de_los_dos_datos_falló(agente):
    rfc_mal = _identificar(agente, rfc="XXX010101XX1")
    nombre_mal = _identificar(agente, nombre="Otra Empresa")
    # Misma respuesta: si dijera "el RFC sí existe", el bot sería un
    # verificador de RFCs.
    assert rfc_mal["motivo"] == nombre_mal["motivo"] == "no_coincide"
    assert "NO digas cuál de los dos falló" in nombre_mal["instruccion"]


def test_al_proveedor_extranjero_se_le_dice_la_verdad(agente):
    """Sin RFC mexicano no hay canal, y la instrucción prohíbe prometer otra vía."""
    data = _identificar(agente, rfc="XXX010101XX1")
    assert "no lo puedes atender" in data["instruccion"]
    assert "su comprador" in data["instruccion"]


def test_la_sesion_del_proveedor_no_sirve_como_cliente(agente):
    """Claves distintas en el bus: identificarse como proveedor no abre nada
    del lado del cliente, aunque sea el mismo teléfono."""
    _identificar(agente)
    from app.sesiones import SesionClienteStore

    cliente = SesionClienteStore(agente._bus)
    assert asyncio.run(cliente.leer(PHONE)) is None


# ------------------------------ "¿Ya me pagaron?" -------------------------- #
def test_el_resumen_dice_cuanto_y_cuanto_vencido(agente):
    _identificar(agente)
    data = _run(agente, "resumen_de_mi_cuenta_proveedor")

    r = data["resumen"]
    assert r["proveedor"] == "GRANOS DEL NORTE, S.A. DE C.V."
    # Dos pendientes: 185,000 vencida + 46,000 al corriente.
    assert r["facturas_pendientes"] == 2
    assert r["por_pagar"] == 231000.0
    assert r["vencido"] == 185000.0


def test_las_facturas_traen_vencida_masticada(agente):
    _identificar(agente)
    data = _run(agente, "listar_mis_facturas_proveedor")

    por_folio = {f["id"]: f for f in data["facturas"]}
    assert por_folio["FP-2026-0031"]["vencida"] is True
    assert por_folio["FP-2026-0048"]["vencida"] is False
    # La pagada no cuenta como vencida aunque su fecha ya pasó.
    assert por_folio["FP-2026-0050"]["vencida"] is False
    assert por_folio["FP-2026-0050"]["saldo"] == 0.0
    # El conteo va aparte para que el modelo no tenga que recorrer la lista.
    assert data["vencidas"] == 1


def test_las_ordenes_de_compra_se_listan(agente):
    _identificar(agente)
    data = _run(agente, "listar_mis_ordenes_proveedor")
    assert data["total"] == 2
    assert {o["id"] for o in data["ordenes"]} == {"OC-2026-0001", "OC-2026-0018"}


def test_cerrar_sesion_deja_de_mostrar_lo_suyo(agente):
    _identificar(agente)
    cerrada = _run(agente, "cerrar_sesion_proveedor")
    assert cerrada["sesion_cerrada"] is True
    assert _run(agente, "resumen_de_mi_cuenta_proveedor")["identificado"] is False


# --------------------------- Regla de confidencialidad --------------------- #
def test_nada_de_lo_que_sale_menciona_una_marca_propia(agente):
    """Un proveedor no debe saber bajo qué marca se revende lo que nos vende.

    Aplica a TODO lo que sale por este canal: las respuestas de las tools y el
    prompt con el que el modelo redacta.
    """
    _identificar(agente)
    salida = json.dumps(
        [
            _run(agente, "resumen_de_mi_cuenta_proveedor"),
            _run(agente, "listar_mis_facturas_proveedor"),
            _run(agente, "listar_mis_ordenes_proveedor"),
        ],
        ensure_ascii=False,
    )
    from app.agents.proveedores import SYSTEM_PROMPT

    for marca in ["GRANCORE", "Grancore", "grancore", "MegaCostales", "Ganaplus"]:
        assert marca not in salida
        assert marca not in SYSTEM_PROMPT


def test_el_prompt_prohibe_prometer_fecha_de_pago(agente):
    """Cuándo se paga lo decide una persona. Prometerlo por chat crea una deuda
    de palabra que nadie autorizó."""
    from app.agents.proveedores import SYSTEM_PROMPT

    assert "NUNCA prometas una fecha de pago" in SYSTEM_PROMPT


# ----------------------------------- Menú ---------------------------------- #
def test_el_menu_del_proveedor_respeta_los_topes_de_meta():
    for identificado in (True, False):
        menu = menu_proveedor(identificado)
        assert len(menu.opciones) <= 10
        for o in menu.opciones:
            assert len(o.titulo) <= 24


def test_cada_boton_del_menu_es_ruteable():
    """Un id sin destino es un botón que no hace nada al tocarlo.

    `cli_menu` no vive en ACCIONES: el router lo atiende antes, porque devuelve
    el menú en vez de despachar a un agente. Por eso la prueba pregunta si el
    router sabe qué hacer con el id, no si está en el diccionario.
    """
    from app.router import Router

    router = Router.__new__(Router)
    for identificado in (True, False):
        for o in menu_proveedor(identificado).opciones:
            assert router._parse_command(o.id) is not None, o.id


def test_los_botones_del_proveedor_van_a_su_agente():
    assert ACCIONES[PROV_SOY_PROVEEDOR].agente == "proveedores"
    assert ACCIONES[PROV_PAGOS].agente == "proveedores"


def test_hay_puerta_al_canal_de_proveedor_desde_el_menu_anonimo():
    """Sin ella, quien nos vende cae en el padrón de clientes y falla siempre."""
    from app.menus import menu_cliente

    ids = {o.id for o in menu_cliente(False).opciones}
    assert PROV_SOY_PROVEEDOR in ids
