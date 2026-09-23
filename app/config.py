from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://studybuddy:studybuddy@localhost:5432/studybuddy"
    moodle_base_url: str = "https://elearning.strathmore.edu"
    moodle_token: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_chunk_model: str = "gemini-3.6-flash"
    llm_quiz_model: str = "gemini-3.6-flash"
    llm_grade_model: str = "gemini-3.6-flash"
    resend_api_key: str = ""
    email_from: str = ""
    email_to: str = ""
    secret_key: str = "dev-insecure-change-me"
    session_secure_cookie: bool = False


settings = Settings()
