# Chatbot Multi-Agente WhatsApp · Intergranel

## Qué es este proyecto

Un chatbot de WhatsApp con **un solo número** y un **router central** que clasifica cada mensaje y lo despacha a uno de cuatro agentes especializados: Ventas, Compras, Inventario y Soporte. Los agentes se comunican entre sí a través de un bus de eventos compartido en Redis.

**Intergranel** es una comercializadora de granos y commodities a granel (maíz, sorgo, trigo, soya y derivados) para clientes industriales en México.

## Stack

- Python 3.11, FastAPI, uvicorn
- Claude API: Opus para agentes, Haiku para clasificación rápida del router
- WhatsApp Cloud API de Meta
- Redis: historial, deduplicación, bus de eventos
- ERP: NestJS + Prisma (repo separado), consultado vía REST con header `X-Bot-Api-Key`
- Deploy: Railway
- Lint: ruff (line-length=100, selects E,F,I,UP,B)
- Tests: pytest (sin credenciales, usan mock ERP y WhatsApp modo dev)

## Estructura de archivos

```
app/
  main.py              ← FastAPI, webhooks, _process_message → router.route()
  router.py            ← Clasifica intención → despacha al agente
  bus.py               ← Bus de eventos compartido (Redis / InMemory)
  agents/
    base.py            ← BaseAgent: loop agéntico (Claude + tools + historial)
    ventas.py           ← Funcional, tools con precios mock
    compras.py          ← Stub (tools devuelven "próximamente")
    inventario.py       ← Funcional con stock mock
    soporte.py          ← Migrado del asistente original
  assistant.py          ← Wrapper compat para tests legacy
  config.py, erp.py, history.py, dedup.py, whatsapp.py, notifications.py, models.py
tests/
  test_api.py, test_assistant.py, test_bus.py, test_router.py,
  test_ventas.py, test_erp.py, test_history.py, test_dedup.py,
  test_media.py, test_signature.py
docs/erp/               ← Implementación de referencia NestJS para el módulo bot del ERP
```

## Arquitectura del router

```
Mensaje de WhatsApp
    │
    ▼
Router.route(phone, content)
    │
    ├─ 1. Comando explícito? (/ventas, /menu, /soporte, /compras, /inventario)
    │     → Enrutar directamente al agente nombrado
    │
    ├─ 2. Sesión activa en bus? (bus:session:{phone}:agente, TTL 30min)
    │     → Mismo agente que el turno anterior (continuidad)
    │
    └─ 3. Clasificar intención con Claude Haiku (max_tokens=20)
          → Una palabra: ventas|compras|inventario|soporte
          → Fallback: soporte
```

## Interfaz de BaseAgent (app/agents/base.py)

Cada agente hereda de `BaseAgent` y define:

- `name: str` — nombre único ("ventas", "compras", etc.)
- `system_prompt() -> str` — prompt de sistema especializado
- `tools() -> list[dict]` — definición de herramientas para la API de Claude
- `run_tool(name, tool_input, caller_phone) -> str` — ejecuta la tool y devuelve JSON

`BaseAgent.handle(phone, content, store_text)` implementa el loop agéntico completo:
cargar historial → llamar a Claude → si tool_use, ejecutar tools y continuar → persistir historial.
MAX_HISTORY = 24 mensajes.

## Bus de eventos (app/bus.py)

```python
await self._bus.publish("bus:ventas:cotizacion:5215512345678", data, ttl=86400)
data = await self._bus.read("bus:ventas:cotizacion:5215512345678")
eventos = await self._bus.read_prefix("bus:inventario:alerta:")
await self._bus.set_active_agent(phone, "ventas")       # TTL 30min
agent = await self._bus.get_active_agent(phone)          # -> "ventas" | None
```

Convención de claves: `bus:{agente}:{tipo}:{identificador}`.
Implementaciones: `InMemoryEventBus` (dev) y `RedisEventBus` (prod).

## Transferencias entre agentes

Cada agente puede tener una tool `transferir_a_{otro_agente}` que cambia el agente activo en el bus. El siguiente mensaje del usuario llega automáticamente al nuevo agente.

## Reglas de desarrollo

1. **Leer antes de modificar.** Antes de tocar un archivo, leer su contenido actual completo.
2. **Tests siempre en verde.** Si se cambia una interfaz, actualizar los tests. Cada agente nuevo o tool nueva necesita tests en `tests/test_{agente}.py`.
3. **Lint limpio.** Correr `ruff check app/ tests/` antes de commitear.
4. **Tools devuelven JSON.** Cada `run_tool` devuelve un string JSON. Siempre envolver en try/except con `# noqa: BLE001`. Usar `json.dumps(data, ensure_ascii=False)` para español.
5. **ERPClient extensible.** Si se agrega un método, añadir: abstracto en `ERPClient`, implementación HTTP en `HTTPERPClient`, implementación mock en `MockERPClient`.
6. **Tests sin red.** Nunca llaman a Claude ni a servicios externos. Construir agentes con `AgentClass.__new__(AgentClass)` y ejercitar `run_tool` contra `MockERPClient`. Para integración, monkeypatchear `router.route` y `wa.send_text`.
7. **Bus para comunicación.** Eventos entre agentes van al bus con la convención `bus:{agente}:{tipo}:{id}`.
8. **Historial limpio.** Se serializa como dicts JSON (no Pydantic). Para multimedia, guardar solo placeholder de texto.

## Verificación rápida

```bash
ruff check app/ tests/     # 0 errores
pytest -q                  # 64 tests pasando
```

## Estado actual y fases

### Fase 1 ✅ — Router + refactorización
Completada. Router, bus, BaseAgent, 4 agentes (Soporte y Ventas funcionales, Compras e Inventario stubs).

### Fase 2 — Agente de Ventas → ERP real
Reemplazar PRECIOS_MOCK en agents/ventas.py por llamadas HTTP al ERP.
Endpoints a implementar en el ERP (NestJS):
- `GET /api/v1/bot/precios/:producto` → `{ producto, precio_ton, moneda, disponible_ton, vigencia }`
- `GET /api/v1/bot/precios` → lista de precios vigentes
- `POST /api/v1/bot/cotizaciones` → `{ id, producto, cantidad, total, vigencia, estado }`
- `POST /api/v1/bot/solicitudes` → `{ id, estado: "pendiente" }`
Extender ERPClient con: get_price(), list_prices(), create_quote(), create_request().

### Fase 3 — Agente de Compras completo
Implementar tools reales: consultar_oc, listar_oc_pendientes, crear_oc, aprobar_oc, listar_proveedores.
Agregar lista blanca de teléfonos autorizados (config: COMPRAS_PHONES_ALLOWED).
Endpoints ERP: GET/POST /api/v1/bot/oc/, PATCH /api/v1/bot/oc/:folio/aprobar, GET /api/v1/bot/proveedores.

### Fase 4 — Inventario + alertas proactivas
Conectar agents/inventario.py al ERP real (GET /api/v1/bot/inventario/).
Nuevo webhook: POST /webhooks/erp/inventory-alert.
Notificaciones proactivas al equipo cuando un producto cae bajo umbral.

## Especificación de agentes

### Ventas (agents/ventas.py) — Funcional con mock

| Tool | Params requeridos | Qué hace |
|---|---|---|
| consultar_precio | producto | Precio/ton, disponibilidad, vigencia |
| generar_cotizacion | producto, cantidad_ton | Cotización con total. Publica en bus |
| consultar_contrato | folio | Estado de contrato (usa ERP) |
| listar_contratos_cliente | — | Contratos del remitente (usa ERP) |
| solicitar_pedido | producto, cantidad_ton | Registra solicitud. Publica en bus |
| transferir_a_soporte | motivo | Cambia agente activo en bus |

Precios mock: maíz amarillo $5,200/ton, maíz blanco $5,450, trigo $7,100, sorgo $4,800, soya $11,500.

### Soporte (agents/soporte.py) — Funcional

| Tool | Params requeridos | Qué hace |
|---|---|---|
| consultar_orden | order_id | Estado y detalles por folio |
| listar_ordenes_cliente | — | Órdenes del remitente |
| escalar_a_humano | motivo | Log + mensaje de escalamiento |

### Compras (agents/compras.py) — Stub (Fase 3)

Tools actuales devuelven "próximamente". transferir_a_ventas funciona.
Planificadas: consultar_oc, listar_oc_pendientes, crear_oc, aprobar_oc, listar_proveedores.
Requiere lista blanca de teléfonos.

### Inventario (agents/inventario.py) — Funcional con mock

| Tool | Params requeridos | Qué hace |
|---|---|---|
| consultar_stock | producto | Stock, umbral, ubicación, estado |
| listar_alertas_inventario | — | Productos bajo umbral |
| resumen_inventario | — | Todos los productos |
| transferir_a_ventas | motivo | Cambia agente activo |

Stock mock: trigo cristalino (200 ton, umbral 250 → bajo_umbral), soya (150 ton, umbral 200 → bajo_umbral), resto normal.

## Datos del ERP mock

Contratos en MockERPClient (teléfono 5215512345678):
- CONT-2026-0001: Molinos del Bajío, maíz amarillo 50ton, $185,000, EN_PROCESO / EN_TRANSITO
- CONT-2026-0002: Molinos del Bajío, trigo cristalino 30ton, $92,000, ACTIVO

## Variables de entorno

```
ANTHROPIC_API_KEY, CLAUDE_MODEL (default: claude-opus-4-8)
WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET
ERP_BASE_URL (vacío = mock), ERP_API_KEY, ERP_API_KEY_HEADER (default: X-Bot-Api-Key)
ERP_WEBHOOK_SECRET
REDIS_URL (vacío = memoria), HISTORY_TTL_SECONDS (7d), DEDUP_TTL_SECONDS (1d)
```
