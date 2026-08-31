"""Configuración cargada desde variables de entorno (o un archivo .env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Claude / Anthropic ---
    anthropic_api_key: str = ""
    # Opus 4.8 es el modelo por defecto. Para alto volumen y menor costo puedes
    # cambiarlo a "claude-sonnet-5" o "claude-haiku-4-5".
    #
    # El ID lleva versión SIEMPRE: "claude-sonnet" a secas no existe y la API
    # contesta 404. Ese 404 no se ve en WhatsApp —el except general de
    # _process_message lo convierte en "Tuvimos un inconveniente técnico"—, así
    # que un ID mal escrito rompe TODAS las conversaciones y solo se descubre
    # leyendo logs. Si el bot contesta siempre lo mismo, revisa esto primero.
    claude_model: str = "claude-opus-4-8"

    # --- WhatsApp Cloud API (Meta) ---
    whatsapp_token: str = ""              # token del system user / acceso permanente
    whatsapp_phone_number_id: str = ""    # ID del número emisor (no el número en sí)
    whatsapp_verify_token: str = "intergranel-verify"  # para verificar el webhook (GET)
    # App Secret de la app de Meta, para validar la firma X-Hub-Signature-256 de
    # los webhooks entrantes. Si se deja vacío, la verificación se omite (modo
    # desarrollo). Configúralo en producción.
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"
    # Plantilla aprobada para notificaciones proactivas (mensajes iniciados por el negocio).
    # Si se deja vacío, las notificaciones se envían como texto libre (solo válido
    # dentro de la ventana de 24h de servicio al cliente).
    whatsapp_order_template: str = ""
    whatsapp_template_language: str = "es_MX"
    # Plantilla aprobada para los AVISOS INTERNOS del ERP al equipo (categoría
    # Utility). Es obligatoria en producción: un vencimiento que se avisa a las
    # 7:30 casi nunca cae dentro de la ventana de 24h, y sin plantilla Meta
    # rechaza el mensaje. Vacía = texto libre (desarrollo y dentro de ventana).
    # Parámetros del cuerpo, en orden: {{1}} título, {{2}} detalle, {{3}} empresa.
    whatsapp_aviso_template: str = ""

    # --- ERP / API externo de órdenes ---
    # Si erp_base_url está vacío, se usa un ERP simulado en memoria (para desarrollo).
    # erp_base_url debe incluir el prefijo de la API, p. ej.:
    #   https://erp-intergranel.example.com/api/v1
    erp_base_url: str = ""
    erp_api_key: str = ""
    # Si se define, la API key se envía en este header (p. ej. "X-Bot-Api-Key").
    # Si se deja vacío y hay erp_api_key, se envía como "Authorization: Bearer ...".
    erp_api_key_header: str = ""

    # --- Seguridad del webhook entrante de notificaciones del ERP ---
    erp_webhook_secret: str = ""

    # --- Persistencia del historial de conversación ---
    # Si redis_url está vacío, el historial se guarda en memoria (desarrollo).
    redis_url: str = ""
    history_ttl_seconds: int = 60 * 60 * 24 * 7  # 7 días
    # TTL de los ids de mensaje ya procesados (deduplicación de webhooks).
    dedup_ttl_seconds: int = 60 * 60 * 24  # 1 día

    # --- Identidad ---
    company_name: str = "Intergranel"

    # --- Agente de Compras ---
    # Lista blanca de teléfonos autorizados a usar el agente de Compras, en
    # formato internacional sin '+', separados por comas. Si se deja vacía, no
    # se aplica restricción (modo desarrollo).
    compras_phones_allowed: str = ""

    # --- Chatwoot: la bandeja donde atiende un asesor humano ---
    # Cuando el cliente pide una persona, la conversación pasa a Chatwoot y el
    # bot se vuelve un cable entre WhatsApp y esa bandeja.
    #
    # El bot CONSERVA el número de WhatsApp (no se le cede a Chatwoot) porque
    # Chatwoot no manda mensajes interactivos por la Cloud API: cederle el
    # número cambiaría los menús de botones por texto plano en todas las
    # conversaciones para ganar comodidad solo en las que llegan a un asesor.
    #
    # Vacío = deshabilitado: escalar avisa que no se pudo y lo deja en el log.
    # "mock" = Chatwoot simulado en memoria (desarrollo).
    chatwoot_base_url: str = ""
    chatwoot_api_token: str = ""
    chatwoot_account_id: int = 0
    chatwoot_inbox_id: int = 0
    # Secreto compartido que Chatwoot manda en el webhook de vuelta. Chatwoot no
    # firma sus webhooks, así que sin esto cualquiera podría hacerle decir al bot
    # lo que quisiera por WhatsApp. Vacío = sin verificar (solo desarrollo).
    chatwoot_webhook_secret: str = ""
    # Cuánto dura como mucho una conversación en manos del asesor. Existe para
    # que nadie se quede hablándole al vacío si la conversación no se resuelve:
    # al vencer, el bot retoma. Un turno de trabajo por defecto.
    handoff_ttl_seconds: int = 8 * 60 * 60

    # --- Alertas de inventario ---
    # Teléfonos del equipo que reciben las alertas proactivas de inventario,
    # en formato internacional sin '+', separados por comas. Si se deja vacía,
    # las alertas solo se registran en el bus y el log (no se envían por WhatsApp).
    inventory_alert_phones: str = ""

    @property
    def use_mock_erp(self) -> bool:
        return not self.erp_base_url

    @property
    def compras_allowed_set(self) -> set[str]:
        return {p.strip() for p in self.compras_phones_allowed.split(",") if p.strip()}

    @property
    def inventory_alert_list(self) -> list[str]:
        return [p.strip() for p in self.inventory_alert_phones.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
