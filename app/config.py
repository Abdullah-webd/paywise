"""Central configuration. All secrets/settings live in environment variables."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- app ---
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_base_url: str = "http://localhost:8000"

    @property
    def port(self) -> int:
        """Use PORT env var (Railway/Render) when available, otherwise app_port."""
        import os
        return int(os.environ.get("PORT", self.app_port))

    @property
    def base_url(self) -> str:
        """Use RAILWAY_PUBLIC_DOMAIN / RENDER_EXTERNAL_URL when deployed."""
        import os
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RENDER_EXTERNAL_URL")
        if domain:
            return f"https://{domain}"
        return self.app_base_url.rstrip("/")

    # --- mongo ---
    mongodb_uri: str
    mongodb_db_name: str = "paywise"

    # --- openai ---
    openai_api_key: str
    openai_model: str = "gpt-5"

    # --- elevenlabs ---
    elevenlabs_api_key: str
    elevenlabs_stt_model: str = "scribe_v1"

    # --- gemini tts ---
    gemini_api_key: str
    gemini_tts_model: str = "gemini-3.1-flash-tts-preview"
    gemini_tts_voice: str = "Zephyr"

    # --- twilio ---
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    twilio_sms_from: str = ""          # Twilio phone number for SMS (e.g. +1234567890)

    # --- nomba ---
    nomba_base_url: str = "https://sandbox.nomba.com"
    nomba_client_id: str
    nomba_client_key: str
    nomba_account_id: str           # PARENT account ID — goes in the `accountId` header
    nomba_sub_account_id: str       # SUB-account ID — scopes VAs/transfers in the body
    nomba_webhook_secret: str
    nomba_virtual_account_ttl_hours: int = 48   # fallback only — due_date drives expiry
    collection_grace_days: int = 2              # extra days past due_date before expiry

    @property
    def is_dev(self) -> bool:
        return self.app_env != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
