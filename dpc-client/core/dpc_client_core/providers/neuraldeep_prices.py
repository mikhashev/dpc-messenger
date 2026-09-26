# dpc_client_core/providers/neuraldeep_prices.py
"""NeuralDeep's price list, read live from the vendor's public endpoint.

Fetched on the first priced call after start and then at most once per
`REFRESH_INTERVAL`; each fetch is cached at `cache_path()` so a restart inside
the day does not fetch again. A failed fetch falls back to the cache with a
WARNING naming its age; with no cache the cost is unknown. Never raises into
the model call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

PRICE_LIST_URL = "https://neuraldeep.ru/api/public/wallet-prices"
REFRESH_INTERVAL = timedelta(hours=24)
# So an unreachable vendor does not add a fetch timeout to every call.
RETRY_AFTER_FAILURE = timedelta(hours=1)
FETCH_TIMEOUT_SECONDS = 5.0
CACHE_FILE_NAME = "neuraldeep_wallet_prices.json"


def cache_path() -> Path:
    """Where the last fetched list is kept; `DPC_HOME` is honoured as elsewhere."""
    home = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))
    return home / "cache" / CACHE_FILE_NAME


class PriceListShapeError(ValueError):
    """The endpoint answered, but not with a list of priced models."""


def rows_from_payload(payload: Any) -> Dict[str, Dict[str, Any]]:
    """`{"prices": [{model, ...}, ...]}` -> rows keyed by lowercased model id."""
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise PriceListShapeError("no 'prices' list")
    rows: Dict[str, Dict[str, Any]] = {}
    for item in payload["prices"]:
        if isinstance(item, dict) and isinstance(item.get("model"), str) and item["model"].strip():
            rows[item["model"].strip().lower()] = item
    if not rows:
        raise PriceListShapeError("'prices' holds no model rows")
    return rows


@dataclass(frozen=True)
class PriceList:
    rows: Dict[str, Dict[str, Any]]
    fetched_at: datetime  # UTC, when the vendor answered with this list

    @property
    def fetched_at_iso(self) -> str:
        return self.fetched_at.astimezone(timezone.utc).isoformat()

    def age(self, now: datetime) -> timedelta:
        return now - self.fetched_at


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _fetch_payload() -> Any:
    """GET the public list; raises on transport errors and non-200."""
    import httpx
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
        resp = await client.get(PRICE_LIST_URL)
        resp.raise_for_status()
        return resp.json()


def _write_cache(path: Path, fetched_at: datetime, payload: Any) -> None:
    """Temp file + os.replace, so a reader never sees half a list."""
    from ..dpc_agent.index_meta import atomic_write_text
    atomic_write_text(path, json.dumps(
        {"fetched_at": fetched_at.astimezone(timezone.utc).isoformat(), "payload": payload},
        ensure_ascii=False,
    ))


def _read_cache(path: Path) -> Optional[PriceList]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(doc["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return PriceList(rows=rows_from_payload(doc["payload"]), fetched_at=fetched_at)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("NeuralDeep price cache %s is unreadable (%s); ignoring it", path, exc)
        return None


class NeuralDeepPriceSource:
    """The live list with its daily cadence. One per process: the list is public
    and the same for every key, so every NeuralDeep alias shares it."""

    def __init__(self, *, fetch: Optional[Callable[[], Any]] = None,
                 clock: Callable[[], datetime] = _utcnow):
        self._fetch = fetch
        self._clock = clock
        self._lock = asyncio.Lock()
        self._list: Optional[PriceList] = None
        self._loaded_disk = False
        self._last_failure_at: Optional[datetime] = None

    async def current(self) -> Optional[PriceList]:
        """The list to price with now, fetching first if it is due. Never raises."""
        try:
            async with self._lock:
                return await self._current_locked()
        except Exception:
            logger.error("NeuralDeep price list lookup failed", exc_info=True)
            return self._list

    async def _current_locked(self) -> Optional[PriceList]:
        now = self._clock()
        if not self._loaded_disk:
            self._loaded_disk = True
            self._list = await asyncio.to_thread(_read_cache, cache_path())
        if self._list is not None and self._list.age(now) < REFRESH_INTERVAL:
            return self._list
        if self._last_failure_at is not None and now - self._last_failure_at < RETRY_AFTER_FAILURE:
            return self._list
        try:
            payload = await (self._fetch or _fetch_payload)()
            rows = rows_from_payload(payload)
        except Exception as exc:
            self._last_failure_at = now
            if self._list is not None:
                logger.warning(
                    "NeuralDeep price list fetch failed (%s: %s); pricing from the cached "
                    "list fetched %s, %.1f h old",
                    type(exc).__name__, exc, self._list.fetched_at_iso,
                    self._list.age(now).total_seconds() / 3600,
                )
            else:
                logger.warning(
                    "NeuralDeep price list fetch failed (%s: %s) and there is no cached "
                    "list; NeuralDeep costs are unknown until a fetch succeeds",
                    type(exc).__name__, exc,
                )
            return self._list
        self._last_failure_at = None
        self._list = PriceList(rows=rows, fetched_at=now)
        try:
            await asyncio.to_thread(_write_cache, cache_path(), now, payload)
        except Exception:
            logger.warning("NeuralDeep price list fetched but not cached", exc_info=True)
        return self._list


_SOURCE: Optional[NeuralDeepPriceSource] = None


def price_source() -> NeuralDeepPriceSource:
    """The process-wide source, created on first use."""
    global _SOURCE
    if _SOURCE is None:
        _SOURCE = NeuralDeepPriceSource()
    return _SOURCE


def reset_price_source(source: Optional[NeuralDeepPriceSource] = None) -> None:
    """Replace the process-wide source — for tests."""
    global _SOURCE
    _SOURCE = source
