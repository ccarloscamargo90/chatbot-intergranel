# Avisos del ERP por WhatsApp

Los avisos importantes del ERP —lo que vence hoy, lo que ya se venció, un pago
estancado— salen también por WhatsApp, al teléfono de la persona a la que le
tocan, según el calendario y su rol.

Este documento es el **contrato entre los dos repos**: `ERP-INTERGRANEL`
(quien decide qué avisar y a quién) y `chatbot-intergranel` (quien tiene el
número de WhatsApp y la conversación con la persona).

---

## 1. Cómo viaja un aviso

```
Cualquier módulo del ERP
        │  notifications.notifyUser(...) / notifyRoles(...)
        ▼
NotificationsService ──► campanita del ERP (como siempre)
        │
        └──► AvisosWhatsappService        ¿hay regla activa para este `tipo`?
                    │                     ¿el rol del destinatario aplica?
                    │                     ¿el usuario no lo silenció?
                    │                     ¿estamos en su horario?
                    │                     ¿tiene teléfono?
                    ▼
             outbox `avisos_whatsapp`     (fila auditable, con motivo si no sale)
                    │
        AvisosWhatsappWorkerService        cron cada minuto, por empresa
                    │
        BotWebhookService.notifyAviso()
                    │  POST {BOT_WEBHOOK_URL}/webhooks/erp/notificacion
                    ▼
              chatbot-intergranel          dedup por id → bus → WhatsApp
```

El puente se cuelga de la campanita a propósito. Ese ya era el punto único por
el que **todos** los módulos avisan a una persona (calendario, pagos, forwards,
reciba, trazabilidad, cobranza, nómina operativa, almacén). Por eso ninguno de
ellos tuvo que cambiar para ganar el canal: basta registrar su `tipo` en el
catálogo de reglas.

`notifyBroadcast` **no** sale por WhatsApp. Un aviso general mandado al teléfono
de toda la empresa es la forma más rápida de que la gente aprenda a ignorar el
canal; quien lo quiera, lo dirige por rol.

---

## 2. El webhook: `POST /webhooks/erp/notificacion`

**Autenticación:** header `X-Webhook-Secret`, que debe coincidir con
`ERP_WEBHOOK_SECRET` del chatbot (= `BOT_WEBHOOK_SECRET` del ERP).

**Cuerpo:**

```json
{
  "id": "clx123abc",
  "tipo": "calendario.objetivo",
  "telefono": "5215512345678",
  "titulo": "Vence hoy: Declaración IMSS — Intergranel Valle",
  "mensaje": "“Declaración IMSS” de Intergranel Valle se entrega HOY (2026-08-25). Ampara el periodo 2026-07.",
  "url": "https://erp.intergranel.com/calendario",
  "referencia": "calendario_entrega:cle456",
  "empresa": "Intergranel"
}
```

| Campo | Obligatorio | Qué es |
|---|---|---|
| `id` | sí | Id de la fila en la outbox `avisos_whatsapp`. **Es la llave de deduplicación.** |
| `tipo` | sí | El `tipo` del catálogo (`calendario.objetivo`, `pago.estancado`…). |
| `telefono` | sí | E.164 **sin** `+`. Ya viene normalizado por el ERP. |
| `titulo` | sí | Encabezado corto. |
| `mensaje` | sí | Detalle ya redactado por el módulo que avisa. |
| `url` | no | Liga profunda a la pantalla donde se resuelve el pendiente. |
| `referencia` | no | `<prefijo>:<id>` de la entidad que lo originó. |
| `empresa` | no | Quién firma. El ERP corre para varias empresas sobre el mismo código. |

**Respuestas:**

| Status | Cuerpo | Qué hace el ERP |
|---|---|---|
| 200 | `{"status":"sent","wamid":"wamid.ABC","result":{…}}` | Marca `ENVIADO` y guarda el `wamid`. |
| 200 | `{"status":"duplicate"}` | Lo trata como entrega buena y **no** reintenta. |
| 401 | `{"detail":"Secreto inválido"}` | Reintenta con backoff (1 min, 5 min) y a la tercera queda `FALLIDO`. |
| 422 | error de validación | Igual: reintento y `FALLIDO`. |

El `id` se deduplica en el chatbot con el mismo store que los webhooks de Meta.
El worker del ERP reintenta ante timeouts, así que **la misma persona no puede
recibir el mismo vencimiento dos veces**.

---

## 3. La plantilla de Meta — el paso que no es código

Meta solo acepta texto libre dentro de las **24 horas** posteriores al último
mensaje que la persona le mandó al bot. Un vencimiento que se avisa a las 7:30
casi nunca cae dentro de esa ventana: sin plantilla aprobada, el mensaje se
rechaza y en la outbox aparece `FALLIDO` con el error de Meta.

Hay que dar de alta una plantilla en **Meta Business Manager** antes de prender
el puente. Ésta es la que está aprobada y en producción:

- **Dónde:** en la **misma WABA dueña de `WHATSAPP_PHONE_NUMBER_ID`**. Una
  plantilla creada en otro portafolio de negocio no existe para ese número, y
  el envío falla con `#132001` aunque el nombre y el idioma estén bien. Es el
  primer error que conviene descartar, no el último (ver §6).
- **Categoría:** Utility. No Marketing — ver la regla 3 de abajo.
- **Idioma:** `es_MX`.
- **Nombre:** `erp_aviso` → va en `WHATSAPP_AVISO_TEMPLATE`.
- **Encabezado, pie de página y botones:** vacíos. El puente manda **solo
  parámetros de cuerpo**; una variable en el encabezado o un botón hace que
  Meta rechace el envío aunque la plantilla quede aprobada.
- **Cuerpo:**

  ```
  Recordatorio de un pendiente registrado a tu nombre en el sistema:

  Asunto: {{1}}

  Detalle y liga de acceso: {{2}}

  Este recordatorio corresponde a tu cuenta de usuario en {{3}}. Ingresa al sistema para consultar el estado y registrar el avance.
  ```

  | Parámetro | Contenido |
  |---|---|
  | `{{1}}` | `titulo` |
  | `{{2}}` | `mensaje` + la liga, unidos con espacio (Meta no admite saltos de línea en los parámetros) |
  | `{{3}}` | `empresa`, o `COMPANY_NAME` si el aviso no la trae |

### Tres reglas de Meta que rechazan la plantilla al capturarla

Las tres se descubrieron dando de alta la plantilla real, ninguna está en la
documentación de Meta:

1. **Una variable no puede abrir ni cerrar el cuerpo.** Una primera versión
   empezaba con `*{{1}}*` y terminaba con `_{{3}} · ERP_`; Meta la rechazó por
   las dos puntas. Por eso el cuerpo de arriba abre con "Recordatorio de un
   pendiente…" y cierra con la invitación a entrar.
2. **Tiene que haber suficiente texto fijo para tantas variables.** Aquella
   versión eran 3 variables en 29 caracteres y Meta contestó "demasiadas
   variables en relación con su longitud". La de arriba tiene las mismas 3
   sobre ~220 caracteres.
3. **El clasificador de categoría lee SOLO el texto fijo, nunca las
   variables.** Una segunda versión decía "Tienes un pendiente en el ERP: … Entra
   al ERP para ver el detalle"; Meta abrió *"La categoría no coincide"*,
   recomendó Marketing y advirtió que sería rechazada. Tres variables colgando
   de una frase genérica pueden ser cualquier cosa. Se resolvió sin cambiar una
   línea de código, solo con vocabulario de cuenta existente: "Recordatorio",
   "registrado a tu nombre", "tu cuenta de usuario", "consultar el estado".

**Nunca aceptar Marketing para salir del paso**, por más que Meta lo recomiende:
de los mensajes de Marketing la gente puede darse de baja, y Meta les aplica
límites de frecuencia por usuario. Un vencimiento silenciado por una preferencia
de mercadotecnia es exactamente lo que este módulo existe para evitar. Si el
clasificador insiste, se envía como Utility de todas formas —es una heurística
previa, no el revisor— y si el revisor real la rechaza, se apela explicando que
es una notificación interna a empleados sobre pendientes de su cuenta.

Si se cambia el texto, cuidar que el ORDEN de los parámetros no se mueva: es el
que manda `notify_erp_aviso` del chatbot, y Meta además exige que aparezcan en
orden ascendente. Reordenarlos exige tocar código y volver a desplegar;
envolverlos en más texto fijo, no — que es justo como se resolvió la regla 3.

Sin `WHATSAPP_AVISO_TEMPLATE` el chatbot manda **texto libre**. Eso está bien en
desarrollo y para quien tenga una conversación abierta, pero no es la
configuración de producción.

---

## 4. Configuración

### ERP (`ERP-INTERGRANEL`)

```
AVISOS_WHATSAPP_ENABLED=false         # interruptor maestro; default apagado
AVISOS_WHATSAPP_EXIGIR_VERIFICACION=false
AVISOS_WHATSAPP_LADA_DEFAULT=52
AVISOS_WHATSAPP_BATCH=20
AVISOS_PUBLIC_BASE_URL=               # si falta, primer origen de CORS_ORIGINS
BOT_WEBHOOK_URL=https://chatbot.intergranel.com   # ya existía
BOT_WEBHOOK_SECRET=…                              # ya existía
```

`AVISOS_WHATSAPP_ENABLED` viene apagado a propósito: el puente se mergea sin
mandar nada y se prende cuando la plantilla esté aprobada. Un módulo que empieza
a escribirle a la gente el día del deploy no es una función, es un susto.

### Chatbot (`chatbot-intergranel`)

```
ERP_WEBHOOK_SECRET=…                  # = BOT_WEBHOOK_SECRET del ERP
WHATSAPP_AVISO_TEMPLATE=erp_aviso     # vacío = texto libre (dev)
WHATSAPP_TEMPLATE_LANGUAGE=es_MX
```

---

## 5. Quién recibe qué

Dos capas, y las dos tienen que decir que sí:

**El administrador decide qué se manda** — catálogo `aviso_whatsapp_reglas`, en
*Notificaciones → Avisos al equipo*:

- `activo`: si el tipo sale o no por WhatsApp.
- `roles`: a qué roles. **Vacío = a quien ya le tocaba la campanita**, sea cual
  sea su rol (típicamente el responsable nominal del pendiente). Con roles
  listados, el aviso se acota a ellos.
- `horaInicio` / `horaFin`: ventana en hora de México (default 7–21).

**El usuario decide qué tolera** — `usuario_aviso_prefs`, en *Mi perfil →
Avisos por WhatsApp*: interruptor maestro, teléfono propio, tipos silenciados y
su propia ventana horaria.

Un `tipo` sin regla en el catálogo **no sale**. El puente falla cerrado: el
default de un canal intrusivo no puede ser "sale todo".

### Defaults que trae la migración

| Tipo | Encendido | Por qué |
|---|---|---|
| `calendario.objetivo` | ✅ | Vence hoy — es el aviso que salva el día |
| `calendario.atraso` | ✅ | Ya se incumplió; merece interrumpir |
| `calendario.recordatorio` | ✅ | El recordatorio del día |
| `calendario.previo` | ⬜ | Campanita y correo ya cubren la anticipación |
| `calendario.recordatorio_previo` | ⬜ | Igual |
| `pago.*`, `forward.*`, `reciba.*`, `traza.*`, `deuda.*`, `nomina-operativa.*`, `almacen.*` | ⬜ | Registrados y apagados: el admin ve el menú completo y prende lo que le sirva |

---

## 6. "¿Por qué no me llegó?"

Esa pregunta se contesta con un dato, no con una teoría. Cada destinatario deja
una fila en `avisos_whatsapp`, incluso cuando el aviso **no** sale, con el motivo:

| Motivo | Significa |
|---|---|
| `SIN_TELEFONO` | No hay teléfono ni en su preferencia ni en su ficha |
| `OPT_OUT` | El usuario apagó su interruptor maestro |
| `SILENCIADO` | El usuario silenció ese tipo |
| `ROL_NO_APLICA` | La regla acota a otros roles |
| `USUARIO_INACTIVO` | Dado de baja o inactivo |
| `NO_VERIFICADO` | Con la verificación exigida, el teléfono no está confirmado |
| `REGLA_INACTIVA` | La regla se apagó entre el encolado y el envío (pasa con avisos diferidos por horario) |

Se ve en *Notificaciones → Avisos al equipo*, junto con el estado del envío y el
error del proveedor si lo hubo.

Fuera de la ventana horaria el aviso **se difiere, no se tira**: un vencimiento
no deja de vencer porque el cron corrió de madrugada.

### `ENVIADO` no siempre quiere decir entregado

Hay una fila que engaña: **estado `ENVIADO`, sin motivo, sin error — y el
teléfono nunca sonó.** Es la rama de deduplicación del chatbot.

El chatbot marca el `id` del aviso *antes* de intentar el envío, para que dos
entregas concurrentes no le lleguen dos veces a la misma persona. Cuando ya
tiene el `id` responde `{"status":"duplicate"}`, y el ERP lo da por bueno a
propósito: si lo tratara como error, el worker reintentaría para siempre algo
que sí salió. Pero por esa rama no viene `wamid`, así que la fila queda idéntica
a una entrega real.

**Cómo distinguirlas:** una entrega real trae `proveedorMsgId` (el `wamid` de
Meta). Un `ENVIADO` sin id no generó mensaje nuevo. La bandeja ya lo dice con
todas sus letras en *Motivo / error*:

> Sin id de mensaje: el chatbot lo tomó como repetido y no lo volvió a mandar.

Si aparece sin que nadie haya recibido nada, el `id` quedó sellado en Redis por
un intento anterior que falló. Dura lo que su TTL (24 h). Para probar antes,
dispara un aviso **nuevo** — un recordatorio nuevo tiene `id` nuevo — en vez de
esperar el reintento del que quedó enterrado.

### `(#132001) Template name does not exist in the translation`

Meta rechaza el envío y dice el nombre y el idioma que buscó. Se cierra
verificando contra la lista real de plantillas de la WABA:

```bash
curl -s "https://graph.facebook.com/v21.0/<WABA_ID>/message_templates?fields=name,language,status&access_token=<TOKEN>"
```

Tres causas, en el orden en que conviene descartarlas:

1. **El nombre no es el que crees.** Meta normaliza a minúsculas y guiones
   bajos al capturar; `WHATSAPP_AVISO_TEMPLATE` tiene que ser ese nombre
   normalizado, no el que se tecleó en el formulario.
2. **El idioma no coincide.** `es` y `es_MX` son plantillas distintas para Meta.
   `WHATSAPP_TEMPLATE_LANGUAGE` debe ser el código exacto con el que se aprobó.
3. **La plantilla vive en otra WABA.** Si se creó en una cuenta distinta de la
   dueña de `WHATSAPP_PHONE_NUMBER_ID`, no existe para ese número — el error es
   el mismo aunque el nombre y el idioma estén bien.

Si el mismo nombre falla con dos idiomas distintos, ya no es el idioma: es (1)
o (3).

En el primer despliegue fue la (3), y el `curl` la delató de un vistazo: la WABA
del número contestó con **una sola plantilla, `hello_world` en `en_US`**. Esa es
la huella de una cuenta recién creada o del número de prueba que Meta regala con
la app de desarrollador — `erp_aviso` estaba en otro portafolio. Dos apuntes que
se derivan de ahí:

- Si el `curl` devuelve un error de permisos en vez de la lista, no es que la
  plantilla no exista: al token le falta `whatsapp_business_management`.
- Un número de prueba **solo escribe a destinatarios dados de alta a mano**, así
  que no sirve para avisarle al equipo por más que la plantilla esté aprobada.
  La salida es mover el bot al número real, no la plantilla al de pruebas.

---

## 7. Prender el puente en producción

0. **Confirmar que la plantilla está donde el bot la puede ver**, con el `curl`
   de §6 contra la WABA dueña de `WHATSAPP_PHONE_NUMBER_ID`. Tiene que salir
   `erp_aviso · es_MX · APPROVED`. Este paso es el 0 y no el 1 porque saltárselo
   cuesta una tarde: todo lo demás puede estar bien y no llegar nada.
1. En el chatbot: `WHATSAPP_AVISO_TEMPLATE=erp_aviso`,
   `WHATSAPP_TEMPLATE_LANGUAGE=es_MX`, y que `WHATSAPP_TOKEN` /
   `WHATSAPP_PHONE_NUMBER_ID` sean los de **esa** WABA.
2. Confirmar `BOT_WEBHOOK_URL` y que `BOT_WEBHOOK_SECRET` (ERP) y
   `ERP_WEBHOOK_SECRET` (chatbot) coinciden — son el mismo secreto con dos
   nombres. Si difieren, el chatbot responde 401 y el aviso queda `FALLIDO`.
3. Revisar que la gente tenga teléfono capturado: *Usuarios*, o que cada quien
   lo ponga en *Mi perfil*. Ahí mismo se ve a qué número llegarían sus avisos.
   Si sigue la advertencia ámbar, `AVISOS_WHATSAPP_ENABLED` no llegó al backend.
4. Calibrar el catálogo en *Notificaciones → Avisos al equipo*. Los tres tipos
   de calendario vienen activos y sin acotar por rol: le llegan a quien ya
   recibía la campanita.
5. `AVISOS_WHATSAPP_ENABLED=true` y redeploy.
6. Verificar en la bandeja de envíos que los primeros salgan `ENVIADO`
   **con id de mensaje**: un `ENVIADO` sin id no entregó nada (ver §6).

Para probar sin esperar al cron de las 7:30 está `POST /api/v1/calendario/alertas/ejecutar`,
que es idempotente por día: se puede disparar a mano sin inundar a nadie.
