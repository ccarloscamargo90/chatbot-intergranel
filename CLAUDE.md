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
  router.py            ← Clasifica intención (o lee un botón) → despacha al agente
  bus.py               ← Bus de eventos compartido (Redis / InMemory)
  replies.py           ← Reply/Boton/MenuLista: texto + botones, con los topes de Meta
  menus.py             ← Menú del autoservicio del cliente e ids `cli_*` → acción
  sesiones.py          ← Sesión del cliente identificado, sobre el bus
  agents/
    base.py            ← BaseAgent: loop agéntico (Claude + tools + historial)
    ventas.py           ← Funcional, precios/cotizaciones vía ERP
    compras.py          ← Funcional vía ERP, con lista blanca de teléfonos
    inventario.py       ← Funcional vía ERP, con alertas proactivas
    soporte.py          ← Atención al cliente + autoservicio (identificación por RFC)
  assistant.py          ← Wrapper compat para tests legacy
  config.py, erp.py, history.py, dedup.py, whatsapp.py, notifications.py, models.py
tests/
  test_api.py, test_assistant.py, test_bus.py, test_router.py,
  test_ventas.py, test_erp.py, test_history.py, test_dedup.py,
  test_media.py, test_signature.py, test_soporte.py, test_compras.py,
  test_inventario.py, test_avisos.py, test_clientes.py, test_botones.py
docs/erp/               ← Implementación de referencia NestJS, contrato de avisos
                          (AVISOS_WHATSAPP.md) y de autoservicio del cliente
                          (AUTOSERVICIO_CLIENTES.md)
```

## Arquitectura del router

```
Mensaje de WhatsApp
    │
    ▼
Router.route(phone, content) -> Reply
    │
    ├─ 1. Comando explícito?
    │     · /ventas, /menu, /soporte, /compras, /inventario
    │     · id de un botón del menú (cli_saldo, cli_pedidos, …)
    │     → Enrutar directamente al agente que le toca
    │
    ├─ 2. Sesión activa en bus? (bus:session:{phone}:agente, TTL 30min)
    │     → Mismo agente que el turno anterior (continuidad)
    │
    └─ 3. Clasificar intención con Claude Haiku (max_tokens=20)
          → Una palabra: ventas|compras|inventario|soporte
          → Fallback: soporte
```

Un toque de botón NO se clasifica: ya es una intención exacta. Preguntarle a un
modelo qué quiso decir quien ya nos lo dijo sería gastar tiempo para perder
precisión.

## Interfaz de BaseAgent (app/agents/base.py)

Cada agente hereda de `BaseAgent` y define:

- `name: str` — nombre único ("ventas", "compras", etc.)
- `system_prompt() -> str` — prompt de sistema especializado
- `tools() -> list[dict]` — definición de herramientas para la API de Claude
- `run_tool(name, tool_input, caller_phone) -> str` — ejecuta la tool y devuelve JSON

`BaseAgent.handle(phone, content, store_text)` implementa el loop agéntico completo:
cargar historial → llamar a Claude → si tool_use, ejecutar tools y continuar → persistir historial.
MAX_HISTORY = 24 mensajes. Devuelve un `Reply`, no un string.

`decorate(phone, texto) -> Reply` es el gancho para colgarle botones a la
respuesta. Por defecto devuelve texto pelón; Soporte lo sobrescribe para mandar
el menú o los botones de seguimiento según el estado de la conversación.

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

## Botones (app/replies.py, app/menus.py)

Un agente devuelve `Reply`: texto y, opcionalmente, botones o un menú de lista.
`WhatsAppClient.send_reply` decide el tipo de mensaje; si el interactivo falla,
cae a texto (perder los botones es un mal menor, perder la respuesta no).

```python
Reply("Su saldo es $204,500.00 MXN", botones=[Boton("cli_menu", "📋 Menú")])
Reply("¿Qué desea consultar?", lista=menu_cliente(identificado=True))
```

Los topes de Meta son duros: 3 botones (título 20 chars) o 10 filas de lista
(título 24). Pasarse hace que Meta rechace el **mensaje entero**, así que
`replies.py` recorta al construir, no al enviar.

El ciclo completo: cada opción tiene un id `cli_*` y una frase canónica en
`menus.py`. Cuando el cliente toca, `main.py` lee el **id** del webhook (no el
título: el título es texto de pantalla y cambia al reescribir un menú) y el
router lo trata como comando explícito, despachando al agente con la frase. El
agente nunca se entera de que hubo un botón.

Al agregar una opción al menú: id nuevo en `menus.py`, entrada en `ACCIONES`, y
listo. `test_botones.py` falla si un id del menú se queda sin acción.

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
9. **Los datos del cliente van tras la sesión.** Toda tool que devuelva algo de
   la cuenta de un cliente (pedidos, contratos, facturas, saldo) se declara en
   `TOOLS_CON_SESION` de `soporte.py`. Si se agrega una y se olvida, queda
   abierta; `test_soporte.py` lo detecta comparando la lista contra las tools
   declaradas. Nunca buscar por folio global: buscar **entre los del cliente**,
   para que adivinar un folio no sirva de nada.

## Verificación rápida

```bash
ruff check app/ tests/     # 0 errores
pytest -q                  # 186 tests pasando
```

## Estado actual y fases

### Fase 1 ✅ — Router + refactorización
Completada. Router, bus, BaseAgent, 4 agentes (Soporte y Ventas funcionales, Compras e Inventario stubs).

### Fase 2 ✅ — Agente de Ventas → ERP real
Completada. `agents/ventas.py` ya no tiene precios hardcodeados: consulta el ERP
vía `ERPClient` (HTTP si hay `ERP_BASE_URL`, mock en desarrollo). Se extendió
`ERPClient` con `get_price()`, `list_prices()`, `create_quote()`,
`create_request()` (abstracto + `HTTPERPClient` + `MockERPClient`) y se
añadieron los modelos `Price`, `Quote`, `PurchaseRequest`.
Endpoints que el ERP (NestJS) debe exponer:
- `GET /api/v1/bot/precios/:producto` → `{ producto, precio_ton, moneda, disponible_ton, vigencia }`
- `GET /api/v1/bot/precios` → lista de precios vigentes
- `POST /api/v1/bot/cotizaciones` (body `{ producto, cantidad, telefono }`) → `{ id, producto, cantidad, total, vigencia, estado }`
- `POST /api/v1/bot/solicitudes` (body `{ producto, cantidad, telefono }`) → `{ id, estado: "pendiente" }`

### Fase 3 ✅ — Agente de Compras completo
Completada. `agents/compras.py` implementa tools reales contra el ERP
(consultar_oc, listar_oc_pendientes, crear_oc, aprobar_oc, listar_proveedores)
con lista blanca de teléfonos (`COMPRAS_PHONES_ALLOWED`; vacía = sin
restricción en desarrollo). `transferir_a_ventas` no requiere autorización.
Se extendió `ERPClient` con `get_purchase_order`, `list_pending_purchase_orders`,
`create_purchase_order`, `approve_purchase_order`, `list_suppliers` y se
añadieron los modelos `PurchaseOrder` y `Supplier`.
Endpoints que el ERP (NestJS) debe exponer:
- `GET /api/v1/bot/oc/:folio` → PurchaseOrder | 404
- `GET /api/v1/bot/oc?estado=pendiente` → lista de OC pendientes
- `POST /api/v1/bot/oc` (body `{ proveedor, producto, cantidad }`) → PurchaseOrder
- `PATCH /api/v1/bot/oc/:folio/aprobar` → PurchaseOrder (estado aprobada)
- `GET /api/v1/bot/proveedores` → lista de Supplier

### Fase 4 ✅ — Inventario + alertas proactivas
Completada. `agents/inventario.py` consulta el ERP vía `ERPClient`
(`get_inventory_item`, `list_inventory`; HTTP si hay `ERP_BASE_URL`, mock en
desarrollo) y se añadieron los modelos `InventoryItem` e `InventoryAlertEvent`.
Nuevo webhook `POST /webhooks/erp/inventory-alert` (protegido por
`ERP_WEBHOOK_SECRET`): publica la alerta en el bus
(`bus:inventario:alerta:{producto}`) y notifica al equipo por WhatsApp
(`notify_inventory_alert`, destinatarios en `INVENTORY_ALERT_PHONES`; vacío =
solo log + bus).
Endpoints que el ERP (NestJS) debe exponer:
- `GET /api/v1/bot/inventario/:producto` → InventoryItem | 404
- `GET /api/v1/bot/inventario` → lista de InventoryItem

### Fase 5 ✅ — Avisos internos del ERP al equipo
Completada. El ERP avisa por WhatsApp lo importante —lo que vence hoy, lo
vencido, un pago estancado— a la persona a la que le toca, según el calendario y
su rol. Nuevo webhook `POST /webhooks/erp/notificacion` (protegido por
`ERP_WEBHOOK_SECRET`): deduplica por `id` (el worker del ERP reintenta y nadie
puede recibir el mismo vencimiento dos veces), publica en el bus
(`bus:erp:aviso:{id}` y `bus:erp:aviso_reciente:{telefono}`) para que el agente
tenga contexto si la persona responde, y envía con `notify_erp_aviso`.

Fuera de la ventana de 24h de Meta el texto libre se rechaza, así que en
producción se usa una plantilla aprobada (`WHATSAPP_AVISO_TEMPLATE`, categoría
Utility, parámetros: título · detalle+liga · empresa). Sin ella se cae a texto
libre, que sirve en desarrollo.

Quién decide qué sale y a quién vive del lado del ERP (catálogo de reglas por
rol + preferencias por usuario). El contrato completo del webhook y la plantilla
está en `docs/erp/AVISOS_WHATSAPP.md`.

Respuestas del webhook:
- `{"status": "sent", "wamid": "…"}` → el ERP marca ENVIADO y guarda el wamid
- `{"status": "duplicate"}` → el ERP lo trata como entrega buena, no reintenta
- `401` → secreto inválido; el ERP reintenta con backoff y marca FALLIDO

### Fase 6 ✅ — Autoservicio del cliente (identificación + botones)
Completada. El cliente consulta **lo suyo** —pedidos, contratos, facturas,
saldo y lo vencido— identificándose con su nombre (o el de su empresa) y su RFC.
El ERP valida el par, abre una sesión con caducidad y devuelve un token; el bot
consulta con ese token en `X-Bot-Sesion` y **nunca** con un id de cliente.

Qué tan fuerte es eso, dicho sin adornos: el RFC de una empresa va impreso en
cada factura que emite, así que identifica pero no es un secreto. Lo que
sostiene el candado son cuatro cosas juntas: hay que acertar los DOS datos; los
intentos se cuentan por teléfono y se bloquean; todo intento queda en la
bitácora del ERP; y el fallo nunca dice cuál de los dos datos falló ni si el RFC
existe — si lo dijera, el bot sería un verificador de RFCs. Los RFC genéricos
del SAT (XAXX/XEXX) se rechazan: se repiten entre clientes.

**Cambio de comportamiento:** `listar_ordenes_cliente` (que listaba los
contratos de quien escribiera, sin autenticar) desapareció, y `consultar_orden`
ya no busca por folio global sino entre los contratos del cliente identificado.
Antes bastaba saber —o adivinar— un folio para leer el contrato de otro.

Se extendió `ERPClient` con `identify_customer`, `get_customer_summary`,
`get_customer_debt`, `list_customer_contracts`, `list_customer_orders`,
`list_customer_invoices` y `close_customer_session` (abstracto + `HTTPERPClient`
+ `MockERPClient`), y se añadieron los modelos `CustomerIdentification`,
`CustomerDebt`, `CustomerDebtLine`, `CustomerOrder`, `CustomerInvoice` y
`CustomerSummary`. Un 401 del ERP se traduce a `SesionClienteInvalida`, que el
agente cuenta como "su sesión caducó", no como una falla técnica.

Toda la conversación se maneja con **botones** (ver la sección de arriba): menú
de lista al identificarse, y `[📋 Menú] [👤 Asesor]` pegados a cada respuesta.

Endpoints que el ERP expone (ya implementados en `ERP-INTERGRANEL`):
- `POST /api/v1/bot/clientes/identificar` (body `{ nombre, rfc, telefono }`) → `Identificacion`
- `GET /api/v1/bot/clientes/resumen` → `CustomerSummary`
- `GET /api/v1/bot/clientes/deuda` → `CustomerDebt`
- `GET /api/v1/bot/clientes/contratos` → `Order[]`
- `GET /api/v1/bot/clientes/pedidos` → `CustomerOrder[]`
- `GET /api/v1/bot/clientes/facturas` → `CustomerInvoice[]`
- `POST /api/v1/bot/clientes/cerrar-sesion`

Contrato completo (incluida la nota honesta sobre la fuerza de la
identificación) en `docs/erp/AUTOSERVICIO_CLIENTES.md`.

## Especificación de agentes

### Ventas (agents/ventas.py) — Funcional vía ERP (mock o HTTP)

| Tool | Params requeridos | Qué hace |
|---|---|---|
| consultar_precio | producto | Precio/ton, disponibilidad, vigencia (ERP `get_price`) |
| generar_cotizacion | producto, cantidad_ton | Cotización con total (ERP `create_quote`). Publica en bus |
| consultar_contrato | folio | Estado de contrato (usa ERP) |
| listar_contratos_cliente | — | Contratos del remitente (usa ERP) |
| solicitar_pedido | producto, cantidad_ton | Registra solicitud (ERP `create_request`). Publica en bus |
| transferir_a_soporte | motivo | Cambia agente activo en bus |

Precios del mock (`MockERPClient`): maíz amarillo $5,200/ton, maíz blanco $5,450, trigo $7,100, sorgo $4,800, soya $11,500.

### Soporte (agents/soporte.py) — Atención al cliente + autoservicio

| Tool | Params requeridos | Sesión | Qué hace |
|---|---|---|---|
| identificar_cliente | nombre, rfc | — | Valida el par contra el ERP y abre sesión |
| resumen_de_mi_cuenta | — | ✔ | Contratos, pedidos, facturas pendientes y saldo |
| consultar_mi_saldo | — | ✔ | Estado de cuenta: total, vencido y renglones |
| listar_mis_contratos | — | ✔ | Contratos del cliente identificado |
| listar_mis_pedidos | — | ✔ | Pedidos con estado y fecha de entrega |
| listar_mis_facturas | — | ✔ | Facturas con monto, saldo y estado |
| consultar_orden | order_id | ✔ | Un contrato **de los suyos**, por folio |
| cerrar_sesion | — | ✔ | Deja de mostrar su información |
| escalar_a_humano | motivo | — | Log + mensaje de escalamiento |

Las tools marcadas con ✔ están declaradas en `TOOLS_CON_SESION`: sin sesión
devuelven `identificado: false` y el agente pide identificarse. Freno local de
intentos en `sesiones.py` (`MAX_INTENTOS_LOCALES`); el bloqueo que cuenta y
audita lo lleva el ERP.

Cliente del mock (`MockERPClient`): Molinos del Bajío S.A. de C.V., RFC
`MBA950101AB1` — saldo $204,500.00 con $112,500.00 vencidos.

### Compras (agents/compras.py) — Funcional vía ERP (mock o HTTP)

| Tool | Params requeridos | Qué hace |
|---|---|---|
| consultar_oc | folio | Estado y detalles de una OC (ERP) |
| listar_oc_pendientes | — | OC pendientes de aprobación (ERP) |
| crear_oc | proveedor, producto, cantidad_ton | Crea una OC (ERP) |
| aprobar_oc | folio | Aprueba una OC (ERP) |
| listar_proveedores | — | Proveedores registrados (ERP) |
| transferir_a_ventas | motivo | Cambia agente activo en bus |

Acceso restringido por lista blanca `COMPRAS_PHONES_ALLOWED` (vacía = sin
restricción en desarrollo). `transferir_a_ventas` no requiere autorización.

### Inventario (agents/inventario.py) — Funcional vía ERP (mock o HTTP)

| Tool | Params requeridos | Qué hace |
|---|---|---|
| consultar_stock | producto | Stock, umbral, ubicación, estado (ERP `get_inventory_item`) |
| listar_alertas_inventario | — | Productos bajo umbral (ERP `list_inventory`) |
| resumen_inventario | — | Todos los productos (ERP `list_inventory`) |
| transferir_a_ventas | motivo | Cambia agente activo |

Alertas proactivas: el ERP llama a `POST /webhooks/erp/inventory-alert` cuando
un producto cae bajo umbral; el webhook publica en el bus y notifica al equipo
(`INVENTORY_ALERT_PHONES`).

Stock del mock (`MockERPClient`): trigo cristalino (200 ton, umbral 250 → bajo_umbral), soya (150 ton, umbral 200 → bajo_umbral), resto normal.

## Datos del ERP mock

Contratos en MockERPClient (teléfono 5215512345678):
- CONT-2026-0001: Molinos del Bajío, maíz amarillo 50ton, $185,000, EN_PROCESO / EN_TRANSITO
- CONT-2026-0002: Molinos del Bajío, trigo cristalino 30ton, $92,000, ACTIVO

Órdenes de compra (OC) en MockERPClient:
- OC-2026-0001: Granos del Norte, maíz amarillo 100ton, $510,000, pendiente
- OC-2026-0002: Agrícola del Pacífico, sorgo 80ton, $380,000, aprobada

Proveedores: PROV-001 Granos del Norte (maíz), PROV-002 Agrícola del Pacífico (sorgo, trigo).

## Variables de entorno

```
ANTHROPIC_API_KEY, CLAUDE_MODEL (default: claude-opus-4-8)
WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET
ERP_BASE_URL (vacío = mock), ERP_API_KEY, ERP_API_KEY_HEADER (default: X-Bot-Api-Key)
ERP_WEBHOOK_SECRET
WHATSAPP_AVISO_TEMPLATE (vacío = texto libre; obligatoria en producción)
REDIS_URL (vacío = memoria), HISTORY_TTL_SECONDS (7d), DEDUP_TTL_SECONDS (1d)
COMPRAS_PHONES_ALLOWED (vacío = sin restricción; lista separada por comas)
INVENTORY_ALERT_PHONES (vacío = solo log+bus; lista separada por comas)
```

El autoservicio del cliente no agrega variables aquí: sus topes (intentos,
bloqueo, duración de la sesión) viven del lado del ERP
(`BOT_CLIENTE_MAX_INTENTOS`, `BOT_CLIENTE_BLOQUEO_MINUTOS`,
`BOT_CLIENTE_SESION_MINUTOS`), que es quien manda `expira_en_segundos` en la
respuesta de identificación.
