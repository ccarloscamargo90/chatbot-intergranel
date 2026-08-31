"""Autoservicio del cliente: identificación por nombre + RFC y consultas.

Sin red ni Claude: el agente lo arma la fixture `soporte` de `conftest.py`, con
el ERP y Chatwoot simulados sobre un bus en memoria.
"""

import asyncio
import json

import pytest

from app.erp import SesionClienteInvalida
from app.menus import ASESOR, MENU
from app.sesiones import MAX_INTENTOS_LOCALES

PHONE = "5215512345678"
OTRO_PHONE = "5215599999999"
NOMBRE = "Molinos del Bajío"
RFC = "MBA950101AB1"


def _run(agent, name, payload=None, phone=PHONE):
    return json.loads(asyncio.run(agent.run_tool(name, payload or {}, phone)))


def _identificar(agent, nombre=NOMBRE, rfc=RFC, phone=PHONE):
    return _run(agent, "identificar_cliente", {"nombre": nombre, "rfc": rfc}, phone)


# ------------------------------ Identificación ---------------------------- #
def test_identificar_con_nombre_y_rfc_correctos(soporte):
    data = _identificar(soporte)
    assert data["identificado"] is True
    assert data["cliente"] == "Molinos del Bajío S.A. de C.V."
    assert data["minutos_de_sesion"] > 0


def test_identificar_acepta_el_nombre_sin_acentos_ni_mayusculas(soporte):
    assert _identificar(soporte, nombre="molinos del bajio", rfc="mba950101ab1")[
        "identificado"
    ]


def test_rfc_correcto_con_nombre_ajeno_no_identifica(soporte):
    """El RFC solo no basta: viene impreso en cada factura que emite el cliente."""
    data = _identificar(soporte, nombre="Harinera del Norte")
    assert data["identificado"] is False
    assert data["motivo"] == "no_coincide"


def test_nombre_correcto_con_rfc_ajeno_no_identifica(soporte):
    data = _identificar(soporte, rfc="XAXX010101000")
    assert data["identificado"] is False


def test_el_fallo_no_delata_si_el_rfc_existe(soporte):
    """El motivo es el MISMO falle lo que falle: si distinguiera, el bot sería
    un verificador de RFCs."""
    solo_rfc_malo = _identificar(soporte, rfc="AAA010101AAA")
    solo_nombre_malo = _identificar(soporte, nombre="Otra Empresa")
    assert solo_rfc_malo["motivo"] == solo_nombre_malo["motivo"] == "no_coincide"


def test_faltan_datos_no_llama_al_erp(soporte):
    data = _run(soporte, "identificar_cliente", {"nombre": "Molinos", "rfc": "  "})
    assert data["identificado"] is False
    assert data["motivo"] == "faltan_datos"


def test_intentos_fallidos_terminan_en_bloqueo(soporte):
    for _ in range(MAX_INTENTOS_LOCALES):
        assert _identificar(soporte, nombre="Quien Sea")["identificado"] is False
    bloqueado = _identificar(soporte, nombre="Quien Sea")
    assert bloqueado["motivo"] == "bloqueado"
    assert bloqueado["espera_minutos"] > 0


def test_el_bloqueo_es_por_telefono(soporte):
    for _ in range(MAX_INTENTOS_LOCALES + 1):
        _identificar(soporte, nombre="Quien Sea")
    # Otro teléfono no arrastra el bloqueo del primero.
    assert _identificar(soporte, phone=OTRO_PHONE)["identificado"] is True


def test_identificarse_bien_limpia_los_intentos(soporte):
    _identificar(soporte, nombre="Quien Sea")
    _identificar(soporte, nombre="Quien Sea")
    assert _identificar(soporte)["identificado"] is True
    assert asyncio.run(soporte._sesiones.intentos(PHONE)) == 0


# --------------------------- Consultas con sesión -------------------------- #
@pytest.mark.parametrize(
    "tool",
    [
        "resumen_de_mi_cuenta",
        "consultar_mi_saldo",
        "listar_mis_contratos",
        "listar_mis_pedidos",
        "listar_mis_facturas",
        "consultar_orden",
        "cerrar_sesion",
    ],
)
def test_sin_identificarse_no_se_entrega_nada(soporte, tool):
    data = _run(soporte, tool, {"order_id": "CONT-2026-0001"})
    assert data["identificado"] is False
    assert "instruccion" in data


def test_saldo_trae_totales_y_renglones(soporte):
    _identificar(soporte)
    data = _run(soporte, "consultar_mi_saldo")
    cuenta = data["estado_de_cuenta"]
    assert cuenta["saldo"] == 204500.0
    assert cuenta["saldo_vencido"] == 112500.0
    assert len(cuenta["lineas"]) == 3
    # El vencido se puede señalar renglón por renglón.
    assert any(line["vencida"] and line["dias_vencido"] for line in cuenta["lineas"])


def test_resumen_de_cuenta(soporte):
    _identificar(soporte)
    resumen = _run(soporte, "resumen_de_mi_cuenta")["resumen"]
    assert resumen["contratos_activos"] == 2
    assert resumen["facturas_pendientes"] == 2
    assert resumen["saldo_vencido"] == 112500.0


def test_listados_del_cliente(soporte):
    _identificar(soporte)
    assert _run(soporte, "listar_mis_contratos")["total"] == 2
    assert _run(soporte, "listar_mis_pedidos")["total"] == 2
    assert _run(soporte, "listar_mis_facturas")["total"] == 2


def test_consultar_orden_propia(soporte):
    _identificar(soporte)
    data = _run(soporte, "consultar_orden", {"order_id": "cont-2026-0001"})
    assert data["encontrada"] is True
    assert data["orden"]["id"] == "CONT-2026-0001"


def test_consultar_orden_ajena_no_aparece(soporte):
    """Un folio que no es del cliente se busca ENTRE LOS SUYOS, así que no hay
    forma de leer el contrato de otro adivinando folios."""
    _identificar(soporte)
    data = _run(soporte, "consultar_orden", {"order_id": "CONT-2026-9999"})
    assert data["encontrada"] is False


# ------------------------------ Ciclo de sesión ---------------------------- #
def test_cerrar_sesion_deja_de_entregar_datos(soporte):
    _identificar(soporte)
    assert _run(soporte, "cerrar_sesion")["sesion_cerrada"] is True
    assert _run(soporte, "consultar_mi_saldo")["identificado"] is False


def test_sesion_muerta_en_el_erp_se_limpia_aqui(soporte):
    """Si el ERP tira el token antes de que caduque la copia local, el agente
    borra la suya en vez de seguir mandando uno muerto cada turno."""
    _identificar(soporte)

    async def _muerta(_token):
        raise SesionClienteInvalida("token revocado")

    soporte._erp.get_customer_debt = _muerta
    data = _run(soporte, "consultar_mi_saldo")
    assert data["sesion_expirada"] is True
    assert asyncio.run(soporte._sesiones.leer(PHONE)) is None


def test_la_sesion_no_se_comparte_entre_telefonos(soporte):
    _identificar(soporte)
    assert _run(soporte, "consultar_mi_saldo", phone=OTRO_PHONE)["identificado"] is False


def test_escalar_a_humano_no_exige_identificarse(soporte):
    assert _run(soporte, "escalar_a_humano", {"motivo": "reclamo"})["escalado"] is True


# --------------------------------- Botones -------------------------------- #
def test_recien_identificado_se_manda_el_menu_completo(soporte):
    _identificar(soporte)
    reply = asyncio.run(soporte.decorate(PHONE, "Listo."))
    assert reply.lista is not None
    ids = {o.id for o in reply.lista.opciones}
    assert {"cli_saldo", "cli_pedidos", "cli_facturas"} <= ids


def test_despues_van_los_dos_botones_de_seguimiento(soporte):
    reply = asyncio.run(soporte.decorate(PHONE, "Aquí tiene."))
    assert reply.lista is None
    assert [b.id for b in reply.botones] == [MENU, ASESOR]
