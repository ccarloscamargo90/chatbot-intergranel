# Endpoint del ERP para el chatbot (referencia)

Esta carpeta contiene una **implementación de referencia** del endpoint que el
ERP de Intergranel (NestJS) debe exponer para que el chatbot consulte órdenes.
Cópiala a tu repo `ERP-INTERGRANEL` (no se ejecuta desde el chatbot).

## Qué expone

Un módulo `bot` read-only, protegido por **API key**, bajo el prefijo
`/api/v1`:

```
GET /api/v1/bot/ordenes/:folio          -> Order (JSON) | 404
GET /api/v1/bot/ordenes?telefono=<E164> -> Order[] (JSON)
```

La "orden del cliente" se mapea a un **Contrato** (`CONT-YYYY-NNNN`). El DTO de
salida coincide con el modelo `Order` que consume el chatbot (`app/models.py`):

```jsonc
{
  "id": "CONT-2026-0001",
  "cliente": "Molinos del Bajío S.A.",
  "telefono": "5215512345678",
  "estado": "EN_PROCESO",            // EstadoContrato
  "estado_embarque": "EN_TRANSITO",  // último Embarque (opcional)
  "estado_factura": "EMITIDA",       // Factura (opcional)
  "total": 185000.0,                  // pesos (montoTotal en centavos / 100)
  "moneda": "MXN",
  "fecha": "2026-05-20",
  "fecha_entrega_estimada": "2026-05-31",
  "lineas": [{ "producto": "Maíz amarillo", "cantidad": 50, "unidad": "ton" }],
  "notas": null
}
```

## Seguridad

- Header `X-Bot-Api-Key` validado por `BotApiKeyGuard` contra `BOT_API_KEY`
  (variable de entorno del ERP).
- Marca el controlador como público respecto al guard JWT global. En este repo
  el patrón habitual es un decorador de bypass (p. ej. `@Public()` o
  `@SkipAuth()`); aquí se asume `@Public()` — **ajústalo al nombre real** de tu
  proyecto (revisa `common/decorators` y cómo se registra el guard JWT global
  en `app.module.ts`).

## Cómo integrarlo

1. Copia `bot.module.ts`, `bot.controller.ts`, `bot.service.ts` y
   `bot-api-key.guard.ts` a `apps/backend/src/modules/bot/`.
2. Importa `BotModule` en `app.module.ts`.
3. Define `BOT_API_KEY` en el entorno del ERP (Railway). Usa el **mismo** valor
   en el chatbot como `ERP_API_KEY` (con `ERP_API_KEY_HEADER=X-Bot-Api-Key`).
4. Ajusta nombres de campos/relaciones de Prisma si difieren de los asumidos
   (`Contrato`, `Cliente.telefono`, `Embarque.estado`, `Factura.estado`).

> Si prefieres que implemente esto directamente en el repo del ERP, dame acceso
> (agrega `ERP-INTERGRANEL` al alcance de mis herramientas de GitHub) y lo hago
> en un PR allí.
