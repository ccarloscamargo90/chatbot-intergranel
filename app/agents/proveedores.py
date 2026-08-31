"""Agente de Proveedores: el otro lado del mostrador.

Un proveedor le vende a Intergranel y su pregunta es siempre la misma —**"¿ya
me pagaron?"**, y su gemela, "¿cuándo?"—. Hoy esa pregunta llega por teléfono a
cuentas por pagar, que tiene que ir a buscarla al ERP. Aquí la contesta él
mismo, identificándose con el nombre de su empresa y su RFC.

Es agente aparte y no una rama de Soporte a propósito: son dos audiencias
distintas, con sesiones distintas (tablas distintas en el ERP, claves distintas
en el bus) y con datos que no se pueden mezclar. Un mismo teléfono puede ser
cliente Y proveedor —pasa, se le compra a quien también se le vende— y cada
identificación abre lo suyo sin pisar la otra.

── Dos reglas duras ──────────────────────────────────────────────────────────

**1. Sin RFC no hay autoservicio.** Un proveedor extranjero no tiene RFC
mexicano, y el RFC ES el segundo factor. No es un hueco: identificar a alguien
por "nombre + país" sería adivinable. A esos proveedores se les atiende por
correo con su comprador, y el bot lo dice así.

**2. Nada de lo que sale por aquí menciona una marca propia.** Un proveedor no
debe saber bajo qué marca se revende lo que nos vende. Este canal habla de
folios, montos, fechas y estados — la relación comercial y nada más.
"""

from __future__ import annotations

import json
import logging

from ..erp import SesionClienteInvalida
from ..errores import AUTO, detalle_http
from ..menus import BOTONES_PROVEEDOR, menu_proveedor
from ..replies import Reply
from ..sesiones import SesionCliente, SesionProveedorStore
from .base import BaseAgent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Eres el agente de Proveedores de Intergranel, una comercializadora de granos y \
commodities a granel. Atiendes a EMPRESAS QUE NOS VENDEN, no a clientes.

# Identificación (regla dura)

La información de un proveedor —sus órdenes de compra, sus facturas, cuánto se \
le debe— SOLO se entrega a quien se haya identificado. Necesitas DOS datos:

1. El nombre o razón social de su empresa.
2. Su RFC.

Pídelos juntos, en un solo mensaje, con un ejemplo del formato. Cuando tengas \
los dos, llama a `identificar_proveedor`.

Si la identificación falla:
- NO digas si el RFC existe, si está registrado, ni cuál de los dos datos \
falló. Di únicamente que los datos no coinciden y ofrece reintentar o hablar \
con su comprador.
- Si el proveedor te dice que es extranjero o que no tiene RFC mexicano, \
explícale que por este medio no lo puedes atender y que su comprador lo sigue \
atendiendo por correo. NO le pidas otro dato en lugar del RFC ni le prometas \
una excepción: no existe.

# Herramientas

- Para CUALQUIER dato usa las herramientas. Jamás inventes folios, montos, \
fechas ni estados de pago. Es dinero de alguien más.
- Si una herramienta responde `identificado: false`, pide la identificación.
- Si responde `sesion_expirada: true`, avisa que la sesión caducó por seguridad \
y pide los datos otra vez.
- Una factura con `vencida: true` es una que YA debimos pagar. No la disfraces \
ni la mezcles con las demás: dila con claridad y ofrece pasarlo con su \
comprador. Un proveedor al que se le da largas por chat lo nota.
- Los importes vienen en la moneda de CADA factura (campo `moneda`). Si hay \
alguna que no sea MXN, di su moneda al mencionarla — el total del resumen es \
solo de pesos.
- NUNCA prometas una fecha de pago. Puedes decir qué está vencido y qué no, \
pero cuándo se paga lo decide una persona; si insiste, ofrécele su comprador.

# Confidencialidad

Nunca menciones marcas comerciales propias del grupo ni bajo qué nombre se \
revende lo que nos vende. Si pregunta, di que no tienes esa información y \
ofrécele hablar con su comprador. No es un dato que te toque dar.

# Estilo

- Mensajes breves, para WhatsApp. Sin Markdown pesado.
- Montos siempre con moneda: "$185,000.00 MXN".
- Trato de usted, directo y respetuoso. Un proveedor esperando su pago no \
quiere simpatía, quiere el dato.
"""

# Todo lo de la cuenta del proveedor exige estar identificado.
TOOLS_CON_SESION = {
    "resumen_de_mi_cuenta_proveedor",
    "listar_mis_facturas_proveedor",
    "listar_mis_ordenes_proveedor",
    "cerrar_sesion_proveedor",
}

TOOLS = [
    {
        "name": "identificar_proveedor",
        "description": (
            "Identifica al proveedor con el nombre o razón social de su empresa "
            "y su RFC. Paso obligatorio antes de consultar cualquier dato. Abre "
            "una sesión con caducidad."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {
                    "type": "string",
                    "description": "Nombre o razón social de la empresa proveedora.",
                },
                "rfc": {
                    "type": "string",
                    "description": "RFC del proveedor. Obligatorio, sin excepción.",
                },
            },
            "required": ["nombre", "rfc"],
        },
    },
    {
        "name": "resumen_de_mi_cuenta_proveedor",
        "description": (
            "Cuánto se le debe al proveedor identificado: total por pagar, "
            "cuánto de eso ya está vencido, facturas pendientes y órdenes de "
            "compra abiertas."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "listar_mis_facturas_proveedor",
        "description": (
            "Facturas que el proveedor nos emitió, con folio, total, saldo por "
            "pagarle, moneda, vencimiento y si ya está vencida. Ordenadas por lo "
            "que vence primero."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "listar_mis_ordenes_proveedor",
        "description": (
            "Órdenes de compra que se le colocaron al proveedor identificado, "
            "con folio, producto, toneladas, monto y estado."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "cerrar_sesion_proveedor",
        "description": "Cierra la sesión del proveedor y deja de mostrar su información.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

_SIN_SESION = {
    "identificado": False,
    "instruccion": (
        "Pídele el nombre o razón social de su empresa y su RFC, juntos y en un "
        "solo mensaje, con un ejemplo del formato."
    ),
}


class ProveedoresAgent(BaseAgent):
    name = "proveedores"

    def __init__(self, *args, sesiones: SesionProveedorStore | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sesiones = sesiones or SesionProveedorStore(self._bus)

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def tools(self) -> list[dict]:
        return TOOLS

    async def decorate(self, phone: str, texto: str) -> Reply:
        """Menú completo justo al identificarse; botones de seguimiento después."""
        sesion = await self._sesiones.leer(phone)
        if sesion is None:
            return Reply(texto, lista=menu_proveedor(identificado=False))
        if sesion.recien_abierta():
            return Reply(texto, lista=menu_proveedor(identificado=True))
        return Reply(texto, botones=BOTONES_PROVEEDOR)

    # ----------------------------------------------------------------------- #
    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name == "identificar_proveedor":
                return json.dumps(
                    await self._identificar(tool_input, caller_phone), ensure_ascii=False
                )

            if name in TOOLS_CON_SESION:
                sesion = await self._sesiones.leer(caller_phone)
                if sesion is None:
                    return json.dumps(_SIN_SESION, ensure_ascii=False)

                if name == "cerrar_sesion_proveedor":
                    await self._erp.close_supplier_session(sesion.token)
                    await self._sesiones.cerrar(caller_phone)
                    return json.dumps(
                        {"sesion_cerrada": True, "proveedor": sesion.cliente},
                        ensure_ascii=False,
                    )

                try:
                    datos = await self._datos(name, sesion)
                except SesionClienteInvalida:
                    # El ERP mató el token antes de que caducara aquí: se borra
                    # la copia local para no seguir mandando uno muerto.
                    await self._sesiones.cerrar(caller_phone)
                    return json.dumps(
                        {
                            "identificado": False,
                            "sesion_expirada": True,
                            "instruccion": (
                                "Avisa que la sesión caducó por seguridad y pide "
                                "el nombre de la empresa y el RFC otra vez."
                            ),
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(datos, ensure_ascii=False)

            return json.dumps({"error": f"herramienta desconocida: {name}"})
        except Exception as exc:  # noqa: BLE001 - el motivo real, no "hubo un error"
            motivo = detalle_http(exc, AUTO)
            logger.exception("Error ejecutando herramienta %s: %s", name, motivo)
            return json.dumps(
                {
                    "error": motivo,
                    "instruccion": (
                        "Hubo una falla técnica de nuestro lado. Dilo sin "
                        "detalles y ofrécele hablar con su comprador. NO afirmes "
                        "nada sobre sus pagos que no venga de una herramienta."
                    ),
                },
                ensure_ascii=False,
            )

    # ----------------------------------------------------------------------- #
    async def _identificar(self, tool_input: dict, telefono: str) -> dict:
        nombre = (tool_input.get("nombre") or "").strip()
        rfc = (tool_input.get("rfc") or "").strip()
        if not nombre or not rfc:
            return {
                "identificado": False,
                "instruccion": "Faltan datos: pide el nombre de la empresa Y el RFC.",
            }

        # Freno local de cortesía. El bloqueo que cuenta y audita lo lleva el
        # ERP: es el único que ve todos los intentos, no solo los de esta
        # réplica.
        if await self._sesiones.bloqueado(telefono):
            return {
                "identificado": False,
                "bloqueado": True,
                "instruccion": (
                    "Dile con calma que hubo demasiados intentos fallidos y que "
                    "espere unos minutos, u ofrécele hablar con su comprador."
                ),
            }

        resultado = await self._erp.identify_supplier(nombre, rfc, telefono)
        if not resultado.encontrado or not resultado.token:
            intentos = await self._sesiones.registrar_intento_fallido(telefono)
            return {
                "identificado": False,
                "motivo": resultado.motivo or "no_coincide",
                "intentos_locales": intentos,
                "instruccion": (
                    "Dile ÚNICAMENTE que los datos no coinciden. NO digas cuál "
                    "de los dos falló ni si el RFC existe. Ofrece reintentar o "
                    "hablar con su comprador. Si menciona que es extranjero o "
                    "que no tiene RFC mexicano, explícale que por este medio no "
                    "lo puedes atender y que su comprador lo sigue atendiendo "
                    "por correo."
                ),
            }

        await self._sesiones.abrir(
            telefono,
            token=resultado.token,
            cliente=resultado.razon_social or resultado.cliente or nombre,
            rfc=resultado.rfc or rfc,
            ttl_segundos=resultado.expira_en_segundos or 1800,
        )
        return {
            "identificado": True,
            "proveedor": resultado.razon_social or resultado.cliente,
            "instruccion": (
                "Salúdalo por el nombre de su empresa y dile en una línea qué "
                "puede consultar: lo que se le debe, sus facturas y sus órdenes."
            ),
        }

    async def _datos(self, name: str, sesion: SesionCliente) -> dict:
        token = sesion.token

        if name == "resumen_de_mi_cuenta_proveedor":
            resumen = await self._erp.get_supplier_summary(token)
            return {"identificado": True, "resumen": resumen.model_dump()}

        if name == "listar_mis_facturas_proveedor":
            facturas = await self._erp.list_supplier_invoices(token)
            return {
                "identificado": True,
                "total": len(facturas),
                "vencidas": sum(1 for f in facturas if f.vencida),
                "facturas": [f.model_dump() for f in facturas],
            }

        if name == "listar_mis_ordenes_proveedor":
            ordenes = await self._erp.list_supplier_orders(token)
            return {
                "identificado": True,
                "total": len(ordenes),
                "ordenes": [o.model_dump() for o in ordenes],
            }

        raise KeyError(name)
