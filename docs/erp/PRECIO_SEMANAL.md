# Precio de venta semanal, dictado por WhatsApp

Cada sábado a las 7:00 (hora de México) el ERP le pregunta al responsable el
precio de venta de la semana siguiente. Él contesta por WhatsApp, el bot le
confirma cada cifra con botones, y el lunes a las 00:00 el precio entra en vigor
en el ERP, que lo publica al CRM, de donde el chatbot cotiza.

Este documento es el **contrato entre los dos repos**: `ERP-INTERGRANEL` (quien
decide a quién, qué productos y cuándo entra en vigor) y `chatbot-intergranel`
(quien tiene la conversación).

---

## 1. El recorrido

```
Sábado 7:00 (México)          ERP · PrecioSemanalScheduler
        │  abre la pregunta de la semana siguiente (una por semana)
        │  NotificationsService.notifyUser(responsable, tipo "precios.semanal_solicitud")
        ▼
Puente de avisos ─► plantilla de Meta ─► WhatsApp del responsable
        │                                         (el bot marca su teléfono)
        ▼
Responsable: "Buenos días"
Bot: 💲 Maíz blanco grado 1 — hoy $6,950.00 por tonelada.     [🟰 Mismo precio] [⏸️ Después]
Responsable: "7,050"
Bot: ¿Confirmo Maíz blanco grado 1 a $7,050.00 desde el lunes 5 de oct?  [✅ Confirmar] [✏️ Corregir]
Responsable: ✅ Confirmar ─► POST /bot/precio-semanal/respuesta  (queda PROGRAMADO)
        ▼
Domingo 10:00                 recordatorio, solo si falta algo
Lunes 00:00                   PROGRAMADO ─► lista predeterminada ─► CRM ─► chatbot
```

## 2. En el ERP

*Inventario › Precio de venta › Precio semanal por WhatsApp › Configurar*:

- **Preguntar cada sábado** (apagado de fábrica).
- **Quién dicta el precio**: solo aparecen quienes pueden cambiar precios
  (`inventario:UPDATE`). Por WhatsApp nadie gana un permiso que la pantalla no
  le da. La respuesta solo se acepta desde **su** teléfono (el de *Mi perfil*).
- **De qué productos**: una casilla por producto (`Producto.pedirPrecioSemanal`).

"Mandar ya" dispara la pregunta sin esperar al sábado (idempotente por semana).

Lo que se dicta se escribe en la **lista predeterminada** (`PrecioProducto`)
como precio "puesto a mano": la fórmula (flete, merma, margen) no se toca.

Solo aplica a empresas cuyo catálogo publica esa lista (hoy, granos). En
MegaCostales y GRANCORE el precio sale de su tarifario propio y la pantalla no
deja prenderlo: el precio no llegaría al CRM. Lo declara cada estrategia de
catálogo (`precioDesdeListaPredeterminada`).

### Estados de cada producto

| Estado | Qué pasó |
|---|---|
| `PENDIENTE` | Aún no contesta |
| `PROGRAMADO` | Dictó precio (o "mismo precio"); espera el lunes 00:00 |
| `APLICADO` | Ya está en la lista y se publicó al CRM |
| `OMITIDO` | "No cambiar": sigue el precio anterior |
| `SIN_RESPUESTA` | Terminó la semana sin respuesta: siguió el anterior |

Una respuesta tardía (ya empezada la semana) entra en vigor en el momento. Lo
programado se aplica en un cron de cada 15 minutos, no una sola vez el lunes: si
el servidor se reinicia a las 00:00, el precio entra un cuarto de hora tarde, no
una semana tarde.

## 3. El webhook de salida (ERP → bot)

Es el MISMO `POST /webhooks/erp/notificacion` de los avisos internos (ver
`AVISOS_WHATSAPP.md`), con:

```json
{
  "id": "aviso-…",
  "tipo": "precios.semanal_solicitud",
  "telefono": "524421234567",
  "titulo": "Precio de venta de la semana del 5 al 11 de oct",
  "mensaje": "Conteste este mensaje y le pregunto uno por uno el precio por tonelada de: Maíz blanco grado 1 (hoy $6,950.00); Maíz amarillo grado 2 (hoy $6,500.00). Entra en vigor el lunes 5 de oct a las 00:00.",
  "referencia": "precio-semanal:<solicitudId>",
  "empresa": "Intergranel"
}
```

El recordatorio del domingo trae el mismo `tipo`. El bot marca el teléfono
**después** de entregar el aviso: si Meta lo rechazó, no hay pregunta abierta.

Requisitos: `AVISOS_WHATSAPP_ENABLED=true` en el ERP y la plantilla `erp_aviso`
aprobada (a las 7:00 casi nadie tiene la ventana de 24 h abierta). La regla
`precios.semanal_solicitud` la crea la migración 238, activa.

## 4. Los endpoints de entrada (bot → ERP)

Ambos con `X-Bot-Api-Key`. El teléfono va en el **cuerpo**, no en la URL.

### `POST /api/v1/bot/precio-semanal/pendientes`

```json
{ "telefono": "5214421234567" }
```

```json
{
  "pendientes": [
    {
      "empresa": "intergranel",
      "empresaNombre": "Intergranel",
      "solicitudId": "…",
      "productoId": "…",
      "producto": "Maíz blanco grado 1",
      "unidad": "tonelada",
      "semana": "del 5 al 11 de oct",
      "desde": "2026-10-05",
      "hasta": "2026-10-11",
      "vigenteDesde": "2026-10-05T06:00:00.000Z",
      "precioActualCentavos": "695000"
    }
  ]
}
```

Recorre **todas** las empresas: el mismo responsable puede dictar el precio de
más de una, y `empresa` (el slug) es lo que devuelve la respuesta a la BD
correcta. Un teléfono que no es el del responsable recibe la lista vacía, igual
que uno que no existe: no se le confirma a nadie quién dicta los precios.

### `POST /api/v1/bot/precio-semanal/respuesta`

```json
{
  "empresa": "intergranel",
  "solicitudId": "…",
  "productoId": "…",
  "telefono": "5214421234567",
  "accion": "precio",
  "precioCentavos": 705000,
  "wamid": "wamid.…",
  "texto": "7,050"
}
```

`accion`: `precio` (con `precioCentavos`), `mismo` (el publicado hoy) u `omitir`.
Se manda solo lo que el responsable **confirmó** en el chat.

| `motivo` (con `registrado: false`) | Qué pasó |
|---|---|
| `no_autorizado` | No es el teléfono del responsable, perdió el permiso o la empresa no existe |
| `no_encontrado` | Ese producto no está en esa pregunta |
| `solicitud_cerrada` | Esa semana ya terminó |
| `ya_aplicado` | Ya está en vigor; cambiarlo es desde la pantalla |
| `precio_invalido` | Cero, negativo o fuera de rango |
| `sin_precio_actual` | "Mismo precio" de un producto que no tiene precio publicado |

## 5. El "1" de los celulares de México

WhatsApp puede entregar un celular mexicano como `521` + 10 dígitos aunque en el
ERP esté capturado como `52` + 10. Los dos lados comparan con esa forma
normalizada (`telefonoComparable` en el ERP, `telefono_comparable` en el bot).
