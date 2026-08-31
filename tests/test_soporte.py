"""Agente de Soporte: lo que atiende sin identificar y lo que ya no.

El detalle de la cuenta del cliente (saldo, contratos, pedidos, facturas) vive
en `test_clientes.py`. Aquí solo se fija el borde: qué se puede hacer sin
identificarse y qué dejó de poderse.
"""

import asyncio
import json

from app.agents.soporte import TOOLS, TOOLS_CON_SESION

PHONE = "5215512345678"


def _run(agent, name, payload=None):
    return json.loads(asyncio.run(agent.run_tool(name, payload or {}, PHONE)))


def test_escalar_a_humano(soporte):
    data = _run(soporte, "escalar_a_humano", {"motivo": "reclamo"})
    assert data["escalado"] is True


def test_herramienta_desconocida_no_truena(soporte):
    assert "error" in _run(soporte, "no_existe")


def test_consultar_orden_ya_no_atiende_a_cualquiera(soporte):
    """Antes bastaba con saber (o adivinar) el folio: `consultar_orden` iba
    contra el catálogo global y devolvía el contrato de quien fuera. Ahora
    exige sesión y busca solo entre los contratos del cliente identificado."""
    data = _run(soporte, "consultar_orden", {"order_id": "CONT-2026-0001"})
    assert data["identificado"] is False
    assert "orden" not in data


def test_toda_tool_con_sesion_esta_declarada(soporte):
    """Si alguien agrega una tool de datos y olvida meterla en
    TOOLS_CON_SESION, queda abierta. Este test es el recordatorio."""
    declaradas = {t["name"] for t in TOOLS}
    assert TOOLS_CON_SESION <= declaradas
    abiertas = declaradas - TOOLS_CON_SESION
    assert abiertas == {"identificar_cliente", "escalar_a_humano"}
