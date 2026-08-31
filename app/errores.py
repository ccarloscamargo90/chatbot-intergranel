"""El motivo REAL de un fallo HTTP, en una línea que se pueda guardar y leer.

`str(httpx.HTTPStatusError)` dice "Server error '503 Service Unavailable' for
url '...'": el status y nada más. El diagnóstico está en el cuerpo de la
respuesta —Meta explica qué rechazó, el ERP manda su propio mensaje— y ahí es
donde se queda si nadie lo saca.

No es un lujo de logs: cuando una herramienta del agente falla, lo que devuelve
es lo único que el modelo tiene para contestarle al cliente. Con un mensaje
mudo, el modelo rellena el hueco — y lo que rellenó en producción fue "el
módulo de envío no las está entregando", que no era cierto.
"""

from __future__ import annotations

from typing import Any

#: Etiqueta de origen a deducir del host de la respuesta.
AUTO = "__auto__"


def _host(respuesta: Any) -> str:
    peticion = getattr(respuesta, "request", None)
    return getattr(getattr(peticion, "url", None), "host", "") or ""


def _texto(valor: Any) -> str:
    """Un `message` puede venir como string o como lista (Nest + class-validator)."""
    if isinstance(valor, list):
        return "; ".join(str(v) for v in valor if v)
    return str(valor) if valor else ""


def detalle_http(exc: Exception, origen: str) -> str:
    """`"Meta 400: ..."` / `"ERP 409: ..."`, o el tipo del error si no hubo respuesta."""
    respuesta = getattr(exc, "response", None)
    if respuesta is None:
        return f"{type(exc).__name__}: {exc}"
    return detalle_respuesta(respuesta, origen)


def detalle_respuesta(respuesta: Any, origen: str) -> str:
    """Lo mismo, a partir de la respuesta cruda (no todo fallo es una excepción).

    Entiende las dos formas de cuerpo que recibimos:

    - Meta: ``{"error": {"message": ..., "error_data": {"details": ...}}}``
    - ERP (filtro global de Nest): ``{"message": ..., "error": ...}``

    `origen` es la etiqueta que se antepone. Donde quien llama no puede saber a
    qué servicio le habló —el `except` general de una tool, que cubre ERP,
    Chatwoot y Meta a la vez— se pasa `AUTO` y sale el host real: una etiqueta
    equivocada en un diagnóstico manda a revisar el sistema que no era.
    """
    if origen == AUTO:
        origen = _host(respuesta) or "HTTP"

    try:
        cuerpo = respuesta.json()
    except Exception:  # noqa: BLE001 - el cuerpo puede no ser JSON
        cuerpo = {}

    partes: list[str] = []
    if isinstance(cuerpo, dict):
        error = cuerpo.get("error")
        if isinstance(error, dict):  # Meta
            partes = [
                _texto(error.get("message")),
                _texto((error.get("error_data") or {}).get("details")),
            ]
        else:  # ERP
            partes = [_texto(cuerpo.get("message")), _texto(error)]

    partes = [p for p in partes if p]
    if partes:
        return f"{origen} {respuesta.status_code}: {' · '.join(partes)}"
    return f"{origen} {respuesta.status_code}: {respuesta.text[:300]}"


def codigo_erp(respuesta: Any) -> str:
    """El campo `error` del cuerpo del ERP: su código estable de fallo.

    Se lee el código y no el status porque un mismo status llega por varias
    razones (un 503 es tanto "no bajé el archivo" como "la base no responde") y
    el agente tiene que contestar distinto en cada caso.
    """
    try:
        cuerpo = respuesta.json()
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(cuerpo, dict):
        return ""
    error = cuerpo.get("error")
    return error if isinstance(error, str) else ""
