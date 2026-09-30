import os
import warnings
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_SECRET_KEY = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://studybuddy:studybuddy@localhost:5432/studybuddy"
    moodle_base_url: str = "https://elearning.strathmore.edu"
    moodle_token: str = ""
    moodle_token_owner: str = ""  # the only email allowed to use MOODLE_TOKEN
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_refresh_token_owner: str = ""  # the only email allowed to use GOOGLE_REFRESH_TOKEN
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_chunk_model: str = "gemini-3.6-flash"
    llm_quiz_model: str = "gemini-3.6-flash"
    llm_grade_model: str = "gemini-3.6-flash"
    # grading runs inside a web request, so it fails fast instead of retrying for minutes
    llm_grade_timeout: int = 30
    llm_grade_attempts: int = 2
    llm_pace: float = 0.0  # seconds between pipeline LLM calls; ~5-45 on free tiers
    app_base_url: str = "http://localhost:8000"  # public origin for links in emails
    resend_api_key: str = ""
    email_from: str = ""
    email_to: str = ""
    secret_key: str = INSECURE_SECRET_KEY
    session_secure_cookie: bool = False
    # comma-separated addresses and/or "@domain" entries; empty = any Google account
    allowed_emails: str = "@strathmore.edu"
    healthcheck_ping_url: str = ""  # e.g. healthchecks.io ping on worker success
    timezone: str = "Africa/Nairobi"  # IANA zone whose midnight starts a study day

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @model_validator(mode="after")
    def _require_known_timezone(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"TIMEZONE {self.timezone!r} is not an IANA zone name") from None
        return self

    @model_validator(mode="after")
    def _require_real_secret_in_prod(self):
        # SECRET_KEY signs sessions and encrypts stored tokens; the default is public.
        in_prod = self.session_secure_cookie or any(
            os.environ.get(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME")
        )
        if in_prod and self.secret_key in ("", INSECURE_SECRET_KEY):
            raise ValueError("SECRET_KEY must be set to a random value in production")
        return self

    @model_validator(mode="after")
    def _warn_unowned_shared_tokens(self):
        # A shared token acts as one real account, so it is only ever used for
        # its named owner; with no owner it is unused (it used to go to everyone).
        for token, owner in (("MOODLE_TOKEN", "MOODLE_TOKEN_OWNER"),
                             ("GOOGLE_REFRESH_TOKEN", "GOOGLE_REFRESH_TOKEN_OWNER")):
            if getattr(self, token.lower()) and not getattr(self, owner.lower()).strip():
                warnings.warn(f"{token} is set but {owner} is empty, so no user will "
                              f"get it; set {owner} to the account it belongs to",
                              stacklevel=2)
        return self


settings = Settings()
