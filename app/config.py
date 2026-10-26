from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""

    # Meta WhatsApp Cloud API (preferred for Guide when set)
    whatsapp_token: str = Field(default="", validation_alias=AliasChoices("WHATSAPP_TOKEN", "META_WA_TOKEN"))
    whatsapp_phone_number_id: str = Field(
        default="",
        validation_alias=AliasChoices("WHATSAPP_PHONE_NUMBER_ID", "META_WA_PHONE_NUMBER_ID"),
    )
    whatsapp_verify_token: str = Field(
        default="",
        validation_alias=AliasChoices("WHATSAPP_VERIFY_TOKEN", "META_WA_VERIFY_TOKEN"),
    )
    whatsapp_graph_version: str = Field(default="v21.0", validation_alias=AliasChoices("GRAPH_API_VERSION", "WHATSAPP_GRAPH_VERSION"))
    meta_app_secret: str = Field(default="", validation_alias=AliasChoices("META_APP_SECRET", "APP_SECRET"))

    guide_public_url: str = ""
    guide_db_path: str = "data/guide.db"

    # DeepSeek — chat, CV, cover drafts
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    deepseek_reason_model: str = "deepseek-reasoner"

    # Gemini — optional. Flight search itself does not wait on it.
    gemini_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GEMINI_API_KEY", "Gemini_API_KEY"),
    )
    gemini_model: str = "gemini-flash-lite-latest"

    vault_secret_key: str = ""
    log_level: str = "INFO"

    # Indian residential proxy (DataImpulse). Used for booking Chromium on Render.
    proxy_host: str = Field(default="", validation_alias=AliasChoices("PROXY_HOST"))
    proxy_port: str = Field(default="", validation_alias=AliasChoices("PROXY_PORT"))
    proxy_username: str = Field(default="", validation_alias=AliasChoices("PROXY_USERNAME"))
    proxy_password: str = Field(default="", validation_alias=AliasChoices("PROXY_PASSWORD"))
    proxy_search: bool = Field(default=False, validation_alias=AliasChoices("PROXY_SEARCH"))

    # Hetzner (or other VPS) Chromium worker. When set, booking runs there, not on Render.
    booking_worker_url: str = Field(default="", validation_alias=AliasChoices("BOOKING_WORKER_URL"))
    booking_worker_token: str = Field(default="", validation_alias=AliasChoices("BOOKING_WORKER_TOKEN"))

    # Kernel cloud browser for Ixigo and Cleartrip WhatsApp OTP login.
    kernel_api_key: str = Field(default="", validation_alias=AliasChoices("KERNEL_API_KEY"))

    @property
    def twilio_ready(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_whatsapp_from)

    @property
    def proxy_ready(self) -> bool:
        return bool(self.proxy_host and self.proxy_port and self.proxy_username and self.proxy_password)

    def playwright_proxy(self) -> dict[str, str] | None:
        """Playwright proxy dict, or None when unset."""
        if not self.proxy_ready:
            return None
        host = self.proxy_host.strip()
        port = self.proxy_port.strip()
        if host.startswith(("http://", "https://", "socks5://")):
            server = host if host.rsplit(":", 1)[-1].isdigit() else f"{host}:{port}"
        else:
            server = f"http://{host}:{port}"
        return {
            "server": server,
            "username": self.proxy_username.strip(),
            "password": self.proxy_password.strip(),
        }

    @property
    def meta_wa_ready(self) -> bool:
        return bool(self.whatsapp_token and self.whatsapp_phone_number_id)

    @property
    def meta_verify_ready(self) -> bool:
        return bool(self.whatsapp_verify_token)

    @property
    def llm_ready(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def search_ready(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def vault_ready(self) -> bool:
        return bool(self.vault_secret_key) and len(self.vault_secret_key) >= 16

    @property
    def booking_worker_ready(self) -> bool:
        return bool(self.booking_worker_url.strip() and self.booking_worker_token.strip())

    @property
    def kernel_ready(self) -> bool:
        return bool(self.kernel_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
