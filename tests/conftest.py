"""Fixtures compartidas.

El agente de Soporte se arma con `__new__` para no tocar la red (regla 6): se
le inyectan a mano sus dependencias. Vive aquí y no en cada archivo de pruebas
porque ya son tres los que lo necesitan — así, cuando el agente gana una
dependencia, se actualiza en un solo lugar en vez de en tres.
"""

import pytest

from app.agents.soporte import SoporteAgent
from app.bus import InMemoryEventBus
from app.chatwoot import MockChatwootClient
from app.crm import MockCRMClient
from app.erp import MockERPClient
from app.handoff import HandoffStore
from app.history import InMemoryHistoryStore
from app.sesiones import SesionClienteStore


@pytest.fixture
def soporte() -> SoporteAgent:
    agente = SoporteAgent.__new__(SoporteAgent)
    agente._erp = MockERPClient()
    agente._bus = InMemoryEventBus()
    agente._history_store = InMemoryHistoryStore()
    agente._sesiones = SesionClienteStore(agente._bus)
    agente._chatwoot = MockChatwootClient()
    agente._handoff = HandoffStore(agente._bus, ttl_segundos=3600)
    # Al escalar, el resumen de lo que se habló también va al CRM: es lo que
    # lee el vendedor que abra la ficha del cliente después.
    agente._crm = MockCRMClient()
    return agente
