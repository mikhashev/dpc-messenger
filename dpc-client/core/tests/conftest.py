"""Shared test constants. Override TEST_DOMAIN via DPC_TEST_DOMAIN env var."""
import os

TEST_DOMAIN = os.environ.get("DPC_TEST_DOMAIN", "wikipedia.org")
TEST_DOMAIN_WWW = f"www.{TEST_DOMAIN}"
TEST_DOMAIN_URL = f"https://{TEST_DOMAIN}"
