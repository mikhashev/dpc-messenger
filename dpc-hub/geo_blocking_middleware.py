"""
Geographic Blocking Middleware for D-PC Hub

IMPORTANT: This middleware provides IP-based geographic blocking to comply with
legal restrictions. Install geoip2 library: pip install geoip2

Download GeoLite2-Country database from:
https://dev.maxmind.com/geoip/geolite2-free-geolocation-data
(Requires free MaxMind account)

Usage:
    1. Configure environment variables in .env (see .env.example)
    2. Import this module in dpc_hub/main.py
    3. Add middleware: app.add_middleware(GeoBlockingMiddleware)
"""

import os
import logging
from typing import List, Optional
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Try to import geoip2 (optional dependency)
try:
    import geoip2.database
    import geoip2.errors
    GEOIP_AVAILABLE = True
except ImportError:
    GEOIP_AVAILABLE = False
    logger.warning(
        "geoip2 library not installed. Geographic blocking disabled. "
        "Install with: pip install geoip2"
    )


class GeoBlockingMiddleware(BaseHTTPMiddleware):
    """
    Middleware to block requests from specific countries based on IP geolocation.

    This provides legal compliance by blocking access from jurisdictions where
    the software may be subject to legal restrictions.
    """

    def __init__(self, app):
        super().__init__(app)

        # Load configuration from environment
        self.enabled = os.getenv("ENABLE_GEO_BLOCKING", "false").lower() == "true"
        self.db_path = os.getenv("GEOIP_DATABASE_PATH", "")
        self.log_blocks = os.getenv("LOG_GEO_BLOCKS", "true").lower() == "true"

        # Parse blocked countries (comma-separated ISO codes)
        blocked_str = os.getenv("BLOCKED_COUNTRIES", "RU,BY")
        self.blocked_countries: List[str] = [
            code.strip().upper()
            for code in blocked_str.split(",")
            if code.strip()
        ]

        # Initialize GeoIP reader
        self.reader: Optional[geoip2.database.Reader] = None

        if self.enabled and GEOIP_AVAILABLE:
            if not self.db_path or not os.path.exists(self.db_path):
                logger.error(
                    f"GeoIP database not found at: {self.db_path}. "
                    "Geographic blocking disabled. "
                    "Download from: https://dev.maxmind.com/geoip/geolite2-free-geolocation-data"
                )
                self.enabled = False
            else:
                try:
                    self.reader = geoip2.database.Reader(self.db_path)
                    logger.info(
                        f"Geographic blocking enabled. Blocked countries: {', '.join(self.blocked_countries)}"
                    )
                except Exception as e:
                    logger.error(f"Failed to load GeoIP database: {e}")
                    self.enabled = False

        if not self.enabled:
            logger.warning(
                "Geographic blocking is DISABLED. This may create legal compliance risks. "
                "See docs/GEOGRAPHIC_RESTRICTIONS.md for details."
            )

    async def dispatch(self, request: Request, call_next):
        """Process request and block if from prohibited country."""

        # Skip if geo-blocking disabled
        if not self.enabled or not self.reader:
            return await call_next(request)

        # Get client IP address
        client_ip = self._get_client_ip(request)

        if not client_ip:
            # If we can't determine IP, allow request but log
            logger.warning("Unable to determine client IP for geo-blocking")
            return await call_next(request)

        # Check if IP is from blocked country
        try:
            response = self.reader.country(client_ip)
            country_code = response.country.iso_code

            if country_code in self.blocked_countries:
                # Block the request
                country_name = response.country.name or country_code

                if self.log_blocks:
                    logger.warning(
                        f"BLOCKED: Request from {country_name} ({country_code}) - "
                        f"IP: {client_ip} - Path: {request.url.path}"
                    )

                # Return 451 Unavailable For Legal Reasons
                # https://tools.ietf.org/html/rfc7725
                return JSONResponse(
                    status_code=status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
                    content={
                        "detail": "Service unavailable in your jurisdiction",
                        "error_code": "GEO_BLOCKED",
                        "message": (
                            "This service is not available in your geographic location "
                            "due to legal restrictions. See NOTICE and "
                            "docs/GEOGRAPHIC_RESTRICTIONS.md for details."
                        ),
                        "country": country_code,
                        "documentation": "https://github.com/[repo]/blob/main/docs/GEOGRAPHIC_RESTRICTIONS.md"
                    },
                    headers={
                        "X-Geo-Block": country_code,
                        "X-Legal-Notice": "Service unavailable due to legal restrictions"
                    }
                )

            # Country is allowed, proceed with request
            return await call_next(request)

        except geoip2.errors.AddressNotFoundError:
            # IP not in database (private IP, etc.) - allow but log
            logger.debug(f"GeoIP lookup failed for {client_ip}: Address not found")
            return await call_next(request)

        except Exception as e:
            # GeoIP lookup failed for other reason - allow but log error
            logger.error(f"GeoIP lookup error for {client_ip}: {e}")
            return await call_next(request)

    def _get_client_ip(self, request: Request) -> Optional[str]:
        """
        Extract client IP address from request.

        Handles common proxy headers (X-Forwarded-For, X-Real-IP) for
        deployments behind reverse proxies (Nginx, Cloudflare, etc.).
        """
        # Check X-Forwarded-For header (standard for proxies)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # X-Forwarded-For can contain multiple IPs (client, proxy1, proxy2, ...)
            # The leftmost is the original client IP
            client_ip = forwarded_for.split(",")[0].strip()
            return client_ip

        # Check X-Real-IP header (used by some proxies)
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()

        # Fall back to direct client address
        if request.client:
            return request.client.host

        return None

    def __del__(self):
        """Close GeoIP database reader on cleanup."""
        if self.reader:
            try:
                self.reader.close()
            except:
                pass


# Example integration in dpc_hub/main.py:
"""
from dpc_hub.geo_blocking_middleware import GeoBlockingMiddleware

app = FastAPI(...)

# Add geo-blocking middleware
app.add_middleware(GeoBlockingMiddleware)

# Rest of your FastAPI application...
"""


# Standalone test function
async def test_geo_blocking():
    """Test geo-blocking functionality."""
    import sys

    if not GEOIP_AVAILABLE:
        print("ERROR: geoip2 library not installed")
        print("Install with: pip install geoip2")
        sys.exit(1)

    db_path = os.getenv("GEOIP_DATABASE_PATH", "")
    if not db_path or not os.path.exists(db_path):
        print(f"ERROR: GeoIP database not found at: {db_path}")
        print("Download from: https://dev.maxmind.com/geoip/geolite2-free-geolocation-data")
        sys.exit(1)

    # Test IPs (public DNS servers)
    test_ips = {
        "8.8.8.8": "US (Google DNS - should ALLOW)",
        "1.1.1.1": "AU/US (Cloudflare - should ALLOW)",
        "77.88.8.8": "RU (Yandex DNS - should BLOCK)",
        "178.124.183.1": "BY (Belarus - should BLOCK)",
    }

    print("\n" + "="*60)
    print("GEO-BLOCKING TEST")
    print("="*60)
    print(f"Database: {db_path}")
    print(f"Blocked countries: {os.getenv('BLOCKED_COUNTRIES', 'RU,BY')}")
    print("="*60 + "\n")

    try:
        reader = geoip2.database.Reader(db_path)

        for ip, description in test_ips.items():
            try:
                response = reader.country(ip)
                country = f"{response.country.name} ({response.country.iso_code})"
                blocked = response.country.iso_code in ["RU", "BY"]
                status_str = "BLOCKED" if blocked else "ALLOWED"

                print(f"{ip:20} {country:30} {status_str}")
                print(f"  → {description}")
                print()

            except Exception as e:
                print(f"{ip:20} ERROR: {e}")
                print()

        reader.close()
        print("="*60)
        print("Test complete!")
        print("="*60)

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_geo_blocking())
