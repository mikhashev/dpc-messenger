# dpc-hub/dpc_hub/settings.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # URL Exmaple: postgresql+asyncpg://user:password@localhost/dpc_hub
    DATABASE_URL: str

    class Config:
        env_file = ".env"

settings = Settings()