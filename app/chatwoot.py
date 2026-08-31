"""Cliente de Chatwoot: la bandeja donde un asesor de carne y hueso atiende.

Cuando el cliente pide hablar con una persona, el bot deja de contestar y la
conversación pasa a Chatwoot. De ahí en adelante el bot es un cable: lo que
escribe el cliente entra a Chatwoot, lo que escribe el asesor sale por WhatsApp.

## Por qué el bot conserva el número y no al revés

Lo natural con Chatwoot sería apuntarle el webhook de Meta y volver al bot un
"Agent Bot". No se hizo, y la razón es concreta: **Chatwoot no manda mensajes
interactivos por WhatsApp Cloud API**. Los botones solo salen dentro de una
plantilla aprobada y los menús de lista no salen. Todo el autoservicio del
cliente está construido sobre esos menús, así que ceder el número habría
cambiado botones por texto plano en el 95% de las conversaciones para ganar
comodidad en el 5% que llega a un asesor.

Con este reparto, Chatwoot ve toda la conversación escalada y el asesor trabaja
en su bandeja de siempre, sin que el resto del bot pierda nada.

## El baile de la Application API

Chatwoot no tiene un "manda esto a este teléfono". Hay que armar la cadena:

    contacto → contact_inbox (source_id) → conversación → mensajes

Por eso el primer escalamiento de un teléfono hace varias llamadas y los
siguientes ninguna: los ids quedan guardados en el bus (ver `handoff.py`).

Sin `CHATWOOT_BASE_URL` el cliente queda deshabilitado y el escalamiento se
comporta como antes de esto: avisa al cliente y lo deja en el log. Es el mismo
patrón que `ERPClient` con su mock.
"""

from __future__ import annotations

import abc
import itertools
import logging
from dataclasses import dataclass, field

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)


class ChatwootNoDisponible(Exception):
    """No se pudo dejar la conversación en Chatwoot.

    Es una condición que el agente TIENE que contarle al cliente: prometerle un
    asesor cuando nadie se enteró es peor que decirle que no se pudo.
    """


@dataclass
class ConversacionChatwoot:
    """La conversación abierta para un teléfono."""

    id: int
    contacto_id: int
    #: `source_id` del contact_inbox: es lo que ata el contacto a ESTE inbox.
    source_id: str = ""


class ChatwootClient(abc.ABC):
    """Lo que el bot necesita de Chatwoot, y nada más."""

    #: False en el cliente nulo: el agente lo consulta antes de prometer nada.
    habilitado: bool = True

    @abc.abstractmethod
    async def abrir_conversacion(
        self, telefono: str, nombre: str = "", atributos: dict | None = None
    ) -> ConversacionChatwoot:
        """Deja lista una conversación abierta para ese teléfono.

        Crea el contacto y el contact_inbox si hacen falta. Idempotente en la
        práctica: si el contacto ya existe, lo reutiliza."""

    @abc.abstractmethod
    async def mensaje_del_cliente(self, conversacion_id: int, texto: str) -> None:
        """Registra en Chatwoot algo que dijo el cliente (entrante)."""

    @abc.abstractmethod
    async def nota_privada(self, conversacion_id: int, texto: str) -> None:
        """Nota que solo ve el equipo. Aquí va el contexto del cliente."""

    @abc.abstractmethod
    async def resolver(self, conversacion_id: int) -> None:
        """Cierra la conversación (el bot retoma)."""


class NullChatwootClient(ChatwootClient):
    """Cuando Chatwoot no está configurado.

    No finge: cualquier intento de escalar levanta `ChatwootNoDisponible` y el
    agente decide qué decirle al cliente. Falla ruidoso a propósito — un
    escalamiento que se pierde en silencio es un cliente esperando para siempre.
    """

    habilitado = False

    async def abrir_conversacion(
        self, telefono: str, nombre: str = "", atributos: dict | None = None
    ) -> ConversacionChatwoot:
        raise ChatwootNoDisponible("CHATWOOT_BASE_URL no está configurada")

    async def mensaje_del_cliente(self, conversacion_id: int, texto: str) -> None:
        raise ChatwootNoDisponible("CHATWOOT_BASE_URL no está configurada")

    async def nota_privada(self, conversacion_id: int, texto: str) -> None:
        raise ChatwootNoDisponible("CHATWOOT_BASE_URL no está configurada")

    async def resolver(self, conversacion_id: int) -> None:
        raise ChatwootNoDisponible("CHATWOOT_BASE_URL no está configurada")


class HTTPChatwootClient(ChatwootClient):
    """Chatwoot real, vía Application API (`api_access_token`)."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        account_id: int,
        inbox_id: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = f"{base_url.rstrip('/')}/api/v1/accounts/{account_id}"
        self._token = api_token
        self._inbox_id = inbox_id
        self._transport = transport  # inyectable en pruebas

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=15,
            headers={"api_access_token": self._token, "Content-Type": "application/json"},
            transport=self._transport,
        )

    # --- Contacto y conversación ------------------------------------------- #
    async def abrir_conversacion(
        self, telefono: str, nombre: str = "", atributos: dict | None = None
    ) -> ConversacionChatwoot:
        try:
            async with self._client() as http:
                contacto_id, source_id = await self._contacto(http, telefono, nombre, atributos)
                if not source_id:
                    source_id = await self._contact_inbox(http, contacto_id, telefono)
                conversacion_id = await self._conversacion(http, contacto_id, source_id)
        except ChatwootNoDisponible:
            raise
        except Exception as exc:  # noqa: BLE001 - cualquier fallo se cuenta igual
            raise ChatwootNoDisponible(f"{type(exc).__name__}: {exc}") from exc
        return ConversacionChatwoot(
            id=conversacion_id, contacto_id=contacto_id, source_id=source_id
        )

    async def _contacto(
        self, http: httpx.AsyncClient, telefono: str, nombre: str, atributos: dict | None
    ) -> tuple[int, str]:
        """(contacto_id, source_id). El source_id puede venir vacío."""
        # Chatwoot guarda el teléfono en E.164 CON '+'; WhatsApp lo manda sin.
        e164 = telefono if telefono.startswith("+") else f"+{telefono}"

        resp = await http.get(f"{self._base}/contacts/search", params={"q": e164})
        resp.raise_for_status()
        for contacto in resp.json().get("payload", []):
            if (contacto.get("phone_number") or "").lstrip("+") == e164.lstrip("+"):
                return contacto["id"], self._source_id(contacto)

        resp = await http.post(
            f"{self._base}/contacts",
            json={
                "inbox_id": self._inbox_id,
                "name": nombre or e164,
                "phone_number": e164,
                # `identifier` hace idempotente el alta: dos escalamientos
                # simultáneos del mismo teléfono no crean dos contactos.
                "identifier": e164,
                **({"custom_attributes": atributos} if atributos else {}),
            },
        )
        resp.raise_for_status()
        contacto = resp.json().get("payload", {}).get("contact", {})
        return contacto["id"], self._source_id(contacto)

    def _source_id(self, contacto: dict) -> str:
        """El source_id del contact_inbox de NUESTRO inbox, si ya existe."""
        for ci in contacto.get("contact_inboxes") or []:
            if (ci.get("inbox") or {}).get("id") == self._inbox_id:
                return ci.get("source_id") or ""
        return ""

    async def _contact_inbox(
        self, http: httpx.AsyncClient, contacto_id: int, telefono: str
    ) -> str:
        """Ata un contacto que ya existía a nuestro inbox."""
        resp = await http.post(
            f"{self._base}/contacts/{contacto_id}/contact_inboxes",
            json={"inbox_id": self._inbox_id, "source_id": telefono},
        )
        resp.raise_for_status()
        return resp.json().get("source_id") or telefono

    async def _conversacion(
        self, http: httpx.AsyncClient, contacto_id: int, source_id: str
    ) -> int:
        resp = await http.post(
            f"{self._base}/conversations",
            json={
                "source_id": source_id,
                "inbox_id": self._inbox_id,
                "contact_id": contacto_id,
                # `open` y no `pending`: pending es la bandeja de los bots en
                # Chatwoot, y aquí el bot ya se rindió — esto necesita persona.
                "status": "open",
            },
        )
        resp.raise_for_status()
        return resp.json()["id"]

    # --- Mensajes ----------------------------------------------------------- #
    async def mensaje_del_cliente(self, conversacion_id: int, texto: str) -> None:
        await self._mensaje(conversacion_id, texto, tipo="incoming", privado=False)

    async def nota_privada(self, conversacion_id: int, texto: str) -> None:
        await self._mensaje(conversacion_id, texto, tipo="outgoing", privado=True)

    async def _mensaje(
        self, conversacion_id: int, texto: str, tipo: str, privado: bool
    ) -> None:
        try:
            async with self._client() as http:
                resp = await http.post(
                    f"{self._base}/conversations/{conversacion_id}/messages",
                    json={"content": texto, "message_type": tipo, "private": privado},
                )
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise ChatwootNoDisponible(f"{type(exc).__name__}: {exc}") from exc

    async def resolver(self, conversacion_id: int) -> None:
        try:
            async with self._client() as http:
                resp = await http.post(
                    f"{self._base}/conversations/{conversacion_id}/toggle_status",
                    json={"status": "resolved"},
                )
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise ChatwootNoDisponible(f"{type(exc).__name__}: {exc}") from exc


@dataclass
class _ConversacionMock:
    id: int
    telefono: str
    estado: str = "open"
    mensajes: list[dict] = field(default_factory=list)


class MockChatwootClient(ChatwootClient):
    """Chatwoot simulado en memoria, para desarrollo y pruebas."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.conversaciones: dict[int, _ConversacionMock] = {}
        self._por_telefono: dict[str, int] = {}

    async def abrir_conversacion(
        self, telefono: str, nombre: str = "", atributos: dict | None = None
    ) -> ConversacionChatwoot:
        existente = self._por_telefono.get(telefono)
        if existente is not None and self.conversaciones[existente].estado == "open":
            conv = self.conversaciones[existente]
        else:
            conv = _ConversacionMock(id=next(self._ids), telefono=telefono)
            self.conversaciones[conv.id] = conv
            self._por_telefono[telefono] = conv.id
        return ConversacionChatwoot(id=conv.id, contacto_id=conv.id, source_id=telefono)

    def _conv(self, conversacion_id: int) -> _ConversacionMock:
        conv = self.conversaciones.get(conversacion_id)
        if conv is None:
            raise ChatwootNoDisponible(f"conversación {conversacion_id} inexistente")
        return conv

    async def mensaje_del_cliente(self, conversacion_id: int, texto: str) -> None:
        self._conv(conversacion_id).mensajes.append(
            {"tipo": "incoming", "privado": False, "texto": texto}
        )

    async def nota_privada(self, conversacion_id: int, texto: str) -> None:
        self._conv(conversacion_id).mensajes.append(
            {"tipo": "outgoing", "privado": True, "texto": texto}
        )

    async def resolver(self, conversacion_id: int) -> None:
        self._conv(conversacion_id).estado = "resolved"


def get_chatwoot_client() -> ChatwootClient:
    settings = get_settings()
    if not settings.chatwoot_base_url:
        return NullChatwootClient()
    if settings.chatwoot_base_url == "mock":
        return MockChatwootClient()
    return HTTPChatwootClient(
        settings.chatwoot_base_url,
        api_token=settings.chatwoot_api_token,
        account_id=settings.chatwoot_account_id,
        inbox_id=settings.chatwoot_inbox_id,
    )
