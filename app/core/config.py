from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # App
    app_name: str = "Apna Business"
    app_version: str = "1.0.0"
    debug: bool = True
    api_prefix: str = "/api/v1"

    # Database
    database_url: str

    # Redis
    redis_url: str

    # JWT
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080  # 7 days

    # OTP
    otp_secret_key: str = "otp-secret-change-in-production"
    otp_expiry_minutes: int = 10
    otp_max_attempts: int = 5
    expose_test_otp: bool = True

    # MSG91
    msg91_auth_key: str = ""
    msg91_sender_id: str = "APNAIZ"
    msg91_template_id: str = ""

    # WATI
    wati_api_endpoint: str = ""
    wati_access_token: str = ""

    # Razorpay
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""

    # Anthropic
    anthropic_api_key: str = ""

    # OpenAI
    openai_api_key: str = ""

    # Google Gemini
    gemini_api_key: str = ""

    # Groq
    groq_api_key: str = ""

    # MuRIL NLP
    muril_enabled: bool = True
    muril_model_name: str = "google/muril-base-cased"
    muril_cache_dir: str = ".muril_cache"
    muril_similarity_threshold: float = 0.60

    # Cloudflare R2
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""
    r2_public_url: str = ""

    model_config = {"env_file": ".env", "case_sensitive": False, "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
