import os
from dotenv import load_dotenv

# Load variables from .env file into environment
load_dotenv()


class Settings:
    """Application settings loaded from environment variables."""

    PORT: int = int(os.getenv("PORT", "8080"))
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    
    # Read comma-separated ALLOWED_ORIGINS from .env (fallback to localhost ports if not set)
    ALLOWED_ORIGINS: list[str] = [
        origin.strip()
        for origin in os.getenv(
            "ALLOWED_ORIGINS",
            "http://localhost:5980,http://localhost:5981,http://localhost:5982"
        ).split(",")
        if origin.strip()
    ]
    
    JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-insecure-secret-change-me")
    JWT_EXPIRES_MINUTES: int = int(os.getenv("JWT_EXPIRES_MINUTES", "525600"))
    JWT_ALGORITHM: str = "HS256"
    SUPABASE_STORAGE_BUCKET: str = os.getenv("SUPABASE_STORAGE_BUCKET", "waterwatch-images")
    OPENWEATHER_API_KEY: str = os.getenv("OPENWEATHER_API_KEY", "")
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    RESEND_FROM_EMAIL: str = os.getenv("RESEND_FROM_EMAIL", "")
    RESIDENT_PORTAL_URL: str = os.getenv("RESIDENT_PORTAL_URL", "http://localhost:5982")


settings = Settings()
