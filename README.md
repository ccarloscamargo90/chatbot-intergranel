# Asistente de WhatsApp con IA · Intergranel

Asistente conversacional con IA (Claude) para **Intergranel**, comercializadora
de granos y commodities a granel. Hace dos cosas:

1. **Atención al cliente por WhatsApp** — responde dudas sobre órdenes de
   compra, fechas de entrega, productos y montos, consultando el ERP mediante
   herramientas (tools). Escala con un asesor humano cuando hace falta.
2. **Notificaciones proactivas** — cuando el ERP reporta un cambio de estado en
   una orden, el bot avisa al cliente por WhatsApp.

## Arquitectura

```
Cliente WhatsApp ─▶ POST /webhooks/whatsapp ─▶ Claude (+ tools) ─▶ ERP ─▶ respuesta ─▶ WhatsApp
ERP (evento de orden) ─▶ POST /webhooks/erp/order-update ─▶ plantilla/texto WhatsApp ─▶ Cliente
```

- **Backend:** Python + FastAPI
- **IA:** Claude API (`claude-opus-4-8` por defecto), con tool use y prompt caching
- **WhatsApp:** WhatsApp Cloud API de Meta
- **Órdenes:** ERP/API externo (con un *mock* en memoria para desarrollar)

| Archivo | Responsabilidad |
|---|---|
| `app/main.py` | App FastAPI y endpoints (webhooks) |
| `app/assistant.py` | Integración con Claude + herramientas |
| `app/whatsapp.py` | Cliente de la WhatsApp Cloud API |
| `app/erp.py` | Cliente del ERP (HTTP real + mock) |
| `app/notifications.py` | Notificaciones proactivas de órdenes |
| `app/config.py` | Configuración por variables de entorno |
| `app/models.py` | Modelos de datos |

## Puesta en marcha (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # completa al menos ANTHROPIC_API_KEY
uvicorn app.main:app --reload
```

Sin `ERP_BASE_URL` ni `WHATSAPP_TOKEN`, el servicio arranca en **modo
desarrollo**: usa un ERP simulado (órdenes `OC-1001`, `OC-1002`) y registra los
mensajes de WhatsApp en consola en vez de enviarlos.

### Probar la atención al cliente sin WhatsApp

```bash
curl -X POST http://localhost:8000/webhooks/whatsapp \
  -H "Content-Type: application/json" \
  -d '{"entry":[{"changes":[{"value":{"messages":[
        {"from":"5215512345678","type":"text","text":{"body":"¿Cómo va mi orden OC-1001?"}}
      ]}}]}]}'
```

La respuesta del asistente aparecerá en los logs (modo desarrollo).

### Probar una notificación de orden

```bash
curl -X POST http://localhost:8000/webhooks/erp/order-update \
  -H "Content-Type: application/json" \
  -d '{"order_id":"OC-1001","telefono":"5215512345678","estado_nuevo":"en_ruta","cliente":"Molinos del Bajío"}'
```

## Conectar WhatsApp Cloud API

1. Crea una app en [Meta for Developers](https://developers.facebook.com/) con
   el producto **WhatsApp**.
2. Obtén el **token de acceso** (idealmente de un *system user*, permanente) y
   el **Phone Number ID**; ponlos en `WHATSAPP_TOKEN` y
   `WHATSAPP_PHONE_NUMBER_ID`.
3. Configura el webhook apuntando a `https://TU_DOMINIO/webhooks/whatsapp` con
   el *verify token* de `WHATSAPP_VERIFY_TOKEN`, y suscríbete al campo
   `messages`.
4. Para notificaciones proactivas (fuera de la ventana de 24h) necesitas una
   **plantilla aprobada**; pon su nombre en `WHATSAPP_ORDER_TEMPLATE`.

> Para exponer tu servidor local durante pruebas puedes usar un túnel
> (p. ej. `ngrok http 8000`).

## Conectar tu ERP

El bot espera un contrato REST sencillo (ajustable en `app/erp.py`):

- `GET {ERP_BASE_URL}/orders/{id}` → una orden (JSON)
- `GET {ERP_BASE_URL}/orders?telefono={tel}` → lista de órdenes (JSON)

El formato de cada orden es el del modelo `Order` (ver `app/models.py`).
Para disparar notificaciones, tu ERP debe hacer `POST` a
`/webhooks/erp/order-update` con el header `X-Webhook-Secret` (si configuras
`ERP_WEBHOOK_SECRET`).

## Despliegue en Railway

El proyecto incluye configuración lista para [Railway](https://railway.com):

- `railway.toml` — builder Nixpacks, comando de arranque y healthcheck en `/`.
- `Procfile` — proceso web (`uvicorn`) como respaldo.
- `.python-version` — fija Python 3.11.

El servidor escucha en `0.0.0.0:$PORT` (Railway inyecta `$PORT`
automáticamente; no lo configures a mano).

### Pasos

1. Crea un proyecto en Railway → **Deploy from GitHub repo** y elige este repo
   (rama `main`). Railway detecta Python e instala `requirements.txt`.
2. En **Variables**, define al menos:
   - `ANTHROPIC_API_KEY`
   - `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`
   - `ERP_BASE_URL`, `ERP_API_KEY` (omítelas para usar el ERP simulado)
   - `ERP_WEBHOOK_SECRET`, y `WHATSAPP_ORDER_TEMPLATE` si usas plantilla
   - (opcional) `CLAUDE_MODEL` para cambiar de modelo
   > La app arranca aunque falten variables: el healthcheck `/` responde y los
   > clientes se inicializan al primer uso. Configura las claves antes de
   > recibir tráfico real.
3. En **Settings → Networking**, genera un dominio público
   (`https://<tu-app>.up.railway.app`).
4. Configura los webhooks con ese dominio:
   - WhatsApp (Meta): `https://<tu-app>.up.railway.app/webhooks/whatsapp`
   - ERP (notificaciones): `https://<tu-app>.up.railway.app/webhooks/erp/order-update`

### Con Railway CLI (opcional)

```bash
npm i -g @railway/cli
railway login
railway init           # o: railway link  (a un proyecto existente)
railway up             # despliega el código actual
railway variables --set ANTHROPIC_API_KEY=sk-ant-...
```

## Notas

- El historial de conversación se guarda **en memoria** por número. Para
  producción, muévelo a Redis o una base de datos.
- Ejecuta detrás de HTTPS (Meta lo exige para webhooks). Railway provee HTTPS
  en el dominio generado.
