# Módulo `bot` del ERP (implementación de referencia)

Esta carpeta contiene una **implementación de referencia** del módulo que el ERP
de Intergranel (NestJS + Prisma) debe exponer para que el chatbot multi-agente
consulte y registre datos. Cópiala a tu repo `ERP-INTERGRANEL` (no se ejecuta
desde el chatbot).

Cada agente del chatbot consume un grupo de endpoints; todos cuelgan del prefijo
`/api/v1/bot` y están protegidos por **API key** (`X-Bot-Api-Key`).

## Endpoints

### Soporte — órdenes (Contratos) · `bot.controller.ts`
```
GET  /api/v1/bot/ordenes/:folio          -> Order | 404
GET  /api/v1/bot/ordenes?telefono=<E164> -> Order[]
```

### Ventas — precios, cotizaciones, solicitudes · `bot-ventas.controller.ts`
```
GET  /api/v1/bot/precios/:producto   -> Price | 404
GET  /api/v1/bot/precios             -> Price[]
POST /api/v1/bot/cotizaciones        -> Quote | 404   body: { producto, cantidad, telefono }
POST /api/v1/bot/solicitudes         -> PurchaseRequest  body: { producto, cantidad, telefono }
```

### Compras — órdenes de compra y proveedores · `bot-compras.controller.ts`
```
GET   /api/v1/bot/oc/:folio          -> PurchaseOrder | 404
GET   /api/v1/bot/oc?estado=pendiente-> PurchaseOrder[]
POST  /api/v1/bot/oc                 -> PurchaseOrder   body: { proveedor, producto, cantidad }
PATCH /api/v1/bot/oc/:folio/aprobar  -> PurchaseOrder | 404
GET   /api/v1/bot/proveedores        -> Supplier[]
```

### Inventario — existencias · `bot-inventario.controller.ts`
```
GET  /api/v1/bot/inventario/:producto -> InventoryItem | 404
GET  /api/v1/bot/inventario           -> InventoryItem[]
```

### Inventario — alerta proactiva (saliente ERP -> bot) · `bot-inventory-alert.emitter.ts`
Cuando una operación del ERP deja el stock de un producto por debajo de su
umbral, el ERP llama al webhook del chatbot:
```
POST {BOT_WEBHOOK_URL}/webhooks/erp/inventory-alert
Header: X-Webhook-Secret: <BOT_WEBHOOK_SECRET>   (= ERP_WEBHOOK_SECRET del bot)
Body: { producto, stock_ton, umbral_ton, ubicacion?, mensaje? }
```
El chatbot publica la alerta en su bus y notifica al equipo (`INVENTORY_ALERT_PHONES`).

## Formato de los DTOs (lo que el chatbot espera)

```jsonc
// Order  -> el "pedido del cliente" mapea a un Contrato (CONT-YYYY-NNNN)
{ "id": "CONT-2026-0001", "cliente": "Molinos del Bajío S.A.", "telefono": "5215512345678",
  "estado": "EN_PROCESO", "estado_embarque": "EN_TRANSITO", "estado_factura": "EMITIDA",
  "total": 185000.0, "moneda": "MXN", "fecha": "2026-05-20",
  "fecha_entrega_estimada": "2026-05-31",
  "lineas": [{ "producto": "Maíz amarillo", "cantidad": 50, "unidad": "ton" }], "notas": null }

// Price
{ "producto": "maíz amarillo", "precio_ton": 5200.0, "moneda": "MXN",
  "disponible_ton": 1200.0, "vigencia": "2026-06-30" }

// Quote
{ "id": "COT-2026-0007", "producto": "trigo", "cantidad": 10, "total": 71000.0,
  "moneda": "MXN", "vigencia": "2026-06-30", "estado": "borrador" }

// PurchaseRequest
{ "id": "SOL-2026-0003", "producto": "soya", "cantidad": 5, "telefono": "5215512345678",
  "estado": "pendiente" }

// PurchaseOrder
{ "id": "OC-2026-0001", "proveedor": "Granos del Norte S.A.", "producto": "Maíz amarillo",
  "cantidad": 100, "unidad": "ton", "total": 510000.0, "moneda": "MXN",
  "estado": "pendiente", "fecha": "2026-06-01", "fecha_entrega_estimada": "2026-06-15" }

// Supplier
{ "id": "PROV-001", "nombre": "Granos del Norte S.A.",
  "productos": ["Maíz amarillo", "Maíz blanco"], "contacto": "ventas@granosdelnorte.mx" }

// InventoryItem
{ "producto": "trigo cristalino", "stock_ton": 200, "umbral_ton": 250,
  "ubicacion": "Silo Querétaro", "estado": "bajo_umbral" }
```

> **Montos:** los servicios asumen `montoTotal`/`precioTon` como `BigInt` en
> centavos y los convierten a pesos (`/100`). Ajusta si tu esquema usa `Decimal`.

## Seguridad

- Header `X-Bot-Api-Key` validado por `BotApiKeyGuard` contra `BOT_API_KEY`
  (variable de entorno del ERP). Sin clave, el guard deniega.
- Cada controlador se marca como público respecto al guard JWT global con
  `@Public()`. **Ajusta el nombre real** del decorador de bypass de tu proyecto
  (revisa `common/decorators` y cómo se registra el guard JWT global en
  `app.module.ts`). Si tu guard JWT no es global, puedes omitir `@Public()`.

## Cómo integrarlo

1. Copia todos los `bot*.ts` a `apps/backend/src/modules/bot/`.
2. Importa `BotModule` en `app.module.ts`.
3. Define en el entorno del ERP (Railway):
   - `BOT_API_KEY` — igual al `ERP_API_KEY` del chatbot (con
     `ERP_API_KEY_HEADER=X-Bot-Api-Key`).
   - `BOT_WEBHOOK_URL` y `BOT_WEBHOOK_SECRET` — para emitir alertas de
     inventario (este último igual al `ERP_WEBHOOK_SECRET` del chatbot).
4. Ajusta nombres de modelos/relaciones/campos de Prisma a tu esquema real
   (`Contrato`, `Precio`, `Cotizacion`, `SolicitudPedido`, `OrdenCompra`,
   `Proveedor`, `Inventario`). Los servicios están comentados con las
   suposiciones hechas.
5. Para disparar alertas: inyecta `BotInventoryAlertEmitter` donde el ERP
   modifique stock y llama a `maybeEmit({ producto, stockTon, umbralTon, ubicacion })`
   tras el cambio.

## Requisitos

- `@nestjs/axios` (HttpModule) para el emisor de alertas.
- `class-validator` para los DTOs de los `POST` (asume `ValidationPipe` global).

> Si prefieres que implemente esto directamente en el repo del ERP, dame acceso
> (agrega `ERP-INTERGRANEL` al alcance de mis herramientas de GitHub) y lo hago
> en un PR allí.
