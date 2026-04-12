from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings, pulling completely from environment variables.
    Fails fast if required variables are missing at startup.
    """
    
    # --- LLM Provider (Google Gemini) ---
    gemini_api_key: str = Field(..., description="Google Gemini API Key for langchain-google-genai")

    # --- Database / Persistence (Supabase PostgreSQL) ---
    supabase_database_url: str = Field(..., description="Async connection string for Supabase")

    # --- Caching / State Storage (Upstash Redis) ---
    upstash_redis_rest_url: str = Field(..., description="Upstash Redis REST URL for caching")
    upstash_redis_rest_token: str = Field(..., description="Upstash Redis REST Token for caching")

    # --- Observability & Tracing (Langfuse) ---
    langfuse_public_key: str = Field(..., description="Langfuse Public Key for tracing")
    langfuse_secret_key: str = Field(..., description="Langfuse Secret Key for tracing")
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com", 
        description="Langfuse instance URL"
    )

    # --- Application Setup ---
    environment: str = Field(
        default="development", 
        description="App environment (e.g., development, staging, production)"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"  # Ignore undocumented environment variables
    )


# Instantiate specific settings object to be imported across the application
settings = Settings()
