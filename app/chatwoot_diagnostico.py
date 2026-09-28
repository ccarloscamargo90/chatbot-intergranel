"""¿Quedó bien conectado Chatwoot? La respuesta, en palabras.

Configurar el handoff son siete datos repartidos entre Railway y el panel de
Chatwoot (dirección, token, cuenta, bandeja, secreto, webhook y sus eventos), y
cuando uno está mal el síntoma es siempre el mismo: el cliente pide un asesor y
recibe el teléfono de respaldo. El log dice "401" y nada más. Quien configura no
tiene por qué saber leer eso.

Este diagnóstico hace las MISMAS llamadas que hace el bot al escalar —con el
mismo token— y dice cuál falló y qué hacer. Lo que no se puede comprobar se dice
como tal, nunca como un ✅.

Tres cosas que aprendimos del código de Chatwoot v4.17.0 y que se revisan aquí:

- **Un token de Agent Bot no sirve.** `AccessTokenAuthHelper` solo deja a los
  bots crear conversaciones y mensajes; buscar y crear contactos —lo primero que
  hace el bot— responde 401 "Access to this endpoint is not authorized for
  bots". Hace falta el token de un usuario.
- **Un agente solo ve sus bandejas** (`InboxPolicy#show?` → `assigned_inboxes`),
  y solo un administrador puede listar webhooks (`WebhookPolicy`). Por eso se
  recomienda el token de un administrador.
- **La bandeja tiene que ser de tipo API.** El número de WhatsApp lo conserva el
  bot; una bandeja de WhatsApp en Chatwoot intentaría mandar por su cuenta.

Nunca imprime el token ni el secreto: la página se puede compartir en una
captura de pantalla.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from .config import Settings
from .errores import detalle_respuesta
from .handoff import asesor_de_respaldo

#: Eventos a los que el webhook tiene que estar suscrito: la respuesta del
#: asesor y el "resolver" que le devuelve el cliente al bot.
EVENTOS_REQUERIDOS = ("message_created", "conversation_status_changed")

#: Un teléfono que no es de nadie: buscarlo prueba el permiso sin tocar datos.
_BUSQUEDA_DE_PRUEBA = "+520000000000"

OK, FALLA, AVISO = "ok", "falla", "aviso"
_ICONO = {OK: "✅", FALLA: "❌", AVISO: "⚠️"}


@dataclass(frozen=True)
class Chequeo:
    estado: str  # OK | FALLA | AVISO
    texto: str


def como_texto(chequeos: list[Chequeo]) -> str:
    """La página que ve quien configura: una línea por revisión y un veredicto."""
    fallas = sum(1 for c in chequeos if c.estado == FALLA)
    lineas = ["Diagnóstico de Chatwoot", ""]
    lineas += [f"{_ICONO[c.estado]} {c.texto}" for c in chequeos]
    lineas.append("")
    if fallas:
        lineas.append(
            f"RESULTADO: ❌ falta corregir {fallas} cosa(s). Mientras tanto, quien pida un "
            "asesor recibe el teléfono de respaldo."
        )
    else:
        lineas.append(
            "RESULTADO: ✅ listo. Pruébelo: desde WhatsApp toque «👤 Asesor» y la "
            "conversación debe aparecer en la bandeja de Chatwoot."
        )
    return "\n".join(lineas) + "\n"


async def diagnosticar(
    settings: Settings,
    host_publico: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Chequeo]:
    """Revisa la configuración de punta a punta. Nunca lanza: todo es un chequeo."""
    chequeos: list[Chequeo] = []
    base = (settings.chatwoot_base_url or "").strip().rstrip("/")

    if not base:
        chequeos.append(
            Chequeo(
                FALLA,
                "CHATWOOT_BASE_URL está vacía: el bot ni siquiera intenta pasar al "
                "cliente a Chatwoot. Ponga la dirección con la que abre su Chatwoot en el "
                "navegador (https://…, sin / al final).",
            )
        )
        chequeos.append(_respaldo())
        return chequeos
    if base == "mock":
        chequeos.append(
            Chequeo(
                FALLA,
                "CHATWOOT_BASE_URL=mock: es el Chatwoot SIMULADO de desarrollo. Ningún "
                "asesor ve esas conversaciones. Ponga la dirección real.",
            )
        )
        return chequeos
    if not base.startswith(("https://", "http://")):
        chequeos.append(
            Chequeo(FALLA, f"CHATWOOT_BASE_URL no empieza con https:// ({base}).")
        )
        return chequeos
    chequeos.append(Chequeo(OK, f"Dirección de Chatwoot: {base}"))

    faltan = [
        nombre
        for nombre, valor in (
            ("CHATWOOT_API_TOKEN", settings.chatwoot_api_token),
            ("CHATWOOT_ACCOUNT_ID", settings.chatwoot_account_id),
            ("CHATWOOT_INBOX_ID", settings.chatwoot_inbox_id),
        )
        if not valor
    ]
    if faltan:
        chequeos.append(Chequeo(FALLA, f"Faltan variables en Railway: {', '.join(faltan)}."))
        chequeos.append(_respaldo())
        return chequeos

    cuenta = settings.chatwoot_account_id
    bandeja = settings.chatwoot_inbox_id
    api = f"{base}/api/v1"
    async with httpx.AsyncClient(
        timeout=15,
        headers={"api_access_token": settings.chatwoot_api_token},
        transport=transport,
    ) as http:
        # 1. ¿Chatwoot contesta, y de quién es el token?
        perfil = await _get(http, f"{api}/profile")
        if isinstance(perfil, str):
            chequeos.append(
                Chequeo(
                    FALLA,
                    f"No se pudo conectar con {base} ({perfil}). Revise que sea la dirección "
                    "de su Chatwoot y que el servicio esté en verde en Railway.",
                )
            )
            chequeos.append(_respaldo())
            return chequeos
        if perfil.status_code == 401:
            chequeos.append(Chequeo(FALLA, _token_rechazado(perfil)))
            chequeos.append(_respaldo())
            return chequeos
        if perfil.status_code != 200 or not _es_json(perfil):
            chequeos.append(
                Chequeo(
                    FALLA,
                    f"{base} no responde como Chatwoot ({perfil.status_code}). ¿Es la "
                    "dirección correcta?",
                )
            )
            chequeos.append(_respaldo())
            return chequeos

        datos = perfil.json()
        nombre = datos.get("name") or datos.get("available_name") or "(sin nombre)"
        membresia = next(
            (a for a in datos.get("accounts") or [] if a.get("id") == cuenta), None
        )
        if membresia is None:
            disponibles = ", ".join(
                f"{a.get('id')} ({a.get('name')})" for a in datos.get("accounts") or []
            )
            chequeos.append(
                Chequeo(
                    FALLA,
                    f"El token es de {nombre}, pero ese usuario no pertenece a la cuenta "
                    f"{cuenta}. CHATWOOT_ACCOUNT_ID debe ser una de: {disponibles or 'ninguna'}.",
                )
            )
            chequeos.append(_respaldo())
            return chequeos

        administrador = membresia.get("role") == "administrator"
        if administrador:
            chequeos.append(
                Chequeo(
                    OK,
                    f"Token válido: es de {nombre}, administrador de la cuenta "
                    f"{cuenta} ({membresia.get('name')}).",
                )
            )
        else:
            chequeos.append(
                Chequeo(
                    AVISO,
                    f"El token es de {nombre}, que es AGENTE (no administrador) de la cuenta "
                    f"{cuenta}. Funciona solo si es miembro de la bandeja, y no deja revisar "
                    "el webhook. Mejor use el token de un administrador.",
                )
            )

        # 2. La bandeja: que exista, que la vea y que sea de tipo API.
        inbox = await _get(http, f"{api}/accounts/{cuenta}/inboxes/{bandeja}")
        if isinstance(inbox, str) or inbox.status_code != 200:
            chequeos.append(Chequeo(FALLA, await _bandeja_invisible(http, api, cuenta, inbox)))
        else:
            info = inbox.json()
            tipo = info.get("channel_type")
            if tipo == "Channel::Api":
                chequeos.append(
                    Chequeo(OK, f"Bandeja {bandeja} «{info.get('name')}», de tipo API.")
                )
            else:
                chequeos.append(
                    Chequeo(
                        FALLA,
                        f"La bandeja {bandeja} «{info.get('name')}» es de tipo {tipo}; tiene "
                        "que ser de tipo API (Ajustes → Bandejas de entrada → Agregar → API).",
                    )
                )

        # 3. Lo primero que hace el bot al escalar: buscar el contacto.
        busqueda = await _get(
            http,
            f"{api}/accounts/{cuenta}/contacts/search",
            params={"q": _BUSQUEDA_DE_PRUEBA},
        )
        if not isinstance(busqueda, str) and busqueda.status_code == 200:
            chequeos.append(Chequeo(OK, "El token puede buscar y crear contactos."))
        else:
            motivo = busqueda if isinstance(busqueda, str) else _detalle(busqueda)
            chequeos.append(Chequeo(FALLA, f"El token no puede buscar contactos ({motivo})."))

        # 4. El camino de vuelta: el webhook que trae la respuesta del asesor.
        chequeos += await _webhook(http, api, settings, host_publico, administrador)

    chequeos.append(_respaldo())
    return chequeos


# --- Piezas ------------------------------------------------------------------ #


async def _get(http: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response | str:
    """La respuesta, o por qué no hubo respuesta."""
    try:
        return await http.get(url, **kwargs)
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _es_json(resp: httpx.Response) -> bool:
    try:
        return isinstance(resp.json(), dict)
    except ValueError:
        return False


def _detalle(resp: httpx.Response) -> str:
    return detalle_respuesta(resp, "Chatwoot")


def _token_rechazado(resp: httpx.Response) -> str:
    detalle = _detalle(resp)
    if "not authorized for bots" in detalle:
        return (
            "El token es de un AGENT BOT. Chatwoot no le deja buscar ni crear contactos, "
            "así que el bot nunca puede abrir la conversación. Use el token de un usuario "
            "administrador: en Chatwoot, su foto (abajo a la izquierda) → Configuración "
            "del perfil → Token de acceso."
        )
    return (
        f"Chatwoot rechazó el token ({detalle}). Cópielo otra vez desde Configuración del "
        "perfil → Token de acceso, sin espacios."
    )


async def _bandeja_invisible(
    http: httpx.AsyncClient, api: str, cuenta: int, resp: httpx.Response | str
) -> str:
    """Por qué no se ve la bandeja, con la lista de las que SÍ hay."""
    motivo = resp if isinstance(resp, str) else _detalle(resp)
    lista = await _get(http, f"{api}/accounts/{cuenta}/inboxes")
    disponibles = ""
    if not isinstance(lista, str) and lista.status_code == 200 and _es_json(lista):
        disponibles = ", ".join(
            f"{i.get('id')} «{i.get('name')}» ({(i.get('channel_type') or '').split('::')[-1]})"
            for i in lista.json().get("payload") or []
        )
    return (
        f"No se puede usar la bandeja CHATWOOT_INBOX_ID ({motivo}). Bandejas que ve este "
        f"token: {disponibles or 'ninguna'}. Si la suya no aparece, agregue al usuario del "
        "token como miembro de la bandeja."
    )


async def _webhook(
    http: httpx.AsyncClient,
    api: str,
    settings: Settings,
    host_publico: str,
    administrador: bool,
) -> list[Chequeo]:
    chequeos: list[Chequeo] = []
    secreto = settings.chatwoot_webhook_secret
    if secreto:
        chequeos.append(Chequeo(OK, "CHATWOOT_WEBHOOK_SECRET configurado."))
    else:
        chequeos.append(
            Chequeo(
                FALLA,
                "CHATWOOT_WEBHOOK_SECRET está vacío: cualquiera que descubra la dirección "
                "del webhook podría hacer que el bot escriba por WhatsApp a nombre de la "
                "empresa. Invente una clave larga y póngala en Railway.",
            )
        )

    host = host_publico or "<dirección-del-bot>"
    url_esperada = f"https://{host}/webhooks/chatwoot?secret=<su CHATWOOT_WEBHOOK_SECRET>"
    if not administrador:
        chequeos.append(
            Chequeo(
                AVISO,
                "No se pudo revisar el webhook: solo un administrador puede verlo. "
                f"Confirme a mano que existe uno con la URL {url_esperada}.",
            )
        )
        return chequeos

    resp = await _get(http, f"{api}/accounts/{settings.chatwoot_account_id}/webhooks")
    if isinstance(resp, str) or resp.status_code != 200 or not _es_json(resp):
        motivo = resp if isinstance(resp, str) else _detalle(resp)
        chequeos.append(Chequeo(AVISO, f"No se pudo leer la lista de webhooks ({motivo})."))
        return chequeos

    webhooks = (resp.json().get("payload") or {}).get("webhooks") or []
    hacia_el_bot = [
        w for w in webhooks if urlparse(w.get("url") or "").path.rstrip("/") == "/webhooks/chatwoot"
    ]
    if not hacia_el_bot:
        chequeos.append(
            Chequeo(
                FALLA,
                "No hay ningún webhook hacia el bot: el asesor podría contestar pero el "
                "cliente nunca recibiría la respuesta. Créelo en Ajustes → Integraciones → "
                f"Webhooks con la URL {url_esperada} y marque {' y '.join(EVENTOS_REQUERIDOS)}.",
            )
        )
        return chequeos

    for w in hacia_el_bot:
        chequeos += _revisar_webhook(w, secreto, host_publico, settings.chatwoot_inbox_id)
    return chequeos


def _revisar_webhook(
    webhook: dict, secreto: str, host_publico: str, bandeja: int
) -> list[Chequeo]:
    url = urlparse(webhook.get("url") or "")
    nombre = f"Webhook «{webhook.get('name') or webhook.get('id')}»"
    chequeos: list[Chequeo] = []
    problemas = 0

    if host_publico and url.hostname and url.hostname.lower() != host_publico.split(":")[0].lower():
        problemas += 1
        chequeos.append(
            Chequeo(
                FALLA,
                f"{nombre} apunta a {url.hostname}, pero este bot responde en "
                f"{host_publico.split(':')[0]}.",
            )
        )
    if url.scheme != "https":
        problemas += 1
        chequeos.append(Chequeo(FALLA, f"{nombre} no usa https://."))

    en_la_url = (parse_qs(url.query).get("secret") or [""])[0]
    if secreto and not hmac.compare_digest(en_la_url, secreto):
        problemas += 1
        chequeos.append(
            Chequeo(
                FALLA,
                f"{nombre}: el ?secret= de la URL no coincide con CHATWOOT_WEBHOOK_SECRET, "
                "así que el bot rechaza cada aviso (401). Tienen que ser idénticos.",
            )
        )

    faltan = [e for e in EVENTOS_REQUERIDOS if e not in (webhook.get("subscriptions") or [])]
    if faltan:
        problemas += 1
        chequeos.append(
            Chequeo(FALLA, f"{nombre}: falta marcar el evento {', '.join(faltan)}.")
        )

    inbox = webhook.get("inbox") or {}
    if inbox.get("id") and inbox.get("id") != bandeja:
        problemas += 1
        chequeos.append(
            Chequeo(
                FALLA,
                f"{nombre} solo avisa de la bandeja «{inbox.get('name')}», no de la "
                f"bandeja {bandeja} que usa el bot.",
            )
        )

    if not problemas:
        chequeos.append(
            Chequeo(OK, f"{nombre} apunta al bot, con el secreto y los dos eventos.")
        )
    return chequeos


def _respaldo() -> Chequeo:
    respaldo = asesor_de_respaldo()
    if respaldo is None:
        return Chequeo(
            AVISO,
            "Sin teléfono de respaldo (ASESOR_TELEFONO_RESPALDO): si Chatwoot falla, el "
            "cliente se queda sin a quién acudir.",
        )
    return Chequeo(
        OK, f"Si Chatwoot falla, el cliente recibe el teléfono {respaldo.telefono}."
    )
