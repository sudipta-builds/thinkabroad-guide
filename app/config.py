from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""

    guide_public_url: str = ""
    guide_db_path: str = "data/guide.db"

    # DeepSeek — chat, CV, cover drafts
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    # Gemini — optional. Flight search itself does not wait on it.
    gemini_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GEMINI_API_KEY", "Gemini_API_KEY"),
    )
    gemini_model: str = "gemini-flash-lite-latest"

    vault_secret_key: str = ""
    log_level: str = "INFO"

    @property
    def twilio_ready(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_whatsapp_from)

    @property
    def llm_ready(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def search_ready(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def vault_ready(self) -> bool:
        return bool(self.vault_secret_key) and len(self.vault_secret_key) >= 16


@lru_cache
def get_settings() -> Settings:
    return Settings()
