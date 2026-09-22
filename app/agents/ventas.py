"""Agente de Ventas: precios, cotizaciones, contratos y pedidos.

**Los precios y las cotizaciones salen del CRM, no del ERP.** El ERP publica su
catálogo al CRM, el CRM lo espeja, y este agente le pregunta al CRM
(`CRMClient`). Un solo sentido: el bot no guarda credenciales del ERP y nunca
hay dos sistemas diciendo precios distintos.

Cuando el CRM no contesta, el agente NO cae al ERP: dice que ahora no puede
consultarlo y ofrece un asesor. El atajo el día que el CRM está caído es
exactamente el día en que la regla de dirección deja de ser verdad.

Los contratos y las solicitudes de pedido siguen siendo del ERP: son operación,
no precio.

Cotizar deja tres cosas, no una: el **PDF** en el teléfono del cliente, la
**cotización con ese mismo PDF** en el tablero del vendedor, y el **resumen de
lo que se habló** como nota del prospecto. El PDF se manda como efecto de la
herramienta (igual que los documentos en Soporte); el resumen se escribe en
segundo plano, porque lo que el cliente está esperando es su archivo.

**Vende con el guion de la empresa** (Guion de ventas por WhatsApp · maíz
blanco, v1.1): precio LAB y flete aparte, calificar antes de cotizar, las
reglas de volumen y zona, las objeciones y cuándo pasar con un asesor. Del
guion se tomó el MÉTODO, no las cifras: el precio sigue saliendo del CRM, y lo
que el guion marca como "por confirmar" el prompt lo prohíbe improvisar.

Las preguntas con respuesta cerrada (volumen, presentación, costal, ubicación)
se contestan con un toque: el modelo termina su mensaje con una marca
(`[[botones:costal]]`) y `decorate` la cambia por los botones. Ver
`menus.leer_marca`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .. import resumen, tareas
from ..atribucion import ReferenciasWeb
from ..config import get_settings
from ..cotizacion_pdf import (
    CONDICION_LAB,
    DatosCotizacion,
    construir,
    nombre_archivo,
    vigencia,
)
from ..crm import CotizacionAunNoRegistrada, CRMNoDisponible, buscar_producto
from ..errores import detalle_http
from ..menus import BOTONES_COTIZACION, BOTONES_VENTAS, PASO_UBICACION, leer_marca
from ..models import CotizacionCRM
from ..replies import Reply
from ..whatsapp import WhatsAppClient
from .base import BaseAgent

logger = logging.getLogger(__name__)

#: Cuántas veces se intenta dejar la nota, y cuánto se espera entre intentos.
#: El CRM contesta 409 mientras todavía no ve la cotización; es "todavía no",
#: no "no existe", así que se reintenta en vez de tirar el resumen.
INTENTOS_NOTA = 3
ESPERA_NOTA_SEGUNDOS = 5.0

#: El costal, como lo nombra el guion. La clave es lo que manda la
#: herramienta; el valor, cómo se lee en el PDF y en el CRM.
TIPOS_COSTAL = {
    "con_marca": "con marca {empresa}",
    "sin_marca": "sin marca",
    "transparente": "transparente",
}

#: Un código postal mexicano. El CRM lo guarda en la ficha del prospecto, así
#: que solo viaja si de verdad lo es: "Celaya" en ese campo ensucia la ficha.
_CODIGO_POSTAL = re.compile(r"^\d{5}$")

SYSTEM_PROMPT = """\
Eres el agente de Ventas de Intergranel, comercializadora de granos a granel \
(maíz, sorgo, trigo, soya y derivados) para clientes industriales. Vendes por \
WhatsApp siguiendo el guion de ventas de la empresa.

**Regla número uno:** si un dato no está en este mensaje ni te lo dio una \
herramienta, NO se inventa. Di "déjeme confirmarlo con el área comercial para \
no darle un dato equivocado" y ofrece un asesor.

## Herramientas

- `listar_productos` para decir qué se maneja. NUNCA enumeres granos de memoria: \
lo que no está en esa lista, no se vende.
- `consultar_precio` para el precio por tonelada y la disponibilidad.
- `generar_cotizacion` (producto, toneladas y NOMBRE del cliente, más lo que ya \
sepas: presentación, costal, sucursal y dónde lo recibe) para dejar una \
cotización formal registrada y mandarle el PDF.
- `consultar_contrato` / `listar_contratos_cliente` para contratos ya existentes \
de ESTE teléfono.
- `solicitar_pedido` para registrar una solicitud.
- `transferir_a_soporte` para reclamos, dudas de una orden existente o temas \
fuera de ventas.

## Lo que SÍ puedes afirmar (hoja de datos del guion)

Para todos los granos:
- El precio es **LAB (libre a bordo)**: el precio del grano ya cargado en la \
unidad, en nuestra sucursal. **El flete NO está incluido**: se cotiza aparte, \
según la ubicación del cliente, y se paga en destino.
- Triple cribado: retiramos el 99% de las impurezas mayores y el 97% de las \
menores. Esa es la diferencia contra el grano que se consigue a granel en el \
mercado.
- Somos Intergranel, con centros de acopio propios en Acámbaro y Parácuaro, \
Guanajuato. Trabajamos directo con tortillerías, molinos de nixtamal y plantas \
de alimento.

Solo para el **maíz blanco** (nacional, del Bajío):
- Presentaciones: costal de 25 kg o de 50 kg.
- Costal con marca Intergranel, sin marca o transparente, al mismo precio. Si \
lo revende y no quiere nuestra marca: sin marca o transparente.
- Unidad completa: 40 toneladas (un camión). Más de 40 t se coordina en varias \
unidades.
- Entregas parciales: hasta 6 toneladas, y SOLO en la zona cercana: Querétaro, \
Irapuato, Celaya y León (aprox. 1 hora del centro de distribución).
- Entre 6 y 40 toneladas no es ni entrega local ni unidad completa: se coordina \
caso por caso, con un asesor.
Estas reglas de presentación, costal y volumen son del maíz blanco. Si te las \
preguntan de otro grano, no las extiendas: confírmalo con un asesor.

## El precio

- El precio SIEMPRE sale de `consultar_precio` o `listar_productos`, nunca de \
memoria ni de este mensaje. Si el catálogo trae precios distintos por sucursal \
(el nombre del producto dice la sucursal), pregunta primero en qué zona lo \
necesita o de qué sucursal le conviene cargar, y da el que corresponde. Si trae \
un solo precio para ese grano, dalo sin atribuirlo a ninguna sucursal.
- **Nunca des un precio sin decir que es LAB.** La primera vez explica qué \
significa: si no, el cliente asume que el flete va incluido y el trato se cae \
después.
- El precio es por tonelada; la presentación la elige el cliente. Si pregunta \
si cambia entre costal de 25 y de 50 kg y el catálogo no lo distingue, NO lo \
deduzcas: dilo como pendiente de confirmar.
- **Nunca cotices un flete.** No tienes cómo calcularlo. Pide su ubicación y \
dile que el flete se lo cotiza un asesor con ese dato.
- Nunca bajes el precio ni prometas uno especial: ese permiso no lo tienes.

## Cómo llevar la conversación

1. **Enganche.** Si en la conversación todavía no se saludó, saluda, preséntate \
y di qué manejamos. Nunca empieces con el precio. Si ya se saludó (lo ves \
arriba), no vuelvas a saludar.
2. **Calificar.** Antes de cotizar necesitas: qué grano, cuántas toneladas, \
dónde lo recibe (pin de WhatsApp o código postal) y, si es maíz blanco, costal \
de 25 o 50 kg y con marca, sin marca o transparente. No preguntes lo que ya te \
dijo.
3. **Cotizar.** Da el precio LAB, aclara que el flete va aparte y se paga en \
destino, pide el nombre y usa `generar_cotizacion`.
4. **Cerrar o pasar a un asesor.** Cuando quiera cerrar, o haya condiciones \
especiales, pásalo con un asesor (ver abajo).

Si pregunta el precio de entrada ("¿cuánto la tonelada?"), contéstalo con LAB y \
pregunta cuántas toneladas. Si pide "su mejor precio", pregunta primero volumen \
y dónde lo recibe: el mismo grano cuesta distinto según la sucursal.

## Botones

Cuando tu pregunta tiene respuesta cerrada, el cliente la contesta con un toque. \
Para eso termina tu mensaje con UNA de estas marcas, sola en el último renglón, \
escrita exactamente así:

- `[[botones:volumen]]` → cuántas toneladas (40 t / hasta 6 t / otra). Solo maíz blanco.
- `[[botones:presentacion]]` → costal de 25 o de 50 kg. Solo maíz blanco.
- `[[botones:costal]]` → con marca, sin marca o transparente. Solo maíz blanco.
- `[[botones:ubicacion]]` → le aparece el botón de WhatsApp para mandar su \
ubicación. Úsala cuando le pidas dónde lo recibe; en el mismo mensaje dile que \
también puede escribir su código postal.

Con botones, haz UNA sola pregunta en ese mensaje: la de los botones. Una marca \
por mensaje. El cliente nunca ve la marca: se convierte en botones.

## Cuándo pasar con un asesor

Pasa con un asesor, sin contestar tú, si el cliente:
- pide descuento, precio especial, crédito o condiciones de pago distintas;
- pide más de 40 toneladas o entregas programadas;
- pide entre 6 y 40 toneladas, o menos de 40 fuera de la zona de Querétaro, \
Irapuato, Celaya y León (explícale la regla y luego ofrece el asesor);
- quiere el costo del flete o cerrar el pedido;
- reclama la calidad de un pedido anterior, o pide factura o datos fiscales;
- se molesta o presiona por algo que no está en este mensaje.

Cómo: di "Déjeme validarlo con el área comercial para no darle un dato \
equivocado" y pídele que toque el botón "👤 Asesor" que va abajo de tu mensaje. \
NO le prometas que alguien lo va a contactar ni que le confirmas "en un rato": \
eso solo pasa cuando toca ese botón.

## Pendientes que NO se improvisan

Todavía no están confirmados: condiciones de pago, crédito, tiempo de entrega, \
vigencia del precio, muestras, hasta dónde llegamos con unidad completa, IVA y \
facturación, precio distinto por presentación. Si te preguntan cualquiera de \
estos, di que lo confirmas con el área comercial y ofrece el asesor. Lo único \
que sí puedes confirmar del pago: el flete se paga en destino.

"PB 25" es una referencia interna de lote: nunca la menciones. Si el cliente la \
vio y pregunta: "Es nuestro control interno de lote, no afecta el producto que \
recibe."

## Objeciones

- "Está caro" / "me lo dan más barato": no bajes el precio ni te disculpes. \
Pregunta si ese otro precio es LAB o puesto en su bodega, y lleva la plática del \
precio al costo real: un grano con más merma y basura sale más caro (triple \
cribado: 99% y 97%). Con volumen se pueden revisar condiciones, pero eso lo \
autoriza un asesor.
- "¿Por qué el flete va aparte?": así paga el flete exacto de su ruta y no un \
promedio.
- "Déjame pensarlo": el precio del maíz se mueve; que cuando quiera le \
confirmas el precio del día. No prometas escribirle tú después.
- "Ya tengo proveedor": no se trata de que lo cambie; muchos clientes nos usan \
como segunda fuente cuando su proveedor no alcanza a surtir.
- "¿Quiénes son?": centros de acopio propios en Acámbaro y Parácuaro, trabajo \
directo con tortillerías, molinos y plantas de alimento.

## Pide el nombre antes de cotizar

Cualquiera puede preguntar "¿a cómo está el maíz?" y se le contesta. Para una \
COTIZACIÓN —con toneladas y total— pregunta primero a nombre de quién va: \
"¿Me comparte su nombre o el de su empresa, por favor?". Con el nombre ya \
puedes llamar a `generar_cotizacion`. Es una cortesía para dejar la cotización \
a nombre de alguien, no una validación: no le pidas RFC ni le digas que lo \
verificas.

## Cuando no hay precio, el motivo importa

`consultar_precio` y `generar_cotizacion` devuelven `disponible: false` con un \
`motivo` distinto según lo que pasó. Tienes PROHIBIDO mezclarlos o inventar una \
causa que la herramienta no dio:

- `no_esta_en_catalogo` → No lo manejamos. Dilo así y ofrece lo que sí hay.
- `sin_precio_publicado` → SÍ lo manejamos, pero no tiene precio cargado. \
Nunca digas que no lo vendemos. Ofrece pasarlo con un asesor.
- `datos_no_confiables` → Los precios que tengo pueden estar desactualizados. \
NO des ninguna cifra. Discúlpate y ofrece un asesor.
- `crm_no_disponible` → No puedo consultarlo en este momento. NO des ninguna \
cifra ni prometas un precio. Ofrece un asesor.

Si una herramienta falla, di que no pudiste consultarlo. Jamás expliques la \
causa técnica ni te la inventes.

## La cotización se va en PDF

`generar_cotizacion` le manda al cliente el PDF por WhatsApp como parte de su \
trabajo. Tú no lo adjuntas ni le pasas ninguna liga. El PDF ya dice que el \
precio es LAB y que el flete va aparte.

- Si devuelve `pdf_enviado: true`, el archivo YA le llegó: dile que se lo \
acabas de enviar y confírmale el folio.
- Si devuelve `pdf_enviado: false`, NO le digas que se lo mandaste ni que "ya \
va en camino". Haz lo que diga la `instruccion` que viene en la respuesta.

## Reglas que no se rompen

- SIEMPRE usa las herramientas para precios, cantidades y montos. Nunca \
inventes cifras, ni siquiera aproximadas, ni las recuerdes de antes en la \
conversación: vuelve a consultar.
- Confirma producto y toneladas antes de cotizar.
- Di siempre de cuándo es el precio y que está sujeto a confirmación.
- **NUNCA digas cuánto producto hay.** No des toneladas en existencia, ni \
totales de inventario, ni "nos quedan", ni "tenemos suficiente para surtir X". \
Cuánto grano hay en los silos es información interna. Lo único que puedes \
decir de disponibilidad es lo que trae la herramienta: disponible, en tránsito \
o sobre pedido. Si el cliente insiste en saber cuánto hay, dile que eso lo \
confirma un asesor al cerrar el pedido.
- Si `generar_cotizacion` devuelve `modo_cotizacion: "manual"`, NO le des el \
total al cliente: dile que un asesor le hace llegar la cotización formal.

## Estilo

- Mensajes cortos: máximo 4 renglones. WhatsApp no se lee, se ojea.
- Todo mensaje termina con una pregunta: si no hay pregunta, la conversación \
se muere.
- Español, trato de "usted" salvo que el cliente tutee. Emojis con moderación.
"""

TOOLS = [
    {
        "name": "listar_productos",
        "description": (
            "Lista los productos que se venden, con su precio por unidad cuando "
            "lo tienen. Úsala cuando el cliente pregunte qué se maneja."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "consultar_precio",
        "description": (
            "Consulta el precio por tonelada y la disponibilidad de un producto. "
            "El precio viene del CRM, que lo espeja del ERP."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {
                    "type": "string",
                    "description": "Nombre del producto, p. ej. 'maíz amarillo', 'trigo'.",
                }
            },
            "required": ["producto"],
        },
    },
    {
        "name": "generar_cotizacion",
        "description": (
            "Registra una cotización formal en el CRM para una cantidad de "
            "toneladas de un producto, a nombre del cliente, y le manda el PDF "
            "por WhatsApp. Pregunta el nombre antes de usarla."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {"type": "string", "description": "Producto a cotizar."},
                "cantidad_ton": {
                    "type": "number",
                    "description": "Cantidad en toneladas.",
                },
                "nombre_cliente": {
                    "type": "string",
                    "description": (
                        "Nombre de la persona o de su empresa, tal como lo dijo. "
                        "A nombre de quién queda la cotización."
                    ),
                },
                "presentacion": {
                    "type": "string",
                    "description": (
                        "Presentación que eligió, si ya la dijo. P. ej. 'costal de "
                        "25 kg' o 'costal de 50 kg'."
                    ),
                },
                "tipo_costal": {
                    "type": "string",
                    "enum": list(TIPOS_COSTAL),
                    "description": "Costal con nuestra marca, sin marca o transparente.",
                },
                "sucursal": {
                    "type": "string",
                    "description": (
                        "Sucursal de la que carga, solo si el cliente la eligió o "
                        "si el precio del catálogo es de una sucursal."
                    ),
                },
                "lugar_de_entrega": {
                    "type": "string",
                    "description": (
                        "Dónde lo recibe, tal como lo dio: ciudad, dirección o la "
                        "ubicación que compartió por WhatsApp (con sus coordenadas)."
                    ),
                },
                "codigo_postal": {
                    "type": "string",
                    "description": "Código postal de entrega, si lo dio (5 dígitos).",
                },
            },
            "required": ["producto", "cantidad_ton", "nombre_cliente"],
        },
    },
    {
        "name": "consultar_contrato",
        "description": "Consulta el estado y detalles de un contrato por su folio.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio": {
                    "type": "string",
                    "description": "Folio del contrato, p. ej. CONT-2026-0001.",
                }
            },
            "required": ["folio"],
        },
    },
    {
        "name": "listar_contratos_cliente",
        "description": (
            "Lista los contratos asociados al teléfono del cliente que escribe."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "solicitar_pedido",
        "description": (
            "Registra una solicitud de pedido (producto y toneladas) para que el "
            "equipo la procese."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {"type": "string", "description": "Producto solicitado."},
                "cantidad_ton": {
                    "type": "number",
                    "description": "Cantidad en toneladas.",
                },
            },
            "required": ["producto", "cantidad_ton"],
        },
    },
    {
        "name": "transferir_a_soporte",
        "description": (
            "Transfiere la conversación al agente de Soporte (dudas sobre órdenes "
            "existentes, reclamos o temas fuera de ventas)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "description": "Motivo de la transferencia."}
            },
            "required": ["motivo"],
        },
    },
]


@dataclass(frozen=True)
class Condiciones:
    """Lo que se calificó antes de cotizar (paso 2 del guion de ventas).

    Todo es opcional: se imprime y se registra solo lo que el cliente dijo. Una
    presentación o un lugar de entrega supuestos terminarían en un PDF que el
    cliente trata como oferta.
    """

    presentacion: str | None = None
    sucursal: str | None = None
    lugar_de_entrega: str | None = None
    codigo_postal: str | None = None

    @classmethod
    def de(cls, tool_input: dict, empresa: str) -> Condiciones:
        def texto(clave: str, largo: int) -> str | None:
            valor = " ".join(str(tool_input.get(clave) or "").split())
            return valor[:largo] or None

        presentacion = texto("presentacion", 60)
        costal = TIPOS_COSTAL.get(str(tool_input.get("tipo_costal") or ""))
        if costal:
            costal = costal.format(empresa=empresa)
            presentacion = f"{presentacion}, {costal}" if presentacion else f"costal {costal}"
        cp = re.sub(r"\D", "", str(tool_input.get("codigo_postal") or ""))
        return cls(
            presentacion=presentacion,
            sucursal=texto("sucursal", 80),
            lugar_de_entrega=texto("lugar_de_entrega", 250),
            codigo_postal=cp if _CODIGO_POSTAL.match(cp) else None,
        )

    @property
    def entrega(self) -> str | None:
        cp = f"C.P. {self.codigo_postal}" if self.codigo_postal else None
        return ", ".join(p for p in (self.lugar_de_entrega, cp) if p) or None

    def notas(self) -> str:
        """Las condiciones, para el campo de notas de la cotización en el CRM.

        Empieza por la condición LAB, con la misma redacción del PDF: el
        vendedor tiene que leer en su tablero lo mismo que el cliente.
        """
        renglones = [CONDICION_LAB]
        if self.presentacion:
            renglones.append(f"Presentación: {self.presentacion}.")
        if self.sucursal:
            renglones.append(f"Carga en: {self.sucursal}.")
        if self.entrega:
            renglones.append(f"Entrega en: {self.entrega}.")
        return "\n".join(renglones)


def folio_cotizacion(telefono: str, ahora: datetime | None = None) -> str:
    """Folio del bot para una cotización: `COT-20260908-064512-5678`.

    Lleva SEGUNDOS y las últimas cifras del teléfono porque el CRM deduplica
    por folio: dos cotizaciones del mismo cliente en el mismo minuto tienen que
    ser dos, no una pisando a la otra.
    """
    marca = (ahora or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"COT-{marca}-{telefono[-4:]}"


class VentasAgent(BaseAgent):
    name = "ventas"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # El PDF no viaja en la respuesta del agente: se manda aquí, como
        # efecto de la herramienta, igual que los documentos en Soporte. Un
        # archivo no es texto al que colgarle botones.
        self._wa = WhatsAppClient()

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def tools(self) -> list[dict]:
        return TOOLS

    async def decorate(self, phone: str, texto: str) -> Reply:
        """Cambia la marca de botones del modelo por los botones de verdad.

        Sin marca, la respuesta lleva "Menú" y "Asesor": el asesor es la
        salida que el guion pide para todo lo que este agente no puede
        autorizar, y tiene que estar a un toque.
        """
        limpio, paso = leer_marca(texto)
        # Un mensaje que era solo la marca no puede salir vacío (Meta lo
        # rechaza) ni con la marca a la vista.
        limpio = limpio or "¿Me ayuda a elegir una opción, por favor?"
        if paso == PASO_UBICACION:
            return Reply(texto=limpio, pedir_ubicacion=True)
        if paso in BOTONES_COTIZACION:
            return Reply(texto=limpio, botones=list(BOTONES_COTIZACION[paso]))
        return Reply(texto=limpio, botones=list(BOTONES_VENTAS))

    async def run_tool(self, name: str, tool_input: dict, caller_phone: str) -> str:
        try:
            if name == "listar_productos":
                try:
                    catalogo = await self._crm.catalogo()
                except CRMNoDisponible as exc:
                    logger.warning("CRM no disponible al listar productos: %s", exc)
                    return self._sin_precio("crm_no_disponible")
                if catalogo.desactualizado:
                    return self._sin_precio(
                        "datos_no_confiables", ultima_actualizacion=catalogo.ultima_sync
                    )
                return json.dumps(
                    {
                        "disponible": True,
                        "actualizado_el": catalogo.ultima_sync,
                        "productos": [
                            {
                                "producto": p.nombre,
                                "unidad": p.unidad,
                                "precio_ton": p.precio_unitario,
                                "moneda": p.moneda,
                                "disponibilidad": p.disponibilidad,
                            }
                            for p in catalogo.productos
                        ],
                    },
                    ensure_ascii=False,
                )

            if name == "consultar_precio":
                consulta = tool_input["producto"]
                encontrado = await self._precio_de(consulta)
                if isinstance(encontrado, str):
                    return encontrado
                producto, catalogo = encontrado
                return json.dumps(
                    {
                        "disponible": True,
                        "producto": producto.nombre,
                        "precio_ton": producto.precio_unitario,
                        "moneda": producto.moneda,
                        "unidad": producto.unidad,
                        "disponibilidad": producto.disponibilidad,
                        # La existencia en toneladas NO viaja al modelo. El
                        # prompt se lo prohíbe, pero un prompt se puede rodear
                        # y un dato que no está no se puede decir: cuánto grano
                        # hay en los silos no sale por WhatsApp.
                        "actualizado_el": catalogo.ultima_sync,
                    },
                    ensure_ascii=False,
                )

            if name == "generar_cotizacion":
                return await self._cotizar(tool_input, caller_phone)

            if name == "consultar_contrato":
                # Se busca entre los contratos DE ESTE TELÉFONO, no por folio
                # global (regla 9): los folios son consecutivos, y desde que
                # la página manda a cualquiera directo a Ventas, adivinar
                # CONT-2026-0002 no puede bastar para leer el contrato de otro.
                folio = str(tool_input["folio"]).strip().upper()
                propios = await self._erp.list_orders_by_phone(caller_phone)
                order = next((o for o in propios if o.id.upper() == folio), None)
                if order is None:
                    return json.dumps(
                        {"encontrado": False, "folio": tool_input["folio"]},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"encontrado": True, "contrato": order.model_dump()},
                    ensure_ascii=False,
                )

            if name == "listar_contratos_cliente":
                orders = await self._erp.list_orders_by_phone(caller_phone)
                return json.dumps(
                    {
                        "telefono": caller_phone,
                        "total": len(orders),
                        "contratos": [o.model_dump() for o in orders],
                    },
                    ensure_ascii=False,
                )

            if name == "solicitar_pedido":
                solicitud = await self._erp.create_request(
                    tool_input["producto"], float(tool_input["cantidad_ton"]), caller_phone
                )
                data = solicitud.model_dump()
                await self._bus.publish(
                    f"bus:ventas:solicitud:{caller_phone}", data, ttl=86400
                )
                return json.dumps(data, ensure_ascii=False)

            if name == "transferir_a_soporte":
                motivo = tool_input.get("motivo", "(sin especificar)")
                await self._bus.set_active_agent(caller_phone, "soporte")
                logger.info("Transferencia ventas->soporte (%s): %s", caller_phone, motivo)
                return json.dumps(
                    {
                        "transferido": True,
                        "agente": "soporte",
                        "mensaje": "Le paso con el equipo de soporte para ayudarle con eso.",
                    },
                    ensure_ascii=False,
                )

            return json.dumps({"error": f"herramienta desconocida: {name}"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error ejecutando herramienta %s", name)
            return json.dumps({"error": str(exc)})

    # --- Cotizar: el PDF al cliente, la nota al vendedor -------------------- #

    async def _cotizar(self, tool_input: dict, telefono: str) -> str:
        """Registra la cotización, le manda el PDF y deja el resumen al vendedor.

        El orden importa: el PDF se arma ANTES de hablarle al CRM para que el
        archivo que se guarda en el tablero sea, byte por byte, el mismo que
        recibió el cliente. Si se generara después, un vendedor podría estar
        viendo una versión y el cliente otra.
        """
        consulta = tool_input["producto"]
        cantidad = float(tool_input["cantidad_ton"])
        nombre = str(tool_input["nombre_cliente"]).strip()
        if not nombre:
            return self._sin_precio("falta_nombre_cliente")

        encontrado = await self._precio_de(consulta)
        if isinstance(encontrado, str):
            return encontrado
        producto, catalogo = encontrado

        # `precio_unitario` no puede ser None aquí: `_precio_de` ya lo
        # descartó con `sin_precio_publicado`. La aserción es para que un
        # cambio futuro rompa aquí y no en el total de un cliente.
        assert producto.precio_unitario is not None

        settings = get_settings()
        condiciones = Condiciones.de(tool_input, settings.company_name)
        # De qué campaña vino, si escribió desde la página. Va al CRM con la
        # cotización: es la llave con la que recupera la visita completa.
        referencia = await ReferenciasWeb(self._bus).leer(telefono)
        folio = folio_cotizacion(telefono)
        hoy = datetime.now(UTC).date()
        vence = vigencia(settings.cotizacion_vigencia_dias, hoy)
        pdf, archivo = self._armar_pdf(
            DatosCotizacion(
                folio=folio,
                cliente=nombre,
                producto=producto.nombre,
                cantidad_ton=cantidad,
                precio_ton=producto.precio_unitario,
                empresa=settings.company_name,
                moneda=producto.moneda,
                telefono=telefono,
                precio_actualizado_el=catalogo.ultima_sync,
                emitida=hoy,
                vigencia_hasta=vence,
                tasa_iva=settings.cotizacion_iva_tasa,
                presentacion=condiciones.presentacion,
                sucursal=condiciones.sucursal,
                entrega=condiciones.entrega,
            )
        )

        try:
            cotizacion = await self._crm.registrar_cotizacion(
                folio=folio,
                nombre_cliente=nombre,
                telefono=telefono,
                producto=producto.nombre,
                cantidad_ton=cantidad,
                precio_ton=producto.precio_unitario,
                moneda=producto.moneda,
                pdf=pdf,
                pdf_nombre=archivo,
                vigencia_hasta=vence,
                tasa_iva=settings.cotizacion_iva_tasa,
                notas=condiciones.notas(),
                presentacion=condiciones.presentacion,
                codigo_postal=condiciones.codigo_postal,
                referencia_contacto=referencia,
            )
        except CRMNoDisponible as exc:
            logger.warning("CRM no disponible al cotizar: %s", exc)
            return self._sin_precio("crm_no_disponible")

        envio = await self._mandar_pdf(telefono, cotizacion, pdf, archivo)
        data = cotizacion.model_dump()
        await self._bus.publish(
            f"bus:ventas:cotizacion:{telefono}", {**data, **envio}, ttl=86400
        )
        # El resumen va en segundo plano: lo que el cliente está esperando es
        # su PDF, no que se acabe de escribir una nota interna.
        tareas.lanzar(
            self._nota_para_el_vendedor(
                telefono, cotizacion, envio, condiciones, referencia
            ),
            nombre=f"resumen-cotizacion:{folio}",
        )
        return json.dumps({"disponible": True, **data, **envio}, ensure_ascii=False)

    @staticmethod
    def _armar_pdf(datos: DatosCotizacion) -> tuple[bytes | None, str | None]:
        """Los bytes del PDF, o (None, None) si no se pudo armar.

        Un fallo aquí no cancela la cotización: queda registrada y el vendedor
        la ve en su tablero. Perder el archivo es un mal menor; perder la
        cotización —y con ella al cliente que la pidió— no lo es.
        """
        try:
            return construir(datos), nombre_archivo(datos.folio)
        except Exception:  # noqa: BLE001 - se sigue sin PDF, no se cae la venta
            logger.exception("No se pudo armar el PDF de %s", datos.folio)
            return None, None

    async def _mandar_pdf(
        self,
        telefono: str,
        cotizacion: CotizacionCRM,
        pdf: bytes | None,
        archivo: str | None,
    ) -> dict:
        """Le manda el PDF al cliente, salvo que deba verlo antes un vendedor.

        Devuelve un motivo distinto por causa, como `enviar_mi_documento`: con
        un solo "no se pudo" para todo, el modelo rellena el hueco y le
        explica al cliente una causa que nadie le dio.
        """
        if cotizacion.modo_cotizacion != "automatic":
            # El CRM manda. En modo manual una persona revisa el precio antes
            # de que el cliente lo vea, y mandarle el PDF sería exactamente lo
            # que ese modo existe para evitar.
            return {
                "pdf_enviado": False,
                "motivo_pdf": "requiere_revision_de_vendedor",
                "instruccion": (
                    "NO le des el total ni le digas que ya le mandaste algo. "
                    "Dile que un asesor revisa su cotización y se la hace "
                    "llegar formalmente."
                ),
            }
        if pdf is None or archivo is None:
            return {
                "pdf_enviado": False,
                "motivo_pdf": "no_se_pudo_generar",
                "instruccion": (
                    "Su cotización SÍ quedó registrada con el folio que traes, "
                    "pero no pudiste generar el archivo. Dale el folio, dile "
                    "que un asesor le hace llegar el documento y NO digas que "
                    "ya se lo enviaste."
                ),
            }
        try:
            media_id = await self._wa.upload_media(pdf, archivo, "application/pdf")
            await self._wa.send_document(telefono, media_id, archivo)
        except Exception as exc:  # noqa: BLE001 - el motivo real, no "hubo un error"
            motivo = detalle_http(exc, "Meta")
            logger.error("No se pudo enviar %s a %s: %s", archivo, telefono, motivo)
            return {
                "pdf_enviado": False,
                "motivo_pdf": "fallo_al_enviar",
                "detalle": motivo,
                "instruccion": (
                    "La cotización quedó registrada pero el envío del archivo "
                    "falló. Dale el folio y ofrécele pasarlo con un asesor. NO "
                    "digas que ya le llegó el documento."
                ),
            }
        return {"pdf_enviado": True, "documento": archivo}

    async def _nota_para_el_vendedor(
        self,
        telefono: str,
        cotizacion: CotizacionCRM,
        envio: dict,
        condiciones: Condiciones | None = None,
        referencia: str | None = None,
    ) -> None:
        """Deja en el CRM el resumen de lo que se habló, junto a la cotización.

        El resumen se arma con el historial GUARDADO, que no incluye todavía
        el mensaje que el cliente acaba de mandar: el turno en curso se
        persiste al final, después de esta herramienta. Por eso los hechos
        —qué pidió, cuánto, si ya recibió el PDF— viajan aparte y salen de la
        cotización, no de la plática: así la nota dice lo esencial aunque la
        transcripción venga corta.
        """
        historial = await self._history_store.load(self._history_key(telefono))
        texto = await resumen.redactar(
            hechos=self._hechos(telefono, cotizacion, envio, condiciones, referencia),
            historial=historial,
        )

        for intento in range(1, INTENTOS_NOTA + 1):
            try:
                await self._crm.registrar_nota_cotizacion(
                    folio=cotizacion.folio, resumen=texto
                )
                return
            except CotizacionAunNoRegistrada as exc:
                logger.info(
                    "El CRM aún no ve %s (intento %s/%s): %s",
                    cotizacion.folio,
                    intento,
                    INTENTOS_NOTA,
                    exc,
                )
                if intento < INTENTOS_NOTA:
                    await asyncio.sleep(ESPERA_NOTA_SEGUNDOS)
            except CRMNoDisponible as exc:
                # La cotización y su PDF ya están en el CRM; lo que se pierde
                # es el contexto. No se insiste: insistir contra un CRM caído
                # no lo levanta.
                logger.warning("No se pudo dejar la nota de %s: %s", cotizacion.folio, exc)
                return
        logger.warning("Se agotaron los intentos de dejar la nota de %s", cotizacion.folio)

    @staticmethod
    def _hechos(
        telefono: str,
        cotizacion: CotizacionCRM,
        envio: dict,
        condiciones: Condiciones | None = None,
        referencia: str | None = None,
    ) -> list[str]:
        """Lo que el vendedor tiene que leer aunque el resumen salga vacío."""
        condiciones = condiciones or Condiciones()
        hechos = [
            f"- Pidió {cotizacion.producto}, {cotizacion.cantidad_ton:,.3f} t. "
            f"Escribió por WhatsApp desde el {telefono}."
        ]
        if referencia:
            hechos.append(f"- Llegó por el WhatsApp de la página web (ref {referencia}).")
        if condiciones.presentacion:
            hechos.append(f"- Presentación: {condiciones.presentacion}.")
        if condiciones.sucursal:
            hechos.append(f"- Carga en: {condiciones.sucursal}.")
        # El flete se cotiza con la ubicación: decir si falta es decirle al
        # vendedor qué tiene que pedir antes de poder pasarle el total.
        hechos.append(
            f"- Entrega en: {condiciones.entrega}. Falta cotizarle el flete."
            if condiciones.entrega
            else "- No dio lugar de entrega: hay que pedírselo para cotizar el flete."
        )
        if envio.get("pdf_enviado"):
            hechos.append("- Ya recibió el PDF de la cotización por WhatsApp.")
        elif envio.get("motivo_pdf") == "requiere_revision_de_vendedor":
            hechos.append(
                "- NO se le envió el PDF: la cotización está en modo de revisión, "
                "hay que aprobarla y enviársela."
            )
        else:
            hechos.append(
                "- NO se le pudo enviar el PDF; se le dijo que un asesor se lo "
                "hace llegar."
            )
        return hechos

    # --- helpers ----------------------------------------------------------- #

    @staticmethod
    def _sin_precio(motivo: str, **extra: object) -> str:
        """El "no puedo darte un precio", con la causa REAL y ninguna otra.

        Un motivo por causa, y el prompt tiene prohibido mezclarlos. Con un solo
        motivo para todo, el modelo rellena el hueco: es cómo el bot terminó
        diciéndole a un cliente que no manejamos algo que sí manejamos.
        """
        return json.dumps({"disponible": False, "motivo": motivo, **extra}, ensure_ascii=False)

    async def _precio_de(self, consulta: str):
        """El producto con precio, o el JSON del motivo por el que no lo hay.

        Devolver dos cosas distintas es feo, pero la alternativa —repetir esta
        escalera en cada tool— es peor: el precio y la cotización tienen que
        rechazar por los MISMOS motivos, o el bot cotizaría lo que dijo que no
        podía cotizar.
        """
        try:
            catalogo = await self._crm.catalogo()
        except CRMNoDisponible as exc:
            logger.warning("CRM no disponible al consultar precio: %s", exc)
            return self._sin_precio("crm_no_disponible")

        if catalogo.desactualizado:
            # El CRM copió este precio del ERP y avisa que su copia no es de
            # fiar. Decir la cifra igual sería prometerle a un cliente un precio
            # que quizá ya no existe.
            return self._sin_precio(
                "datos_no_confiables", ultima_actualizacion=catalogo.ultima_sync
            )

        producto = buscar_producto(catalogo, consulta)
        if producto is None:
            return self._sin_precio("no_esta_en_catalogo", producto=consulta)
        if producto.precio_unitario is None:
            # Sí se vende; nadie le ha puesto precio. NO es lo mismo que no
            # manejarlo, y contestarlo igual le miente al cliente.
            return self._sin_precio("sin_precio_publicado", producto=producto.nombre)
        return producto, catalogo
