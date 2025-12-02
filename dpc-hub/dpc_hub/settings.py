"""
Configuration settings with validation.

Loads and validates environment variables for the Hub application.
"""

import sys
import logging
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator, ValidationError

logger = logging.getLogger(__name__)


def __get_version() -> str:
    """Helper function to get version from __version__ module."""
    try:
        from .__version__ import __version__
        return __version__
    except Exception:
        return "unknown"


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    
    All settings are validated on startup. Application will exit
    if required settings are missing or invalid.
    """
    
    # Security
    SECRET_KEY: str = Field(
        ..., 
        min_length=32,
        description="Secret key for JWT tokens (min 32 chars)"
    )
    
    # Database
    DATABASE_URL: str = Field(
        ...,
        description="PostgreSQL connection URL"
    )
    
    # OAuth - Google
    GOOGLE_CLIENT_ID: str = Field(
        ...,
        description="Google OAuth client ID"
    )
    GOOGLE_CLIENT_SECRET: str = Field(
        ...,
        description="Google OAuth client secret"
    )
    
    # OAuth - GitHub (optional)
    GITHUB_CLIENT_ID: Optional[str] = Field(
        None,
        description="GitHub OAuth client ID (optional)"
    )
    GITHUB_CLIENT_SECRET: Optional[str] = Field(
        None,
        description="GitHub OAuth client secret (optional)"
    )
    
    # Application
    APP_NAME: str = Field(
        default="D-PC Federation Hub",
        description="Application name"
    )
    APP_VERSION: str = Field(
        default_factory=lambda: __get_version(),
        description="Application version"
    )
    DEBUG: bool = Field(
        default=False,
        description="Debug mode (never enable in production!)"
    )
    
    # CORS
    ALLOWED_ORIGINS: str = Field(
        default="http://localhost:1420,tauri://localhost,http://localhost:3000",
        description="Comma-separated list of allowed CORS origins"
    )
    
    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = Field(
        default=True,
        description="Enable rate limiting"
    )
    RATE_LIMIT_PER_MINUTE: int = Field(
        default=60,
        description="Max requests per minute per IP"
    )
    
    # Token
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(
        default=30,
        ge=5,
        le=1440,  # Max 24 hours
        description="JWT token expiration time in minutes"
    )
    
    # WebSocket
    WEBSOCKET_MAX_CONNECTIONS: int = Field(
        default=10000,
        description="Maximum concurrent WebSocket connections"
    )
    
    @field_validator('SECRET_KEY')
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        """Validate SECRET_KEY meets security requirements"""
        if len(v) < 32:
            raise ValueError('SECRET_KEY must be at least 32 characters for security')
        
        # Warn if using a weak key
        weak_keys = ['your-secret-key', 'changeme', 'secret', 'password']
        if v.lower() in weak_keys:
            logger.error(f"SECURITY WARNING: SECRET_KEY is set to a common weak value!")
            raise ValueError(
                'SECRET_KEY is too weak. Generate a strong key with: '
                'openssl rand -hex 32'
            )
        
        return v
    
    @field_validator('DATABASE_URL')
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Validate DATABASE_URL is a PostgreSQL connection string"""
        if not v.startswith('postgresql'):
            raise ValueError(
                'DATABASE_URL must be a PostgreSQL connection string. '
                'Expected format: postgresql+asyncpg://user:pass@host:port/db'
            )
        
        # Check for asyncpg driver (required for async operations)
        if 'asyncpg' not in v:
            logger.warning(
                "DATABASE_URL doesn't specify asyncpg driver. "
                "Add +asyncpg after postgresql: postgresql+asyncpg://..."
            )
        
        return v
    
    @field_validator('ALLOWED_ORIGINS')
    @classmethod
    def validate_allowed_origins(cls, v: str) -> str:
        """Validate ALLOWED_ORIGINS format"""
        origins = [origin.strip() for origin in v.split(',')]
        
        for origin in origins:
            if not origin:
                continue
            
            # Check format
            if not (origin.startswith('http://') or 
                   origin.startswith('https://') or 
                   origin.startswith('tauri://')):
                raise ValueError(
                    f"Invalid origin format: {origin}. "
                    f"Must start with http://, https://, or tauri://"
                )
        
        return v
    
    @property
    def allowed_origins_list(self) -> list[str]:
        """Get ALLOWED_ORIGINS as a list"""
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(',') if origin.strip()]
    
    @property
    def is_production(self) -> bool:
        """Check if running in production mode"""
        return not self.DEBUG
    
    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        case_sensitive = True
        extra = "ignore"  # Ignore extra env vars


def load_settings() -> Settings:
    """
    Load and validate settings from environment.
    
    Exits the application if validation fails.
    
    Returns:
        Validated Settings object
    """
    try:
        settings = Settings()
        
        # Log successful load
        logger.info(f"Settings loaded successfully")
        logger.info(f"  - App: {settings.APP_NAME} v{settings.APP_VERSION}")
        logger.info(f"  - Debug mode: {settings.DEBUG}")
        logger.info(f"  - Rate limiting: {settings.RATE_LIMIT_ENABLED}")
        logger.info(f"  - CORS origins: {len(settings.allowed_origins_list)}")
        
        # Security warnings
        if settings.DEBUG:
            logger.warning("DEBUG mode is enabled - DO NOT use in production!")

        if 'localhost' in settings.DATABASE_URL and not settings.DEBUG:
            logger.warning("Using localhost database in production mode")
        
        return settings
        
    except ValidationError as e:
        logger.error("Configuration error:")
        for error in e.errors():
            field = ' -> '.join(str(loc) for loc in error['loc'])
            logger.error(f"   {field}: {error['msg']}")

        logger.error("\nTips:")
        logger.error("   1. Copy .env.example to .env")
        logger.error("   2. Fill in all required values")
        logger.error("   3. Generate SECRET_KEY: openssl rand -hex 32")

        sys.exit(1)

    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        sys.exit(1)


# Load settings on module import
settings = load_settings()