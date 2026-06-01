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
    # cambiarlo a "claude-sonnet-4-6" o "claude-haiku-4-5".
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

    @property
    def use_mock_erp(self) -> bool:
        return not self.erp_base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
