# Autoservicio del proveedor por WhatsApp

Contrato entre el chatbot y el ERP para que una empresa **que nos vende** pueda
consultar lo suyo. Gemelo de `AUTOSERVICIO_CLIENTES.md`; aquí solo se escribe lo
que cambia.

## La pregunta que esto existe para contestar

**"¿Ya me pagaron?"** — y su gemela, "¿cuándo?". Hoy llega por teléfono a
cuentas por pagar, que tiene que ir a buscarla al ERP. Todo lo demás (sus
órdenes, sus facturas) es contexto para que esa respuesta se entienda sin que
nadie levante el auricular.

## Endpoints

| Ruta | Devuelve |
|---|---|
| `POST /api/v1/bot/proveedores/identificar` | `Identificacion` |
| `GET /api/v1/bot/proveedores/resumen` | `SupplierSummary` |
| `GET /api/v1/bot/proveedores/facturas` | `SupplierInvoice[]` |
| `GET /api/v1/bot/proveedores/ordenes` | `SupplierPurchaseOrder[]` |
| `POST /api/v1/bot/proveedores/cerrar-sesion` | `{ "cerrada": true }` |

El token viaja en **`X-Bot-Sesion-Proveedor`**, no en el header del cliente y
jamás en la URL.

## Por qué el header es distinto

Los tokens viven en **tablas separadas** (`bot_sesiones_proveedor` vs.
`bot_sesiones_cliente`), así que uno de cliente presentado a un endpoint de
proveedor ya rebotaría con 401 — no existe en esa tabla. El header propio es la
segunda red: hace imposible que un cambio futuro del bot mande el equivocado
sin que se note, y deja escrito en cada llamada a qué audiencia pertenece.

Se descartó una columna `tipo` en las tablas del cliente: la llave foránea
apunta a otra tabla, y unificarlas obligaría a dos FKs anulables con un CHECK
que nada impide violar desde una migración futura.

## El candado es el mismo, y vive una sola vez

Nombre + RFC, intentos contados por teléfono, bloqueo temporal, bitácora de todo
intento (`bot_accesos_proveedor`) y un fallo que **nunca dice cuál de los dos
datos falló ni si el RFC existe**. Las reglas están en `bot-candado.ts` y las
usan las dos audiencias: copiadas, la próxima corrección de seguridad se
aplicaría en una y se olvidaría en la otra.

## Dos reglas propias del proveedor

### Sin RFC no hay autoservicio

`Proveedor.rfc` es opcional en el modelo porque los proveedores **extranjeros**
no tienen RFC mexicano. Por este canal no pueden entrar, y no es un hueco
pendiente: **el RFC ES el segundo factor**. Identificar a un extranjero por
"nombre + país" sería adivinable en tres intentos.

El bot tiene prohibido prometerle otra vía. Se le dice que su comprador lo sigue
atendiendo por correo, que es la verdad.

### Solo expediente ACTIVO

`estadoExpediente` gobierna quién puede recibir OC o pago. Un proveedor dado de
baja recibe el **mismo "no coincide"** que un desconocido: decirle "existe pero
está dado de baja" le confirmaría a un tercero que ese RFC es proveedor de la
casa.

## Confidencialidad de marca

Este canal es una **salida hacia proveedor**, así que le aplica la regla de oro:
nada de lo que sale puede mencionar una marca propia del grupo. Un proveedor no
debe saber bajo qué marca se revende lo que nos vende. Se verifica en
`bot-proveedores.service.spec.ts` (ERP) y en `test_proveedores.py` (bot), este
último sobre las respuestas **y** sobre el prompt.

## Moneda

Los importes de cada factura van **en la moneda del CFDI**, sin convertir:
`FacturaProveedor.moneda` existe porque el comprobante se guarda como se emitió
(BUG-52). Decirle pesos a quien tiene una factura en dólares es darle un número
que no cuadra con lo que tiene en la mano.

El `por_pagar` y el `vencido` del resumen son **solo de MXN**: sumar monedas
distintas daría un total que no significa nada. Las facturas en divisa se ven
renglón por renglón, con su moneda.

## Lo vencido

Se cuenta contra hoy y **solo sobre lo que de verdad falta por pagar**
(`saldoPendiente > 0`). Una factura ya liquidada que venció hace un mes no es un
adeudo vencido: es historia. El campo `vencida` viaja calculado por el ERP para
que el modelo no tenga que restar fechas — que es como acaba diciéndole a
alguien que su pago está al corriente cuando lleva un mes esperando.
