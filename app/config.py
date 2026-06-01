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
    erp_base_url: str = ""
    erp_api_key: str = ""

    # --- Seguridad del webhook entrante de notificaciones del ERP ---
    erp_webhook_secret: str = ""

    # --- Identidad ---
    company_name: str = "Intergranel"

    @property
    def use_mock_erp(self) -> bool:
        return not self.erp_base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
