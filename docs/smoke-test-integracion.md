# Smoke test de integración — Chatbot ↔ ERP

Checklist para validar end-to-end la integración entre el chatbot multi-agente
y el módulo `bot` del ERP (NestJS) una vez ambos estén desplegados.

Se prueba en 4 capas, de la más aislada a la más integrada:

1. ERP directo (HTTP con API key).
2. Cableado chatbot → ERP.
3. Flujo conversacional por WhatsApp (por agente).
4. Alerta proactiva ERP → chatbot.

---

## 0. Prerrequisitos

- [ ] ERP desplegado con el módulo `bot` (PR del ERP mergeado).
- [ ] Chatbot desplegado.
- [ ] Variables que **deben coincidir** en ambos lados:

| Chatbot | ERP | Debe coincidir |
|---|---|---|
| `ERP_API_KEY` | `BOT_API_KEY` | ✅ mismo valor |
| `ERP_WEBHOOK_SECRET` | `BOT_WEBHOOK_SECRET` | ✅ mismo valor |
| `ERP_API_KEY_HEADER=X-Bot-Api-Key` | (header esperado) | ✅ |
| `ERP_BASE_URL=https://<erp>/api/v1` | — | termina en `/api/v1` |
| — | `BOT_WEBHOOK_URL=https://<chatbot>` | sin `/api`, raíz |
| `INVENTORY_ALERT_PHONES=...` | — | teléfonos del equipo |

Variables de apoyo para los comandos de abajo:

```bash
export ERP=https://<host-del-erp>/api/v1
export BOT=https://<host-del-chatbot>
export KEY=<valor de BOT_API_KEY / ERP_API_KEY>
export WEBHOOK_SECRET=<valor de BOT_WEBHOOK_SECRET / ERP_WEBHOOK_SECRET>
```

---

## 1. ERP directo (13 endpoints)

Todos requieren el header `X-Bot-Api-Key: $KEY`. Ajusta folios/productos a datos
reales del ERP. Lo importante: **código 200 y DTO con la forma esperada**
(claves exactas, montos en pesos, `estado` de inventario derivado).

### Seguridad
- [ ] Sin header o con key inválida → **401** (o 503 si `BOT_API_KEY` no está configurada).

```bash
curl -s -o /dev/null -w "%{http_code}\n" $ERP/bot/precios   # esperado: 401
```

### Soporte
- [ ] `GET /bot/ordenes/:folio` → `Order | 404`
- [ ] `GET /bot/ordenes?telefono=<E164>` → `Order[]`

```bash
curl -s -H "X-Bot-Api-Key: $KEY" $ERP/bot/ordenes/CONT-2026-0001 | jq .
curl -s -H "X-Bot-Api-Key: $KEY" "$ERP/bot/ordenes?telefono=5215512345678" | jq .
```

### Ventas
- [ ] `GET /bot/precios/:producto` → `Price | 404` (claves: `producto, precio_ton, moneda, disponible_ton, vigencia`)
- [ ] `GET /bot/precios` → `Price[]`
- [ ] `POST /bot/cotizaciones` → `Quote` (`id, producto, cantidad, total, moneda, vigencia, estado`); verifica que `total = precio_ton * cantidad`
- [ ] `POST /bot/solicitudes` → `PurchaseRequest` (`estado: "pendiente"`)

```bash
curl -s -H "X-Bot-Api-Key: $KEY" "$ERP/bot/precios/ma%C3%ADz%20amarillo" | jq .
curl -s -H "X-Bot-Api-Key: $KEY" $ERP/bot/precios | jq .
curl -s -X POST -H "X-Bot-Api-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"producto":"trigo","cantidad":10,"telefono":"5215512345678"}' \
  $ERP/bot/cotizaciones | jq .
curl -s -X POST -H "X-Bot-Api-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"producto":"soya","cantidad":5,"telefono":"5215512345678"}' \
  $ERP/bot/solicitudes | jq .
```

### Compras
- [ ] `GET /bot/oc/:folio` → `PurchaseOrder | 404`
- [ ] `GET /bot/oc?estado=pendiente` → `PurchaseOrder[]` (solo pendientes)
- [ ] `POST /bot/oc` → `PurchaseOrder` (`estado: "pendiente"`)
- [ ] `PATCH /bot/oc/:folio/aprobar` → `PurchaseOrder` (`estado: "aprobada"`); 404 si no existe
- [ ] `GET /bot/proveedores` → `Supplier[]`

```bash
curl -s -H "X-Bot-Api-Key: $KEY" "$ERP/bot/oc?estado=pendiente" | jq .
curl -s -X POST -H "X-Bot-Api-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"proveedor":"Granos del Norte","producto":"maíz amarillo","cantidad":100}' \
  $ERP/bot/oc | jq .                                  # anota el folio devuelto
curl -s -X PATCH -H "X-Bot-Api-Key: $KEY" $ERP/bot/oc/<FOLIO>/aprobar | jq .
curl -s -H "X-Bot-Api-Key: $KEY" $ERP/bot/proveedores | jq .
```

### Inventario
- [ ] `GET /bot/inventario/:producto` → `InventoryItem | 404`; `estado="bajo_umbral"` si `stock_ton < umbral_ton`
- [ ] `GET /bot/inventario` → `InventoryItem[]`

```bash
curl -s -H "X-Bot-Api-Key: $KEY" "$ERP/bot/inventario/trigo%20cristalino" | jq .
curl -s -H "X-Bot-Api-Key: $KEY" $ERP/bot/inventario | jq .
```

---

## 2. Cableado chatbot → ERP

- [ ] El health del chatbot reporta ERP en modo HTTP (no mock):

```bash
curl -s $BOT/ | jq .      # esperado: {"erp":"http", ...}
```

- [ ] Revisa los logs del chatbot: las llamadas a tools de Ventas/Compras/Inventario
      deben golpear el ERP real (sin errores de auth ni de parseo de DTO).

> Si `erp` aparece como `"mock"`, falta `ERP_BASE_URL` en el chatbot.

---

## 3. Flujo conversacional por WhatsApp (por agente)

Envía estos mensajes desde un WhatsApp real al número del bot y verifica la
respuesta. El router debe clasificar y la tool debe traer datos **del ERP**.

### Router / comandos
- [ ] `/menu` → muestra el menú de agentes.
- [ ] `/ventas` → saludo del agente de Ventas.

### Ventas
- [ ] "¿Cuánto cuesta el maíz amarillo?" → precio del ERP.
- [ ] "Cotiza 20 toneladas de trigo" → cotización con total correcto.
- [ ] "Quiero pedir 5 toneladas de soya" → solicitud registrada (estado pendiente).

### Soporte (continuidad y transferencia)
- [ ] "¿Cómo va mi contrato CONT-2026-0001?" → estado del contrato.
- [ ] Estando en Ventas, "tengo un problema con una entrega" → transfiere a Soporte
      (el siguiente mensaje lo atiende Soporte).

### Inventario
- [ ] "¿Cuánto trigo cristalino hay?" → stock y estado.
- [ ] "¿Qué productos están bajo umbral?" → lista de alertas.

### Compras (lista blanca)
- [ ] Desde un teléfono **en** `COMPRAS_PHONES_ALLOWED`: "lista las OC pendientes" → datos del ERP.
- [ ] Desde un teléfono **fuera** de la lista (si está configurada): la tool responde "no autorizado".
- [ ] "Crea una OC a Granos del Norte por 50 ton de maíz" → OC creada (estado pendiente).

> Dedup: reenviar el mismo mensaje no debe producir doble respuesta.

---

## 4. Alerta proactiva ERP → chatbot

### Webhook directo (aislado)
- [ ] POST al webhook del chatbot con secreto correcto → 200 y, si hay
      `INVENTORY_ALERT_PHONES`, el equipo recibe el WhatsApp.

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d '{"producto":"trigo cristalino","stock_ton":200,"umbral_ton":250,"ubicacion":"Silo Querétaro"}' \
  $BOT/webhooks/erp/inventory-alert | jq .
```

- [ ] Mismo POST con secreto incorrecto → **401**.

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: malo" \
  -d '{"producto":"x","stock_ton":1,"umbral_ton":2}' \
  $BOT/webhooks/erp/inventory-alert      # esperado: 401
```

### Flujo real (ERP dispara)
- [ ] Provoca en el ERP una operación que deje un producto **por debajo de su
      umbral** y verifica que:
  - [ ] El equipo (`INVENTORY_ALERT_PHONES`) recibe el WhatsApp de alerta.
  - [ ] Un fallo de notificación **no interrumpe** la operación del ERP (fail-soft).

---

## Resultado

- [ ] Capa 1 (ERP directo): 13/13 endpoints OK + seguridad.
- [ ] Capa 2 (cableado): health=`http`, sin errores en logs.
- [ ] Capa 3 (WhatsApp): cada agente responde con datos del ERP; router/transferencias OK.
- [ ] Capa 4 (alerta): webhook OK con secreto, 401 sin él, flujo real notifica.

Si todo está marcado, la integración está validada end-to-end. ✅
