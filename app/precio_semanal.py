"""El precio de venta de la semana, dictado por WhatsApp (ERP · precio semanal).

Cada sábado a las 7:00 el ERP le pregunta al responsable —por el mismo puente
que los avisos internos— el precio de la semana siguiente. Lo que el ERP no
puede hacer es la conversación: preguntar producto por producto, entender
"7,050" o "7 mil 50", y confirmar ANTES de guardar. Eso vive aquí.

Tres reglas gobiernan todo lo de abajo:

1. **Nada se guarda sin un ✅ Confirmar.** Este precio es el que el bot le va a
   dar a los clientes toda la semana. Un "705" al que le faltó un cero no puede
   entrar por haberse leído bien un número mal tecleado.
2. **El bot lee números, no los inventa.** La lectura es determinista (sin
   modelo): un número, sin ambigüedad. "7.050" puede ser siete mil cincuenta o
   siete pesos: se pregunta de nuevo en vez de adivinar.
3. **Quién puede dictar lo decide el ERP.** El bot solo sabe a qué teléfono se
   le preguntó; el ERP vuelve a verificar teléfono y permiso en cada respuesta.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .bus import EventBus
from .erp import ERPClient
from .models import PendientePrecioSemanal, RespuestaPrecioSemanal
from .replies import Boton, Reply

logger = logging.getLogger(__name__)

# El `tipo` del aviso con el que el ERP pregunta el precio del sábado.
TIPO_SOLICITUD_PRECIO = "precios.semanal_solicitud"

# La pregunta queda abierta hasta que termina la semana que precia (del sábado
# al domingo siguiente): nueve días con margen.
PENDIENTE_TTL_SECONDS = 9 * 24 * 3600

# Comando para retomar la captura después de "Después".
COMANDO_PRECIO = "/precio"

# Ids de los botones de la captura. No pasan por el router: los atiende
# `CapturaPrecioSemanal` antes, igual que la respuesta de un transportista.
PS_CONFIRMAR = "ps_confirmar"
PS_CORREGIR = "ps_corregir"
PS_MISMO = "ps_mismo"
PS_OMITIR = "ps_omitir"
PS_DESPUES = "ps_despues"
IDS_CAPTURA = frozenset({PS_CONFIRMAR, PS_CORREGIR, PS_MISMO, PS_OMITIR, PS_DESPUES})

BOTON_CONFIRMAR = Boton(PS_CONFIRMAR, "✅ Confirmar")
BOTON_CORREGIR = Boton(PS_CORREGIR, "✏️ Corregir")
BOTON_MISMO = Boton(PS_MISMO, "🟰 Mismo precio")
BOTON_OMITIR = Boton(PS_OMITIR, "⏭️ No cambiar")
BOTON_DESPUES = Boton(PS_DESPUES, "⏸️ Después")

# Lo que se ESCRIBE en vez de tocar un botón. Se compara la frase completa (sin
# acentos ni signos): "sí" confirma; "sí, pero son 7,100" no — trae un número.
_PALABRAS = {
    PS_CONFIRMAR: {"si", "confirmo", "confirmar", "ok", "correcto", "asi es", "va"},
    PS_CORREGIR: {"no", "corregir", "corrijo", "esta mal", "mal"},
    PS_MISMO: {"mismo", "el mismo", "mismo precio", "igual", "sin cambio", "se queda igual"},
    PS_OMITIR: {"omitir", "saltar", "no cambiar", "siguiente"},
    PS_DESPUES: {"despues", "luego", "al rato", "mas tarde"},
}

# Un cambio de más de 30% contra el precio de hoy casi siempre es un cero de más
# o de menos. No se bloquea —el grano puede moverse—, pero se dice.
_VARIACION_SOSPECHOSA = Decimal("0.30")

# $1,000,000.00 por unidad: el mismo tope de cordura que el ERP.
_MAX_CENTAVOS = 100_000_000

_ACENTOS = str.maketrans("áéíóúüÁÉÍÓÚÜ", "aeiouuAEIOUU")
_MIL = re.compile(r"(\d+(?:[.,]\d+)?)\s*mil\b(?:\s*(?:y\s*)?(\d{1,3})\b)?")
_NUMERO = re.compile(r"\d[\d.,]*")


def es_solicitud_de_precio(tipo: str) -> bool:
    """¿Este aviso del ERP es la pregunta del sábado por el precio?"""
    return tipo == TIPO_SOLICITUD_PRECIO


def telefono_comparable(telefono: str) -> str:
    """El teléfono en la forma con la que se compara y se guarda en el bus.

    WhatsApp puede entregar un celular mexicano con el "1" histórico después
    de la lada (521…) y el ERP lo tiene capturado sin él (52…). Son el mismo;
    sin esto, la respuesta del responsable no encontraría su propia pregunta.
    """
    digitos = re.sub(r"\D", "", telefono or "")
    if len(digitos) == 13 and digitos.startswith("521"):
        return "52" + digitos[3:]
    return digitos


def _normal(texto: str) -> str:
    sin_acentos = (texto or "").translate(_ACENTOS).lower()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", sin_acentos).split())


def intencion_escrita(texto: str) -> str | None:
    """El botón al que equivale lo que escribió, o None."""
    frase = _normal(texto)
    for boton, palabras in _PALABRAS.items():
        if frase in palabras:
            return boton
    return None


@dataclass(frozen=True)
class LecturaPrecio:
    """Lo que se entendió de un mensaje: centavos, o por qué no."""

    centavos: int | None = None
    # "varios" (más de un número) · "ambiguo" (7.050) · "fuera_de_rango"
    problema: str | None = None


def leer_precio(texto: str) -> LecturaPrecio:
    """Pesos por unidad → centavos, sin adivinar.

    Acepta lo que la gente teclea: "7050", "7,050", "$7,050.00", "7050.5",
    "7 mil", "7 mil 50", "7.5 mil". Rechaza lo ambiguo en vez de suponer:
    "7.050" (¿siete mil cincuenta o siete pesos?) y dos números en el mismo
    mensaje ("blanco 7050, amarillo 6800": se pregunta uno por uno).
    """
    t = (texto or "").lower().replace("$", " ")

    m = _MIL.search(t)
    if m:
        try:
            pesos = Decimal(m.group(1).replace(",", ".")) * 1000 + Decimal(m.group(2) or 0)
        except InvalidOperation:
            return LecturaPrecio(problema="ambiguo")
        return _en_rango(pesos)

    numeros = [n.rstrip(".,") for n in _NUMERO.findall(t)]
    numeros = [n for n in numeros if n]
    if not numeros:
        return LecturaPrecio()
    if len(numeros) > 1:
        return LecturaPrecio(problema="varios")

    n = numeros[0]
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", n):
        valor = n.replace(",", "")
    elif re.fullmatch(r"\d+(\.\d{1,2})?", n):
        valor = n
    else:
        # "7.050", "7050,5", "7,05": tres lecturas razonables de lo mismo.
        return LecturaPrecio(problema="ambiguo")
    return _en_rango(Decimal(valor))


def _en_rango(pesos: Decimal) -> LecturaPrecio:
    centavos = int((pesos * 100).to_integral_value())
    if centavos <= 0 or centavos > _MAX_CENTAVOS:
        return LecturaPrecio(problema="fuera_de_rango")
    return LecturaPrecio(centavos=centavos)


def pesos(centavos: int | str | None) -> str:
    """"$7,050.00" a partir de centavos."""
    if centavos is None:
        return "sin precio"
    valor = Decimal(int(centavos)) / 100
    return f"${valor:,.2f}"


class CapturaPrecioSemanal:
    """La conversación con quien dicta el precio.

    El bus guarda dos cosas por teléfono:
      - `bus:precio_semanal:pendiente:{tel}` — a este teléfono se le preguntó
        (lo marca el aviso del sábado o el recordatorio del domingo);
      - `bus:precio_semanal:estado:{tel}` — qué producto se le está preguntando
        y, si ya escribió una cifra, cuál espera confirmación.

    Qué falta por contestar NO se guarda aquí: se le pregunta al ERP cada vez.
    Si alguien fija el precio desde la pantalla a media conversación, el bot no
    pregunta por algo que ya no está pendiente.
    """

    def __init__(self, erp: ERPClient, bus: EventBus) -> None:
        self._erp = erp
        self._bus = bus

    # --- Marca ------------------------------------------------------------- #
    @staticmethod
    def _clave_pendiente(telefono: str) -> str:
        return f"bus:precio_semanal:pendiente:{telefono_comparable(telefono)}"

    @staticmethod
    def _clave_estado(telefono: str) -> str:
        return f"bus:precio_semanal:estado:{telefono_comparable(telefono)}"

    async def marcar(self, telefono: str, referencia: str | None = None) -> None:
        await self._bus.publish(
            self._clave_pendiente(telefono),
            {"referencia": referencia},
            ttl=PENDIENTE_TTL_SECONDS,
        )
        await self._borrar_estado(telefono)

    async def esta_marcado(self, telefono: str) -> bool:
        return bool(await self._bus.read(self._clave_pendiente(telefono)))

    async def desmarcar(self, telefono: str) -> None:
        await self._bus.publish(self._clave_pendiente(telefono), {}, ttl=1)
        await self._borrar_estado(telefono)

    async def _leer_estado(self, telefono: str) -> dict:
        return await self._bus.read(self._clave_estado(telefono)) or {}

    async def _guardar_estado(self, telefono: str, estado: dict) -> None:
        await self._bus.publish(self._clave_estado(telefono), estado, ttl=PENDIENTE_TTL_SECONDS)

    async def _borrar_estado(self, telefono: str) -> None:
        await self._bus.publish(self._clave_estado(telefono), {}, ttl=1)

    # --- Entrada ----------------------------------------------------------- #
    async def reanudar(self, telefono: str) -> Reply:
        """`/precio`: vuelve a abrir la captura si al teléfono le falta algo."""
        pendientes = await self._pendientes(telefono)
        if not pendientes:
            return Reply("No tengo precios pendientes por capturar para este número. 👍")
        await self.marcar(telefono)
        return await self._preguntar(telefono, pendientes, prefacio="")

    async def atender(self, telefono: str, entrada: str, wamid: str | None = None) -> Reply | None:
        """Atiende el mensaje si pertenece a la captura; None si no.

        Un comando ("/menu") o un botón de otro menú (cli_*, cot_*, prov_*) no
        es de la captura: va al router y la pregunta sigue abierta. Todo lo
        demás, mientras el teléfono esté marcado, se lee como parte de ella.
        """
        entrada = (entrada or "").strip()
        es_boton = entrada in IDS_CAPTURA
        if not await self.esta_marcado(telefono):
            if es_boton:
                return Reply(
                    "Esa pregunta ya no está abierta. Si quiere capturar precios, "
                    f"escríbame {COMANDO_PRECIO}."
                )
            return None
        if not es_boton and (entrada.startswith("/") or _es_de_otro_menu(entrada)):
            return None

        accion = entrada if es_boton else intencion_escrita(entrada)

        if accion == PS_DESPUES:
            await self.desmarcar(telefono)
            return Reply(
                f"De acuerdo. Cuando quiera seguir, escríbame {COMANDO_PRECIO}. Si no "
                "contesta, el lunes sigue el precio actual."
            )

        pendientes = await self._pendientes(telefono)
        estado = await self._leer_estado(telefono)
        if not pendientes:
            await self.desmarcar(telefono)
            if es_boton or estado:
                return Reply("Ya no tengo precios pendientes por capturar. ¡Gracias! 🌾")
            # Marcado pero sin nada que preguntar (lo resolvieron desde el ERP):
            # el mensaje era para el bot de siempre.
            return None

        actual = _el_que_se_pregunta(pendientes, estado)

        if estado.get("fase") == "confirmando" and estado.get("producto_id") == actual.productoId:
            if accion == PS_CONFIRMAR:
                return await self._registrar(
                    telefono,
                    actual,
                    "precio",
                    centavos=estado.get("centavos"),
                    texto=estado.get("texto"),
                    wamid=estado.get("wamid"),
                )
            if accion == PS_CORREGIR:
                await self._guardar_estado(telefono, _estado_preguntando(actual))
                return self._pregunta(
                    actual, pendientes, prefacio="Va, escríbame el precio correcto.\n\n"
                )

        if accion == PS_MISMO:
            return await self._registrar(telefono, actual, "mismo")
        if accion == PS_OMITIR:
            return await self._registrar(telefono, actual, "omitir")

        lectura = leer_precio(entrada) if not es_boton else LecturaPrecio()
        if lectura.centavos is not None:
            await self._guardar_estado(
                telefono,
                {
                    "fase": "confirmando",
                    "producto_id": actual.productoId,
                    "centavos": lectura.centavos,
                    "texto": entrada[:1000],
                    "wamid": wamid,
                },
            )
            return self._confirmacion(actual, lectura.centavos)

        prefacio = _explicar(lectura.problema, actual) if lectura.problema else ""
        return await self._preguntar(telefono, pendientes, prefacio=prefacio, actual=actual)

    # --- Internos ---------------------------------------------------------- #
    async def _pendientes(self, telefono: str) -> list[PendientePrecioSemanal]:
        return await self._erp.precio_semanal_pendientes(telefono)

    async def _preguntar(
        self,
        telefono: str,
        pendientes: list[PendientePrecioSemanal],
        prefacio: str,
        actual: PendientePrecioSemanal | None = None,
    ) -> Reply:
        actual = actual or pendientes[0]
        await self._guardar_estado(telefono, _estado_preguntando(actual))
        return self._pregunta(actual, pendientes, prefacio=prefacio)

    @staticmethod
    def _pregunta(
        actual: PendientePrecioSemanal, pendientes: list[PendientePrecioSemanal], prefacio: str
    ) -> Reply:
        varias_empresas = len({p.empresa for p in pendientes}) > 1
        empresa = f"{actual.empresaNombre or actual.empresa} · " if varias_empresas else ""
        avance = (
            f" ({pendientes.index(actual) + 1} de {len(pendientes)})" if len(pendientes) > 1 else ""
        )
        hoy = (
            f"hoy {pesos(actual.precioActualCentavos)} por {actual.unidad}"
            if actual.precioActualCentavos
            else "hoy sin precio publicado"
        )
        texto = (
            f"{prefacio}💲 {empresa}Precio de la semana {actual.semana}{avance}\n"
            f"*{actual.producto}* — {hoy}.\n\n"
            f"¿Cuál es el precio de venta por {actual.unidad} para esa semana? "
            "Escríbalo (por ejemplo 7050) o toque una opción."
        )
        primero = BOTON_MISMO if actual.precioActualCentavos else BOTON_OMITIR
        return Reply(texto=texto, botones=[primero, BOTON_DESPUES])

    @staticmethod
    def _confirmacion(actual: PendientePrecioSemanal, centavos: int) -> Reply:
        aviso = ""
        if actual.precioActualCentavos and int(actual.precioActualCentavos) > 0:
            antes = Decimal(int(actual.precioActualCentavos))
            if abs(Decimal(centavos) - antes) / antes > _VARIACION_SOSPECHOSA:
                aviso = (
                    f"\n\n⚠️ Es muy distinto al de hoy ({pesos(actual.precioActualCentavos)}). "
                    "Revise que no le sobre ni le falte un cero."
                )
        return Reply(
            texto=(
                f"¿Confirmo *{actual.producto}* a *{pesos(centavos)}* por {actual.unidad} "
                f"desde el lunes {_dia(actual.desde)}?{aviso}"
            ),
            botones=[BOTON_CONFIRMAR, BOTON_CORREGIR],
        )

    async def _registrar(
        self,
        telefono: str,
        actual: PendientePrecioSemanal,
        accion: str,
        centavos: int | None = None,
        texto: str | None = None,
        wamid: str | None = None,
    ) -> Reply:
        try:
            r = await self._erp.responder_precio_semanal(
                empresa=actual.empresa,
                solicitud_id=actual.solicitudId,
                producto_id=actual.productoId,
                telefono=telefono,
                accion=accion,
                precio_centavos=centavos,
                wamid=wamid,
                texto=texto,
            )
        except Exception:  # noqa: BLE001 - se le dice y puede volver a confirmar
            logger.exception("No se pudo registrar el precio semanal de %s", telefono)
            botones = [BOTON_CONFIRMAR, BOTON_CORREGIR] if accion == "precio" else [BOTON_DESPUES]
            return Reply(
                "No pude guardar el precio por una falla del ERP. Intente de nuevo en "
                "unos minutos; no se cambió nada.",
                botones=botones,
            )

        if not r.registrado and r.motivo == "no_autorizado":
            await self.desmarcar(telefono)
            return Reply("Este número no está autorizado para dictar precios.")
        if not r.registrado and r.motivo in ("precio_invalido", "sin_precio_actual"):
            prefacio = (
                "No hay un precio actual que repetir: escríbame la cifra.\n\n"
                if r.motivo == "sin_precio_actual"
                else "Ese precio no es válido.\n\n"
            )
            return await self._preguntar(telefono, [actual], prefacio=prefacio, actual=actual)

        hecho = _resultado(actual, r)
        await self._borrar_estado(telefono)
        restantes = await self._pendientes(telefono)
        if not restantes:
            await self.desmarcar(telefono)
            return Reply(f"{hecho}\n\nEso es todo por esta semana. ¡Gracias! 🌾")
        return await self._preguntar(telefono, restantes, prefacio=f"{hecho}\n\n")


def _es_de_otro_menu(entrada: str) -> bool:
    """¿Es el id de un botón de los menús del bot (que resuelve el router)?"""
    return entrada.startswith(("cli_", "cot_", "prov_"))


def _el_que_se_pregunta(
    pendientes: list[PendientePrecioSemanal], estado: dict
) -> PendientePrecioSemanal:
    """El producto que se le está preguntando; el primero si no hay uno en curso."""
    en_curso = estado.get("producto_id")
    return next((p for p in pendientes if p.productoId == en_curso), pendientes[0])


def _estado_preguntando(actual: PendientePrecioSemanal) -> dict:
    return {"fase": "preguntando", "producto_id": actual.productoId}


def _explicar(problema: str, actual: PendientePrecioSemanal) -> str:
    if problema == "varios":
        return f"Vamos uno por uno: escríbame solo el precio de *{actual.producto}*.\n\n"
    if problema == "ambiguo":
        return (
            "No quiero leerlo mal: escríbalo sin puntos de miles, por ejemplo 7050 o 7050.50.\n\n"
        )
    return "Esa cifra no parece un precio válido.\n\n"


_MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _dia(iso: str) -> str:
    """"5 de oct" a partir de 'YYYY-MM-DD'."""
    try:
        _, mes, dia = (int(x) for x in iso.split("-"))
        return f"{dia} de {_MESES[mes - 1]}"
    except (ValueError, IndexError):
        return iso


def _resultado(actual: PendientePrecioSemanal, r: RespuestaPrecioSemanal) -> str:
    """Lo que quedó, en una línea, para que no quede duda de qué se guardó."""
    if not r.registrado:
        if r.motivo == "solicitud_cerrada":
            return f"⚠️ La semana de *{actual.producto}* ya cerró; su precio no se cambió."
        if r.motivo == "ya_aplicado":
            return (
                f"⚠️ El precio de *{actual.producto}* ya estaba en vigor. Para cambiarlo use "
                "la pantalla de Precio de venta del ERP."
            )
        return f"⚠️ No se registró el precio de *{actual.producto}*."
    if r.estado == "OMITIDO":
        return f"⏭️ *{actual.producto}*: sin cambio, sigue el precio actual."
    cuando = (
        "ya está en vigor: el chatbot lo cotiza desde ahora"
        if r.aplicadoYa
        else f"a partir del lunes {_dia(actual.desde)} a las 00:00"
    )
    return f"✅ *{actual.producto}*: {pesos(r.precioCentavos)} por {actual.unidad}, {cuando}."
