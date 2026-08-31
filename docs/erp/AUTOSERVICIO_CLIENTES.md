# Autoservicio del cliente por WhatsApp — contrato ERP ↔ chatbot

El cliente le escribe al bot y quiere ver **lo suyo**: sus pedidos, sus
contratos, sus facturas, cuánto debe y qué trae vencido. Para eso hay que saber
quién es, y del otro lado de WhatsApp no hay usuario del ERP ni contraseña: hay
un teléfono y lo que la persona teclee.

Este documento es el contrato entre los dos repos. La implementación del lado
del ERP vive en `apps/backend/src/modules/bot/` (`bot-identidad.service.ts`,
`bot-clientes.service.ts`); la del bot, en `app/agents/soporte.py`,
`app/sesiones.py` y `app/erp.py`.

---

## 1. Qué tan fuerte es esta identificación (léase antes de subirlo)

**El RFC de una empresa va impreso en cada factura que emite.** Identifica, pero
no es un secreto: cualquiera que haya recibido un CFDI de ese cliente lo tiene,
y con él normalmente también su razón social. Conviene decirlo claro para que
nadie confunda esto con un login.

Lo que convierte "nombre + RFC" en un candado utilizable son cuatro cosas
juntas:

1. **Hay que acertar los DOS datos.** Un RFC suelto no abre nada.
2. **Los intentos se cuentan por teléfono y se bloquean** (5 en 15 minutos, por
   defecto). Probar a ciegas cuesta tiempo real, no milisegundos.
3. **Todo intento queda en bitácora** (`bot_accesos_cliente`): qué se tecleó,
   desde qué número, con qué resultado. Si alguien anda tocando puertas, hay
   dónde verlo.
4. **El fallo nunca delata.** No dice cuál de los dos datos falló ni si el RFC
   existe. Si lo dijera, el bot sería un verificador de RFCs y bastaría con
   probar nombres hasta dar con uno.

Además, los **RFC genéricos del SAT** (`XAXX010101000`, `XEXX010101000`) se
rechazan de entrada: se repiten entre clientes, así que aceptarlos sería dejar
una llave maestra puesta.

Qué NO protege esto: a un cliente cuyo teléfono de empresa quedó en manos
equivocadas dentro de la ventana de sesión (por eso la sesión dura 30 minutos y
hay un botón de cerrar sesión), ni contra alguien que tenga una factura de ese
cliente en la mano. Si en algún momento se quiere subir el listón, el siguiente
escalón natural es un código de un solo uso al correo que el ERP ya tiene en
`Cliente.email` — el flujo de aquí no cambia, solo se le agrega un paso.

---

## 2. Autenticación de los endpoints

Dos capas, y hacen cosas distintas:

| Capa | Header | Qué prueba |
|---|---|---|
| API key del bot | `X-Bot-Api-Key: <BOT_API_KEY>` | Que quien llama es el chatbot |
| Sesión del cliente | `X-Bot-Sesion: <token>` | De qué cliente son los datos |

La segunda es la que importa aquí. **El token viaja en un header, no en la
URL**: los path params acaban escritos en los logs de acceso del proxy, y ahí
quedaría el pase de entrada a los datos de un cliente.

**Ningún endpoint del autoservicio recibe un id de cliente.** El "de quién" sale
siempre de la sesión. Así no hay manera —ni por un bug del bot, ni por un prompt
torcido— de pedir la cartera de otro.

---

## 3. Identificación

```
POST /api/v1/bot/clientes/identificar
X-Bot-Api-Key: <BOT_API_KEY>

{ "nombre": "Molinos del Bajío", "rfc": "MBA950101AB1", "telefono": "5215512345678" }
```

`telefono` no es un dato del cliente: es el **origen** del intento. Sin él no
hay a quién contarle los intentos fallidos ni a quién bloquear.

**Siempre responde 200.** Un "no coincide" es una respuesta de negocio que el
bot tiene que contar con tacto, no un error de transporte.

Éxito:

```json
{
  "encontrado": true,
  "cliente": "Molinos del Bajío",
  "razon_social": "MOLINOS DEL BAJÍO, S.A. de C.V.",
  "rfc": "MBA950101AB1",
  "token": "…",
  "expira_en_segundos": 1800
}
```

Fallo:

```json
{ "encontrado": false, "motivo": "no_coincide", "intentos_restantes": 3, "espera_minutos": null }
```

| `motivo` | Qué pasó | Qué dice el bot |
|---|---|---|
| `no_coincide` | El par nombre+RFC no corresponde a ningún cliente activo | "Los datos no coinciden." Nada más |
| `rfc_invalido` | El RFC no tiene forma de RFC | Pide que lo revise |
| `rfc_generico` | RFC genérico del SAT | Pide el RFC propio de la empresa |
| `bloqueado` | Demasiados intentos fallidos desde ese teléfono | Cuántos minutos esperar + ofrecer asesor |

`no_coincide` es **el mismo motivo** si falló el RFC, si falló el nombre o si el
RFC no existe. Es deliberado (ver §1.4).

### Cómo se compara el nombre

El cliente teclea en un celular, con prisa. La comparación
(`bot-identidad.util.ts`) afloja en lo cosmético —mayúsculas, acentos,
puntuación, `S.A. de C.V.`, espacios de más— y aprieta en lo que distingue: cada
palabra significativa que escribió tiene que estar en el nombre registrado. Se
compara contra `nombre`, `razonSocial` y `contactoNombre`, así que sirve tanto
el nombre de la empresa como el de la persona.

Aceptado: `molinos del bajio`, `Molinos Bajío`, `Bajío Molinos`, `Molinos`.
Rechazado: `Harinera del Norte`, `Molinos del Sur`, `S.A. de C.V.`.

Si dos clientes activos comparten RFC **y** nombre, no se identifica a ninguno:
decir "hay varios" ya sería confirmar que el RFC existe.

---

## 4. Consultas (todas con `X-Bot-Sesion`)

| Endpoint | Devuelve |
|---|---|
| `GET /api/v1/bot/clientes/resumen` | `CustomerSummary` |
| `GET /api/v1/bot/clientes/deuda` | `CustomerDebt` |
| `GET /api/v1/bot/clientes/contratos` | `Order[]` (el mismo DTO de `/bot/ordenes`) |
| `GET /api/v1/bot/clientes/pedidos` | `CustomerOrder[]` |
| `GET /api/v1/bot/clientes/facturas` | `CustomerInvoice[]` |
| `GET /api/v1/bot/clientes/documentos/{tipo}?folio=` | El archivo crudo (bytes) |
| `POST /api/v1/bot/clientes/cerrar-sesion` | `{ "cerrada": true }` |

### Documentos

`tipo` es uno de `factura` · `factura_xml` · `contrato` · `estado_de_cuenta`.
Los tres primeros exigen `?folio=` y **se buscan entre los del cliente de la
sesión**, nunca contra el catálogo global: los folios son consecutivos, así que
acertar uno ajeno sería contar, no adivinar. Un folio que no es suyo devuelve
404 con el mismo mensaje que uno inexistente.

El `estado_de_cuenta` no lleva folio: se genera al vuelo con los mismos números
de `CarteraLecturaService`, para que el PDF y lo que dijo el bot por chat cuadren
renglón por renglón.

**Se devuelven los BYTES, no una URL.** El bot los sube a la Media API de Meta y
manda el documento por `media_id`, así que en ningún momento existe un enlace
—ni firmado, ni de cinco minutos— desde el que se pueda bajar el CFDI de un
cliente. Es también la razón de que el endpoint no reutilice
`StorageService.getSignedUrl`, que sí se usa para los correos.

Un archivo cuya URL guardada no apunte al bucket del ERP no se sirve
(`keyFromUrl` devuelve null): si no, el endpoint sería un proxy de salida hacia
donde alguien haya escrito esa columna.

**401** significa una sola cosa: la sesión expiró o se cerró. El bot lo traduce
a "su sesión caducó por seguridad, identifíquese de nuevo" y borra su copia
local del token — no a "hubo un problema técnico".

Montos **en pesos** (no centavos) y cantidades **en toneladas**, igual que el
resto de los DTOs del bot.

```jsonc
// CustomerDebt
{
  "cliente": "MOLINOS DEL BAJÍO, S.A. de C.V.",
  "moneda": "MXN",
  "saldo": 204500.0,          // total, sobre TODOS los renglones
  "saldo_vencido": 112500.0,
  "lineas": [{
    "tipo": "FACTURA",        // FACTURA | MANUAL | CREDITO
    "folio": "FACT-2026-0031",
    "concepto": "Factura FACT-2026-0031",
    "fecha": "2026-04-30T12:00:00.000Z",
    "fecha_vencimiento": "2026-05-30T12:00:00.000Z",
    "dias_vencido": 12,
    "vencida": true,
    "importe": 185000.0, "cobrado": 85000.0, "saldo": 100000.0,
    "estado": "parcialmente_cobrada"
  }]
}
```

Dos detalles de `lineas` que no son obvios:

- **Se listan hasta 25 renglones**, del más reciente al más viejo. Un WhatsApp
  con ochenta facturas no lo lee nadie.
- **Los TOTALES se calculan sobre todo, no sobre lo listado.** El saldo nunca
  sale recortado por el tope de renglones.
- No se listan los renglones ya liquidados; las notas de crédito sí, porque
  explican por qué el saldo bajó sin que entrara dinero.

El saldo lo produce `CarteraLecturaService`, **el mismo cálculo que alimenta la
pantalla de cartera del ERP**. Si el bot sumara por su cuenta, tarde o temprano
le diría al cliente un número distinto del que ve el cobrador, y esa diferencia
se descubre en una llamada incómoda, no en una prueba.

---

## 5. Ciclo de vida de la sesión

- Dura **30 minutos** (`BOT_CLIENTE_SESION_MINUTOS`). Usarla **no** la renueva:
  un WhatsApp de empresa se presta, se queda abierto en la tablet de la báscula,
  se hereda al que entra.
- **Identificarse revoca las sesiones anteriores de ese teléfono.** Una
  conversación, una sesión viva.
- El token se guarda **hasheado** (SHA-256). Una copia de la base de datos no
  entrega las sesiones vivas.
- Cerrar sesión es **idempotente**: cerrar una ya muerta es justo el resultado
  buscado.

---

## 6. Variables de entorno (ERP)

Todas opcionales; sin ellas rigen los defaults.

| Variable | Default | Para qué |
|---|---|---|
| `BOT_CLIENTE_MAX_INTENTOS` | `5` | Intentos fallidos por teléfono antes de bloquear |
| `BOT_CLIENTE_BLOQUEO_MINUTOS` | `15` | Duración del bloqueo y ventana en que se cuentan |
| `BOT_CLIENTE_SESION_MINUTOS` | `30` | Vida de la sesión |

Los topes de `validation.ts` (máx. 20 intentos, máx. 8 h de sesión) existen para
atajar el error de configuración que anularía el candado sin que nadie lo note.

---

## 7. Auditoría

`bot_accesos_cliente` guarda una fila por intento: teléfono, RFC y nombre
tecleados, resultado (`OK` · `NO_COINCIDE` · `RFC_GENERICO` · `RFC_INVALIDO` ·
`BLOQUEADO`) y, si acertó, el cliente. De ahí sale el bloqueo, y de ahí sale la
respuesta a "¿quién anduvo intentando entrar como este cliente?".

`bot_sesiones_cliente` guarda las sesiones: hash del token, cliente, teléfono,
caducidad, revocación y último uso. Una sesión revocada no se borra — la
constancia de cuándo dejó de valer es parte de la bitácora.
