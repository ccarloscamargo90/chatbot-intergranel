# Handoff a un asesor · Chatwoot

Cuando el cliente pide hablar con una persona, el bot se hace a un lado y la
conversación pasa a Chatwoot. De ahí en adelante el bot es un cable: lo que
escribe el cliente entra a la bandeja, lo que escribe el asesor sale por
WhatsApp. Cuando el asesor **resuelve** la conversación, el bot retoma.

---

## 1. Por qué el bot conserva el número

Lo natural con Chatwoot sería apuntarle el webhook de Meta y convertir el bot en
un *Agent Bot*. **No se hizo, y la razón es concreta: Chatwoot no manda mensajes
interactivos por la WhatsApp Cloud API.** Los botones solo salen dentro de una
plantilla aprobada y los menús de lista no salen
([chatwoot#11249](https://github.com/chatwoot/chatwoot/issues/11249),
[chatwoot#12572](https://github.com/chatwoot/chatwoot/issues/12572)).

Todo el autoservicio del cliente —el menú de "Mis pedidos / Mi saldo / Mis
facturas"— está construido sobre esos menús. Cederle el número a Chatwoot habría
cambiado botones por texto plano en **todas** las conversaciones, para ganar
comodidad solo en la fracción que llega a un asesor.

Con el reparto actual, Chatwoot ve completa la conversación escalada y el asesor
trabaja en su bandeja de siempre, sin que el resto del bot pierda nada.

```
                 ┌── el bot atiende ──┐        ┌── el asesor atiende ──┐
Cliente ⇄ Meta ⇄ │      chatbot       │  ⇄     │       Chatwoot        │
                 └────────────────────┘        └───────────────────────┘
                   botones, menús,               texto libre, historial
                   autoservicio                  y contexto del cliente
```

---

## 2. Qué hay que crear en Chatwoot

Todo esto es de una sola vez, en la instancia self-hosted:

1. **Un inbox de tipo API** (Settings → Inboxes → Add Inbox → API).
   No un inbox de WhatsApp: el número lo tiene el bot, no Chatwoot. Este inbox
   es solo el buzón donde aterrizan las conversaciones escaladas.
   Anota su **Inbox ID** (sale en la URL: `/app/accounts/1/settings/inboxes/**7**`).
2. **Un token de API.** Con un *bot token* basta y es lo preferible (Settings →
   Integrations → Agent Bots) porque no ata las acciones a una persona. Si no,
   sirve el `access_token` del perfil de un agente dedicado.
3. **El account ID** — también sale en la URL: `/app/accounts/**1**/…`.
4. **Un webhook** (Settings → Integrations → Webhooks) apuntando a
   `https://<tu-bot>/webhooks/chatwoot?secret=<CHATWOOT_WEBHOOK_SECRET>`,
   suscrito a **`message_created`** y **`conversation_status_changed`**.

### Sobre el secreto en la URL

Chatwoot **no firma** sus webhooks. Sin un secreto compartido, cualquiera que
descubra la URL puede hacerle decir al bot lo que quiera por WhatsApp a nombre de
la empresa — por eso el secreto no es opcional en producción.

El endpoint lo acepta de dos formas: header `X-Webhook-Secret` (preferible) o
query param `?secret=`. La UI de Chatwoot solo deja capturar una URL, así que en
la práctica será el query param, con el costo conocido de que queda escrito en
los logs de acceso del proxy. Si su reverse proxy puede inyectar el header, es
mejor camino; rótelo si el log se comparte.

---

## 3. Variables de entorno (chatbot)

```
CHATWOOT_BASE_URL=https://chatwoot.suempresa.mx   # vacío = deshabilitado
CHATWOOT_API_TOKEN=...
CHATWOOT_ACCOUNT_ID=1
CHATWOOT_INBOX_ID=7
CHATWOOT_WEBHOOK_SECRET=...                       # obligatorio en producción
HANDOFF_TTL_SECONDS=28800                         # 8 h (un turno)
```

`CHATWOOT_BASE_URL=mock` levanta un Chatwoot simulado en memoria, útil para
probar el flujo completo en local sin instancia.

**Sin `CHATWOOT_BASE_URL` el escalamiento falla ruidoso a propósito**: el bot le
dice al cliente que no puede pasarlo con un asesor, en vez de prometerle uno que
nadie avisó. Ese era exactamente el comportamiento anterior a esto — decía "un
asesor continuará en breve" y solo escribía en el log.

---

## 4. El ciclo, paso a paso

| # | Qué pasa | Quién |
|---|---|---|
| 1 | El cliente pide un asesor (escribe o toca 👤 Asesor) | Cliente |
| 2 | Se crea contacto → contact_inbox → conversación `open` | Bot |
| 3 | Se publica una **nota privada** con el contexto | Bot |
| 4 | El teléfono queda en handoff; el bot deja de contestar | Bot |
| 5 | Lo que escriba el cliente entra a la bandeja como `incoming` | Bot |
| 6 | El asesor contesta desde Chatwoot | Asesor |
| 7 | `message_created` → el bot lo manda por WhatsApp | Chatwoot → Bot |
| 8 | El asesor **resuelve** la conversación | Asesor |
| 9 | El bot retoma y le devuelve los botones al cliente | Bot |

### La nota privada (paso 3)

Es lo que evita que el cliente cuente todo otra vez. Lleva el motivo del
escalamiento, el teléfono, la identidad si el cliente se identificó (razón
social y RFC) y los últimos turnos de la conversación. Va como nota **privada**:
el cliente no la ve.

---

## 5. Las cuatro cosas que pueden salir mal

**El eco.** El bot publica en Chatwoot lo que dice el cliente, y Chatwoot avisa
de *cada* mensaje — incluidos los que publicó el bot. Sin filtro, cada mensaje
del cliente le rebotaría de vuelta. Solo se relaya lo que es `outgoing` y **no**
privado.

**El duplicado.** Chatwoot reintenta ante un timeout. Se deduplica por id de
mensaje, y si el envío falla se suelta el candado para que el reintento sirva de
algo.

**La ventana de 24 h.** Si pasaron más de 24 horas desde el último mensaje del
cliente, Meta rechaza el texto libre. El asesor vería su mensaje "entregado" en
Chatwoot sin estarlo, así que el fallo se le escribe **en su propia bandeja**
como nota privada, con el motivo que devolvió Meta. Para reabrir hace falta una
plantilla aprobada.

**El vacío.** Si nadie resuelve la conversación, el cliente se quedaría
hablándole a nadie para siempre. Por eso el handoff vence
(`HANDOFF_TTL_SECONDS`) y el bot retoma. Y si un mensaje no logra llegar a la
bandeja, se le dice al cliente en vez de dejarlo creyendo que alguien lo lee.

---

## 6. Lo que se pierde

**Los archivos no se suben a Chatwoot.** Si el cliente manda una foto o un PDF
durante el handoff, en la bandeja aparece `[el cliente envió un archivo: image]`
con su pie de foto, pero no el archivo. El asesor sabe que existe y puede
pedírselo. Subirlo requiere multipart contra la API de Chatwoot; es el siguiente
paso natural si resulta que estorba.
