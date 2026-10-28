"""Settings from environment variables only (CLAUDE.md §10). Fails fast."""
from __future__ import annotations

from cryptography.fernet import Fernet
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

NO_REVIEW_DEFAULT = frozenset({"eng", "swh"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    at_username: str
    at_api_key: SecretStr
    at_shortcode: str | None = None
    at_sender_id: str | None = None

    database_url: str = "sqlite:///./lugha.db"

    phone_enc_key: SecretStr
    phone_hash_salt: SecretStr
    admin_token: SecretStr

    translate_api_url: str = "https://mpily-bantugrammar.hf.space/api/translate"
    customer_model_url: str | None = None
    customer_model_key: SecretStr | None = None
    customer_model_request_template: str | None = None
    customer_model_response_path: str | None = None
    network_api_url: str | None = None
    shared_data_url: str | None = None

    # "*" = every language except eng and swh; otherwise a comma list.
    review_required_langs: str = "*"

    payout_per_item_kes: int = 100
    trusted_timeout_min: int = 15
    speaker_timeout_min: int = 30
    fanout_wave_size: int = 5
    review_deadline_min: int = 120
    gold_rate: float = 0.1
    min_accept_rate: float = 0.6
    min_reply_seconds: int = 10

    @field_validator("phone_enc_key")
    @classmethod
    def _valid_fernet(cls, v: SecretStr) -> SecretStr:
        try:
            Fernet(v.get_secret_value().encode())
        except Exception as e:  # noqa: BLE001
            raise ValueError("PHONE_ENC_KEY must be a Fernet key "
                             "(python -c 'from cryptography.fernet import Fernet;"
                             "print(Fernet.generate_key().decode())')") from e
        return v

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if not (self.at_shortcode or self.at_sender_id):
            raise ValueError("set AT_SHORTCODE or AT_SENDER_ID")
        if self.payout_per_item_kes < 2 or self.payout_per_item_kes % 2:
            raise ValueError("PAYOUT_PER_ITEM_KES must be an even number >= 2 "
                             "(it is split between two responders)")
        if not 0 <= self.gold_rate < 1 or not 0 <= self.min_accept_rate <= 1:
            raise ValueError("GOLD_RATE must be in [0,1), MIN_ACCEPT_RATE in [0,1]")
        for name in ("trusted_timeout_min", "speaker_timeout_min",
                     "fanout_wave_size", "review_deadline_min"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name.upper()} must be > 0")
        return self

    def needs_review(self, lang: str) -> bool:
        lang = lang.strip().lower()
        spec = self.review_required_langs.strip()
        if spec == "*":
            return lang not in NO_REVIEW_DEFAULT
        return lang in {x.strip().lower() for x in spec.split(",") if x.strip()}
