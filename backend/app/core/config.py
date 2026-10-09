import os


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", "sqlite:///./affiliate.db")
        self.secret_key = os.getenv("SECRET_KEY", "dev-only-secret-change-me-in-production-0123456789")
        self.access_token_minutes = int(os.getenv("ACCESS_TOKEN_MINUTES", "720"))
        self.public_base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
        self.payment_webhook_secret = os.getenv("PAYMENT_WEBHOOK_SECRET", "dev-webhook-secret")
        # Demo payout provider: lets staff simulate provider callbacks. Disable in production.
        self.simulate_provider = _bool("SIMULATE_PROVIDER", "true")
        # Returns the password-reset token in the API response (dev only; production sends email).
        self.expose_reset_token = _bool("EXPOSE_RESET_TOKEN", "false")
        self.cors_origins = [o for o in os.getenv("CORS_ORIGINS", "").split(",") if o]


settings = Settings()
