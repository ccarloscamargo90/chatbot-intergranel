"""Pruebas de la ejecución de herramientas y el manejo del historial.

No invocan a Claude: construimos el asistente sin __init__ y ejercitamos
directamente la lógica de herramientas contra el ERP simulado.
"""

import asyncio
import json

import pytest

from app.assistant import Assistant
from app.erp import MockERPClient


@pytest.fixture
def assistant() -> Assistant:
    a = Assistant.__new__(Assistant)
    a._erp = MockERPClient()
    return a


def test_consultar_orden_existente(assistant):
    raw = asyncio.run(
        assistant._run_tool(
            "consultar_orden", {"order_id": "CONT-2026-0001"}, "5215512345678"
        )
    )
    data = json.loads(raw)
    assert data["encontrada"] is True
    assert data["orden"]["id"] == "CONT-2026-0001"


def test_consultar_orden_inexistente(assistant):
    raw = asyncio.run(
        assistant._run_tool(
            "consultar_orden", {"order_id": "CONT-9999"}, "5215512345678"
        )
    )
    data = json.loads(raw)
    assert data["encontrada"] is False


def test_listar_usa_telefono_del_remitente(assistant):
    raw = asyncio.run(
        assistant._run_tool("listar_ordenes_cliente", {}, "5215512345678")
    )
    data = json.loads(raw)
    assert data["telefono"] == "5215512345678"
    assert data["total"] == 2


def test_escalar_a_humano(assistant):
    raw = asyncio.run(
        assistant._run_tool("escalar_a_humano", {"motivo": "reclamo"}, "5215512345678")
    )
    data = json.loads(raw)
    assert data["escalado"] is True


def test_trim_empieza_en_turno_de_usuario_limpio(assistant):
    history = [
        {"role": "assistant", "content": "x"},
        {"role": "user", "content": [{"type": "tool_result"}]},
        {"role": "user", "content": "hola"},
    ]
    trimmed = assistant._trim(history)
    assert trimmed[0]["role"] == "user"
    assert isinstance(trimmed[0]["content"], str)
