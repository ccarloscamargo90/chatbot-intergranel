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
- CRM: NestJS + Drizzle (repo separado). **Los PRECIOS salen de aquí**, con
  header `X-Agent-Key`. El bot no consulta el ERP para precios.
- PDF de cotizaciones: fpdf2 (Python puro, sin navegador)
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
  chatwoot.py          ← Bandeja del asesor humano (abstracto + HTTP + mock)
  handoff.py           ← Quién está con un asesor y no con el bot
  agents/
    base.py            ← BaseAgent: loop agéntico (Claude + tools + historial)
    ventas.py           ← Precios y cotizaciones vía CRM; contratos vía ERP
    compras.py          ← Funcional vía ERP, con lista blanca de teléfonos
    inventario.py       ← Funcional vía ERP, con alertas proactivas
    soporte.py          ← Atención al cliente + autoservicio (identificación por RFC)
    proveedores.py      ← El otro lado del mostrador: "¿ya me pagaron?"
  assistant.py          ← Wrapper compat para tests legacy
  errores.py           ← El motivo REAL de un fallo HTTP (Meta o ERP), no el status
  fletes.py            ← Cotización de fletes: salida al transportista e
                          interpretación de su respuesta (ERP · BUG-77)
  crm.py               ← Precios y cotizaciones: la fuente es el CRM, no el ERP
  cotizacion_pdf.py    ← El PDF que se lleva el cliente (fpdf2, sin navegador)
  resumen.py           ← El resumen de lo que se habló, para el vendedor del CRM
  tareas.py            ← Trabajo en segundo plano: lo que el cliente no espera
  config.py, erp.py, history.py, dedup.py, whatsapp.py, notifications.py, models.py
tests/
  test_api.py, test_assistant.py, test_bus.py, test_router.py,
  test_ventas.py, test_erp.py, test_history.py, test_dedup.py,
  test_media.py, test_signature.py, test_soporte.py, test_compras.py,
  test_inventario.py, test_avisos.py, test_clientes.py, test_botones.py,
  test_chatwoot.py, test_documentos.py, test_proveedores.py, test_fletes.py,
  test_crm.py, test_cotizacion_pdf.py, test_resumen.py, test_tareas.py
  conftest.py           ← Fixture `soporte`: el agente con sus mocks (ERP, CRM, Chatwoot)
docs/erp/               ← Implementación de referencia NestJS, contrato de avisos
                          (AVISOS_WHATSAPP.md) y de autoservicio del cliente
                          (AUTOSERVICIO_CLIENTES.md)
docs/CHATWOOT_HANDOFF.md ← Handoff a un asesor: qué crear en Chatwoot y por qué
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

## Documentos al cliente (tool `enviar_mi_documento`)

El cliente identificado puede pedir su factura (PDF **y** XML), su contrato
firmado, su **cotización** o su estado de cuenta, y le llegan por WhatsApp.

Los bytes van del ERP al bot y del bot a la Media API de Meta
(`WhatsAppClient.upload_media` → `send_document` por `media_id`). **Nunca se
publica una URL** desde la que se pueda bajar el documento de un cliente, ni
siquiera firmada y de cinco minutos: un CFDI no es un archivo cualquiera.

Tres cosas que conviene no romper:

- La tool está en `TOOLS_CON_SESION`, y el ERP resuelve el folio **entre los del
  cliente**. Sin las dos, adivinar un consecutivo entregaría la factura de otro.
- Al pedir una factura se mandan **PDF y XML**. El XML es el que vale
  fiscalmente y el que pide el contador; mandar solo el PDF deja el trabajo a
  medias.
- El `filename` es lo que le queda guardado al cliente en el teléfono:
  `FACT-2026-0031.pdf`, no `documento.pdf`.

Los tipos válidos viven en tres lados que tienen que coincidir —`TIPOS_DOCUMENTO`,
el `enum` de la tool y el ERP—; `test_documentos.py` falla si se separan.

### Cotizaciones: el borrador NO sale, y el precio vencido se anuncia

Hay **dos** cosas que se llaman cotización y no son la misma: `Cotizacion` en el
ERP es el documento formal pre-contrato (cliente, vendedor, grano, toneladas,
precio, vigencia, y `convertirAContrato` cuando se acepta); `BotCotizacion` es
la consulta de precio suelta que el agente de Ventas guarda por teléfono. La que
se manda por WhatsApp es la **formal**.

Dos reglas que conviene no romper:

- **Las BORRADOR no se listan ni se mandan.** Un borrador es el precio con el
  que el vendedor todavía está trabajando —lo sube, lo baja, revisa margen— y
  que el cliente no ha recibido. Entregárselo le pone en la mano una oferta que
  la empresa no ha hecho. Un borrador cae en el mismo "no aparece en su cuenta"
  que un folio ajeno: decir "sí es suya pero está en borrador" delataría que le
  están preparando algo.
- **`vencida` viene calculada del ERP**, no se deduce de la fecha. Si el modelo
  tuviera que comparar `vigencia_hasta` contra hoy para saber si un precio sigue
  vivo, tarde o temprano ofrecería uno caducado. El PDF también lo marca, en
  rojo. El grano se mueve de precio y el cliente hace cuentas con lo que se le
  diga.

### El MIME que Meta acepta no es el MIME del archivo

La lista de tipos de documento de Meta es **cerrada** y `application/xml` no
está en ella: PDF, los de Office y `text/plain`. Un XML subido con su MIME real
hace que la Media API rechace el archivo y no salga nada. `MIME_PARA_META`
(en `whatsapp.py`) lo traduce a `text/plain` conservando el nombre `.xml`, que
es lo que decide la extensión con la que le queda guardado al cliente.

Esto no lo veían las pruebas: mockeaban a Meta, y un mock acepta cualquier MIME.
Por eso `WhatsAppClient` ahora recibe un `transport` inyectable, como
`HTTPERPClient` — sin él, de un envío solo se puede probar el modo desarrollo.

### Cuando un documento no sale, el motivo importa

`enviar_mi_documento` devuelve `enviado: false` con un `motivo` distinto por
causa, y el prompt tiene prohibido mezclarlos:

| `motivo` | Qué pasó |
|---|---|
| `no_esta_en_su_cuenta` | No es suyo (o no existe: no se distinguen, a propósito) |
| `sin_archivo_cargado` | **Sí** es suyo, pero nadie subió el archivo al ERP |
| `archivo_no_recuperable` | Es suyo, hay archivo, el ERP no lo pudo bajar |
| `fallo_al_enviar` | El documento salió del ERP y Meta lo rechazó |

La distinción no es cosmética. Con un solo motivo para todo, el bot le dijo a un
cliente que sus facturas no aparecían en su cuenta —las que acababa de listarle—
y, ante la contradicción, inventó una causa: *"es el módulo de envío el que no
las está entregando"*. Lo que devuelve una tool es lo único que el modelo tiene
para contestar: si el motivo va mudo, el modelo rellena el hueco.

De ahí también que `errores.py` (`detalle_http` / `detalle_respuesta`) sea
compartido: `str(HTTPStatusError)` dice el status y tira el mensaje del ERP o de
Meta, que es el único que explica algo. Estaba resuelto solo para los avisos.

## Proveedores: el otro lado del mostrador (app/agents/proveedores.py)

Quien nos VENDE también escribe a este número, y su pregunta es siempre la
misma: **"¿ya me pagaron?"** — y su gemela, "¿cuándo?". Se identifica con la
razón social de su empresa y su RFC, igual que el cliente, y consulta cuánto se
le debe, qué ya venció, sus facturas y sus órdenes de compra.

**Es un agente aparte, no una rama de Soporte.** Son dos audiencias con datos
que no se pueden mezclar: sesiones en tablas distintas del ERP
(`X-Bot-Sesion-Proveedor`) y en claves distintas del bus
(`bus:proveedores:sesion:`). Un mismo teléfono puede ser cliente Y proveedor
—se le compra a quien también se le vende— y cada identificación abre lo suyo
sin pisar la otra.

**No es una categoría del clasificador**, a propósito. Un modelo no puede saber
por el texto si quien pregunta "¿cuándo me pagan?" es un proveedor o un cliente
esperando su nota de crédito, y equivocarse manda a alguien a identificarse
contra el padrón que no es. Se entra por el botón **🚚 Soy proveedor** del menú
anónimo o por `/proveedor`: intenciones exactas. El prompt de Soporte —que es el
fallback— sabe ofrecer esa puerta cuando huele a proveedor.

Cuatro cosas que el código cuida:

- **Sin RFC no hay autoservicio.** Un proveedor extranjero no tiene RFC
  mexicano y el RFC ES el segundo factor; identificarlo por "nombre + país"
  sería adivinable. El prompt tiene prohibido prometerle otra vía: se le dice
  que su comprador lo sigue atendiendo por correo.
- **Nunca se promete una fecha de pago.** Se puede decir qué está vencido y qué
  no; cuándo se paga lo decide una persona. Prometerlo por chat crea una deuda
  de palabra que nadie autorizó.
- **`vencida` viene calculada del ERP.** Restar fechas es como el modelo acaba
  diciéndole a alguien que su pago está al corriente cuando lleva un mes.
- **Nada menciona una marca propia.** Un proveedor no debe saber bajo qué marca
  se revende lo que nos vende (regla de oro de GRANCORE, del lado del ERP).
  `test_proveedores.py` lo verifica sobre las respuestas de las tools **y**
  sobre el prompt.

## Handoff a un asesor humano (app/chatwoot.py, app/handoff.py)

Cuando el cliente pide una persona, `escalar_a_humano` abre una conversación en
Chatwoot con una nota privada de contexto, marca el teléfono en handoff y el bot
**deja de contestar**. Lo que escriba el cliente se reenvía a la bandeja; lo que
escriba el asesor sale por WhatsApp vía `POST /webhooks/chatwoot`. Al resolver la
conversación, el bot retoma.

**El bot conserva el número de WhatsApp**, no se le cede a Chatwoot. Chatwoot no
manda mensajes interactivos por la Cloud API, así que cederle el número cambiaría
los menús de botones por texto plano en TODAS las conversaciones para ganar
comodidad solo en las que llegan a un asesor.

Cuatro cosas que el código cuida y conviene no romper:

- **No hacer eco.** Solo se relaya lo `outgoing` y no privado; lo demás es lo que
  el propio bot publicó y devolvérselo al cliente sería un bucle.
- **No duplicar.** Se deduplica por id de mensaje, y si el envío falla se suelta
  el candado para que el reintento de Chatwoot sirva.
- **No dar por entregado lo que Meta rechazó.** Fuera de la ventana de 24 h el
  texto libre se cae; el fallo se escribe como nota privada en la bandeja, que es
  donde el asesor lo va a ver.
- **No prometer un asesor que nadie avisó.** Si Chatwoot no está configurado o
  falla, `escalar_a_humano` devuelve `escalado: false` y el prompt tiene prohibido
  decir que alguien lo contactará.

Contrato completo y qué crear en la instancia: `docs/CHATWOOT_HANDOFF.md`.

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
pytest -q                  # 359 tests pasando
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
- `GET /api/v1/bot/clientes/cotizaciones` → `CustomerQuote[]` (sin BORRADOR)
- `GET /api/v1/bot/clientes/documentos/:tipo?folio=` → los bytes del archivo
- `POST /api/v1/bot/clientes/cerrar-sesion`

Contrato completo (incluida la nota honesta sobre la fuerza de la
identificación) en `docs/erp/AUTOSERVICIO_CLIENTES.md`.

### Fase 7 ✅ — Cotización de fletes a transportistas (ERP · BUG-77)
Completada. El ERP arma la solicitud, decide a quién se le pregunta y hace
cumplir el tope por transportista y por día. El bot hace las dos cosas que el
ERP no puede: **mandar el mensaje** y **entender la respuesta**. Todo vive en
`app/fletes.py`.

**Salida.** La solicitud llega por el MISMO webhook que los avisos internos
(`POST /webhooks/erp/notificacion`) pero con `tipo: cotizacion_flete.solicitud`,
y toma otro camino: se manda **el texto del ERP tal cual**, con `send_text`.

No es un detalle de estilo. `notify_erp_aviso` firma con
`event.empresa or settings.company_name`, y el ERP manda `empresa: null` a
propósito porque un transportista es un PROVEEDOR: ese fallback volvería a meter
la marca comercial que el ERP le quitó antes de mandársela. La regla de oro de
GRANCORE aplica igual de este lado, y `test_fletes.py` la hace cumplir.

Al mandar, el teléfono queda marcado en el bus
(`bus:flete:pendiente:{telefono}`, TTL 3 días) con la referencia
`cotizacion_flete:<id>` que viajó en el mensaje.

**Entrada.** Mientras ese teléfono esté marcado, lo que escriba NO va al router:
va a `_capturar_flete`. Mandarlo al agente de ventas le contestaría sobre
precios de grano a alguien que está ofreciendo un camión.

La respuesta se interpreta con el modelo de los AGENTES, no con el clasificador
rápido del router: clasificar mal manda una conversación al agente equivocado y
se corrige al siguiente mensaje; leer mal un precio mete un número que se
compara contra otros, se elige y se paga. Se le piden PESOS al modelo y se
convierten a centavos en código — pedirle centavos invita a un error de x100.

Lo que se entiende viaja al ERP **junto con el texto crudo**, nunca en su lugar:

```
POST /api/v1/bot/cotizaciones-flete/respuesta
{ wamid, telefono, texto, referencia?, interpretacion? }
```

Tres cosas que conviene no romper:

- **Lo que no se entiende se deposita igual.** Que un transportista contestó es
  un hecho; el ERP lo marca para revisión humana. Si el ERP está caído, el
  transportista recibe acuse de todas formas: no tiene la culpa.
- **Un precio sin unidad no es un precio.** "28 mil" sin decir de qué no se
  puede comparar contra nada, así que se deja sin monto en vez de entrar a la
  comparativa con una unidad supuesta.
- **El pendiente se limpia solo con precio o negativa.** Quien contesta
  "¿cuántas toneladas?" sigue en la misma conversación, no en una nueva.

Se extendió `ERPClient` con `depositar_respuesta_flete` (abstracto + HTTP +
mock) y se añadieron los modelos `SolicitudFletePendiente`,
`InterpretacionFlete` y `DepositoRespuestaFlete`.

### Fase 8 ✅ — El precio viene del CRM, no del ERP
Completada. El agente de Ventas dejó de preguntarle el precio al ERP: se lo
pregunta al **CRM** (`app/crm.py`). La cadena queda en un solo sentido:

```
ERP  ──publica su catálogo──►  CRM  ──sirve el precio──►  CHATBOT
(dueño del precio)          (espejo de solo lectura)    (nunca toca el ERP)
```

Del lado del ERP, `Inventario → Precio de venta` calcula el precio por tonelada
desde el costo real del kardex (+ flete, merma, almacenaje, costo financiero y
margen) y lo publica al CRM **al guardar**. Del lado del CRM,
`GET /api/ingest/catalog` lo sirve al bot con la misma llave por empresa
(`X-Agent-Key`) que ya usaba la ingesta: **la llave decide la empresa**, nunca
el cuerpo de la petición.

**Por qué el precio no lo daba nadie antes.** El bot leía `bot_precios` del ERP,
una tabla que **nadie escribe desde la aplicación**: se creó en una migración y
ahí se quedó. Los precios del bot llevaban congelados desde entonces.

Cuatro cosas que el código cuida:

- **Sin CRM no hay precio, y no se cae al ERP.** El atajo el día que el CRM está
  caído es exactamente el día en que la regla de dirección deja de ser verdad y
  dos sistemas empiezan a decir precios distintos. Se ofrece un asesor.
- **La frescura viaja con el precio.** El CRM copió el dato del ERP; si esa copia
  se quedó vieja (`stale`), el bot NO da ninguna cifra. Decir un precio de la
  semana pasada con la seguridad de uno de hoy es prometer lo que ya no existe.
- **Un motivo distinto por causa**, como en `enviar_mi_documento`, y el prompt
  tiene prohibido mezclarlos:

| `motivo` | Qué pasó |
|---|---|
| `no_esta_en_catalogo` | No lo vendemos |
| `sin_precio_publicado` | **Sí** lo vendemos, nadie le ha puesto precio |
| `datos_no_confiables` | El espejo del CRM está viejo o falló |
| `crm_no_disponible` | No se pudo consultar el CRM |

  Los dos primeros no son lo mismo, y confundirlos hace que el bot le diga a un
  cliente que no manejamos algo que sí manejamos.

- **La cotización se pide a nombre de alguien.** `generar_cotizacion` exige
  `nombre_cliente`. Es una cortesía para dejarla a nombre de quien la pidió y
  crear el prospecto en el CRM — **no es un candado**: el nombre no valida nada
  y no debe tratarse como si lo hiciera. Por eso detrás de él solo hay precio de
  lista y la cotización recién hecha; los datos de la CUENTA de un cliente
  (saldo, facturas, contratos) siguen exigiendo RFC en el agente de Soporte.

La cotización queda en el CRM como cotización del agente —tablero, estados,
aprobación— y **el CRM avisa solo al vendedor** que la regla de asignación
eligió. Si el CRM responde `modo_cotizacion: "manual"`, el bot no le da el total
al cliente: un vendedor le hace llegar la formal.

Contrato completo de la cadena en `ERP-INTERGRANEL/docs/PRECIO_DE_VENTA.md` y
`crm-intergranel-group/docs/precio-al-chatbot.md`.

### Fase 9 ✅ — El PDF al cliente, el contexto al vendedor, y el inventario callado
Completada. Tres cosas que no tenían que ver entre sí pero que cambian el mismo
camino: cotizar.

**1. La cotización se va en PDF, y el mismo archivo queda en el CRM.**
`app/cotizacion_pdf.py` arma la hoja con `fpdf2` (Python puro: ningún navegador
ni librería del sistema en el contenedor) y `generar_cotizacion` la manda por la
Media API de Meta —`upload_media` → `send_document`, los mismos bytes, nunca una
URL— y la empuja al CRM en `pdfBase64` dentro de la MISMA llamada de
`/ingest/quotes`. Un solo archivo generado una sola vez: si se generara dos
veces, el vendedor podría estar viendo una versión y el cliente otra.

El nombre lleva el folio (`Cotizacion-COT-20260914-064512-5678.pdf`) porque es
lo que le queda guardado al cliente en el teléfono y lo que va a mencionar
cuando llame.

**Dos cifras que el PDF no inventa.** El IVA (`COTIZACION_IVA_TASA`) y la
vigencia (`COTIZACION_VIGENCIA_DIAS`) se imprimen solo si alguien las configuró.
Sin configurar no aparecen —y en lugar de la vigencia el pie dice que hay que
confirmar el precio con un asesor—: un "+16 % de IVA" o un "vigente 5 días"
puestos por omisión serían una condición comercial que nadie autorizó, escrita
en un documento que el cliente va a tratar como una oferta. Cuando el IVA sí
está configurado, la misma tasa viaja al CRM (`taxRate`) para que el total del
tablero sea el mismo número que el del PDF.

Un motivo distinto por causa, como en `enviar_mi_documento`:

| `motivo_pdf` | Qué pasó |
|---|---|
| `requiere_revision_de_vendedor` | El CRM está en modo manual: el PDF se guarda para que una persona lo apruebe, y al cliente NO se le manda |
| `no_se_pudo_generar` | La cotización sí quedó registrada; el archivo no se armó |
| `fallo_al_enviar` | El PDF existe y Meta lo rechazó |

**2. El resumen de lo que se habló queda como NOTA del prospecto.** Antes, de
una cotización por WhatsApp al CRM llegaba un nombre, un teléfono y un PDF: el
vendedor marcaba sin saber si el cliente preguntó de pasada o tiene una planta
parada. Ahora `app/resumen.py` redacta la nota y se guarda en dos momentos, con
los endpoints que el CRM ya tenía:

- al cotizar → `POST /api/ingest/quote-notes` (cuelga del folio; el CRM contesta
  **409** mientras todavía no ve la cotización, que significa "todavía no" y no
  "no existe", así que se reintenta en vez de tirar el resumen);
- al pedir un asesor → `POST /api/ingest/handoffs` (crea el prospecto si no
  existía, así que también aparece quien pidió una persona sin llegar a
  cotizar).

Cuatro cosas que el código cuida:

- **El cliente no espera la nota.** Se escribe en segundo plano (`app/tareas.py`):
  lo que el cliente está esperando es su PDF, no que se acabe de redactar algo
  que él nunca va a leer. Al apagar, el `lifespan` de FastAPI espera lo que esté
  en vuelo para que un redeploy no se lleve una nota a medias.
- **Los HECHOS no los redacta el modelo.** Producto, toneladas, folio y si el
  PDF salió o no se anteponen al texto, tomados de lo que devolvió la
  herramienta. El modelo solo aporta el contexto de la plática (para qué es,
  plaza, urgencia, objeciones), tiene prohibido suponer, y si falla la nota
  sigue diciendo lo esencial.
- **Se usa el modelo de los AGENTES, no el clasificador rápido.** Mismo criterio
  que `fletes.py`: clasificar mal se corrige al siguiente mensaje, pero un
  resumen mal hecho deja escrita una frase que el vendedor va a leer como si la
  hubiera dicho el cliente.
- **Sin nombre, el nombre lleva el teléfono** (`WhatsApp 5215512345678`). El CRM
  liga el prospecto por teléfono y, si no lo encuentra, **por nombre**: un
  "Cliente de WhatsApp" genérico haría que la nota de alguien aterrizara en la
  ficha de otro.

**3. El bot ya no dice cuánto inventario hay.** Dos candados, porque un prompt
se puede rodear y un dato que no está no se puede decir:

- `consultar_precio` dejó de entregarle `existencia_ton` al modelo. Lo único que
  se dice de disponibilidad es lo cualitativo del catálogo: disponible, en
  tránsito o sobre pedido.
- El agente de **Inventario** —que da toneladas exactas, umbral y ubicación del
  silo— pasó a tener lista blanca de teléfonos (`INVENTARIO_PHONES_ALLOWED`),
  igual que Compras. El router mandaba ahí a cualquiera que preguntara "¿cuánto
  maíz tienen?"; ahora un número de fuera se queda con Ventas.
  `transferir_a_ventas` no pide autorización: es justo lo que queremos que pase.

Se extendió `CRMClient` con `registrar_nota_cotizacion` y
`registrar_canalizacion` (abstracto + HTTP + mock), `registrar_cotizacion` ganó
`pdf`, `pdf_nombre`, `vigencia_hasta` y `tasa_iva`, y se añadieron los modelos
`NotaCRM` y `CanalizacionCRM`.

**Lo que quedó fuera, a propósito.** En modo manual el CRM llama al agente
(`POST /crm/cotizacion/aprobar`) cuando el vendedor pulsa «Aprobar y enviar al
cliente». Ese endpoint no existe en este bot y no se agregó: el `AGENT_BASE_URL`
del CRM apunta a UN solo agente —hoy el de MegaCostales— así que apuntarlo aquí
rompería el otro. Mientras el modo siga en `automatic` (el de fábrica) no falta
nada; si se quiere usar el manual en Intergranel, primero hay que hacer esa
configuración por empresa del lado del CRM.

## Especificación de agentes

### Ventas (agents/ventas.py) — Precios vía CRM, contratos vía ERP

| Tool | Params requeridos | Fuente | Qué hace |
|---|---|---|---|
| listar_productos | — | CRM | Qué se vende, con precio. El modelo no enumera granos de memoria |
| consultar_precio | producto | CRM | Precio/ton, de cuándo es el dato y disponibilidad **cualitativa** (sin toneladas) |
| generar_cotizacion | producto, cantidad_ton, **nombre_cliente** | CRM | Registra la cotización, le manda el **PDF** por WhatsApp y deja el **resumen** como nota del prospecto |
| consultar_contrato | folio | ERP | Estado de un contrato |
| listar_contratos_cliente | — | ERP | Contratos del remitente |
| solicitar_pedido | producto, cantidad_ton | ERP | Registra solicitud. Publica en bus |
| transferir_a_soporte | motivo | — | Cambia agente activo en bus |

Precios del CRM simulado (`MockCRMClient`): maíz blanco $6,169.56/ton, maíz
amarillo $5,890.00, trigo cristalino $7,420.50, y sorgo dulce **sin precio** —
ese último está a propósito, para poder probar la diferencia entre "no lo
vendemos" y "sí, pero no tiene precio".

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
| listar_mis_cotizaciones | — | ✔ | Cotizaciones con precio, vigencia y si ya venció |
| enviar_mi_documento | tipo (+folio) | ✔ | Le manda por WhatsApp su factura (PDF/XML), su contrato, su cotización o su estado de cuenta |
| cerrar_sesion | — | ✔ | Deja de mostrar su información |
| escalar_a_humano | motivo | — | Abre la conversación en Chatwoot, pasa el teléfono a handoff y deja el **resumen** como nota del prospecto en el CRM |

Las tools marcadas con ✔ están declaradas en `TOOLS_CON_SESION`: sin sesión
devuelven `identificado: false` y el agente pide identificarse. Freno local de
intentos en `sesiones.py` (`MAX_INTENTOS_LOCALES`); el bloqueo que cuenta y
audita lo lleva el ERP.

Cliente del mock (`MockERPClient`): Molinos del Bajío S.A. de C.V., RFC
`MBA950101AB1` — saldo $204,500.00 con $112,500.00 vencidos.

### Proveedores (agents/proveedores.py) — Autoservicio del proveedor

| Tool | Params requeridos | Sesión | Qué hace |
|---|---|---|---|
| identificar_proveedor | nombre, rfc | — | Valida el par contra el ERP y abre sesión |
| resumen_de_mi_cuenta_proveedor | — | ✔ | Por pagar, vencido, facturas y OC abiertas |
| listar_mis_facturas_proveedor | — | ✔ | Sus facturas con saldo, moneda y si venció |
| listar_mis_ordenes_proveedor | — | ✔ | Órdenes de compra colocadas |
| cerrar_sesion_proveedor | — | ✔ | Deja de mostrar su información |

Proveedor del mock (`MockERPClient`): Granos del Norte S.A. de C.V., RFC
`GNO900215QT4` — $231,000.00 por pagar, de los cuales $185,000.00 vencidos.

Endpoints que el ERP expone:
- `POST /api/v1/bot/proveedores/identificar` → `Identificacion`
- `GET /api/v1/bot/proveedores/resumen` → `SupplierSummary`
- `GET /api/v1/bot/proveedores/facturas` → `SupplierInvoice[]`
- `GET /api/v1/bot/proveedores/ordenes` → `SupplierPurchaseOrder[]`
- `POST /api/v1/bot/proveedores/cerrar-sesion`

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

**Uso interno:** acceso restringido por lista blanca `INVENTARIO_PHONES_ALLOWED`
(vacía = sin restricción en desarrollo). Lo que contesta son toneladas exactas,
umbrales y ubicación de los silos —el que sabe cuánto grano hay sabe cuánta
prisa tenemos por vender— y el router manda aquí a cualquiera que pregunte por
existencias. `transferir_a_ventas` no requiere autorización: es la salida para
quien preguntó de buena fe queriendo comprar.

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
CRM_BASE_URL (vacío = CRM simulado), CRM_AGENT_KEY (la llave decide la empresa)
WHATSAPP_AVISO_TEMPLATE (vacío = texto libre; obligatoria en producción)
REDIS_URL (vacío = memoria), HISTORY_TTL_SECONDS (7d), DEDUP_TTL_SECONDS (1d)
COMPRAS_PHONES_ALLOWED (vacío = sin restricción; lista separada por comas)
INVENTARIO_PHONES_ALLOWED (vacío = sin restricción; quién puede consultar existencias)
INVENTORY_ALERT_PHONES (vacío = solo log+bus; lista separada por comas)
COTIZACION_IVA_TASA (0 = el PDF no menciona impuestos; 0.16 = separa IVA y va al CRM)
COTIZACION_VIGENCIA_DIAS (0 = el PDF no lleva vigencia y dice que hay que confirmar)
CHATWOOT_BASE_URL (vacío = escalamiento deshabilitado; "mock" = simulado)
CHATWOOT_API_TOKEN, CHATWOOT_ACCOUNT_ID, CHATWOOT_INBOX_ID
CHATWOOT_WEBHOOK_SECRET (obligatorio en producción: Chatwoot no firma)
HANDOFF_TTL_SECONDS (8h; al vencer el bot retoma)
```

El autoservicio del cliente no agrega variables aquí: sus topes (intentos,
bloqueo, duración de la sesión) viven del lado del ERP
(`BOT_CLIENTE_MAX_INTENTOS`, `BOT_CLIENTE_BLOQUEO_MINUTOS`,
`BOT_CLIENTE_SESION_MINUTOS`), que es quien manda `expira_en_segundos` en la
respuesta de identificación.
