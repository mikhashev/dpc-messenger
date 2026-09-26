"""Tests for the live NeuralDeep price list: cadence, cache, fallbacks. No network:
the fetch is a stub and the clock is ours."""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from dpc_client_core.providers import neuraldeep_prices
from dpc_client_core.providers.neuraldeep_prices import (
    CACHE_FILE_NAME,
    NeuralDeepPriceSource,
    PRICE_LIST_URL,
    cache_path,
)

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
PAYLOAD = {"prices": [
    {"model": "gemma-4-31b", "billing": "token", "in_rub_1m": 11.0, "out_rub_1m": 37.4,
     "cached_in_rub_1m": 8.8, "unit_rub": None, "rub_per_min": None,
     "rub_per_1k_chars": None, "premium": False, "or_model": False},
    {"model": "whisper-1", "billing": "minute", "in_rub_1m": 0.0, "out_rub_1m": 0.0,
     "cached_in_rub_1m": None, "unit_rub": None, "rub_per_min": 102.0,
     "rub_per_1k_chars": None, "premium": False, "or_model": False},
]}


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class _Fetch:
    def __init__(self, result=PAYLOAD):
        self.result = result
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    return tmp_path


def _source(fetch, clock):
    return NeuralDeepPriceSource(fetch=fetch, clock=clock)


def test_cache_lives_under_dpc_home(_home):
    assert cache_path() == _home / "cache" / CACHE_FILE_NAME


@pytest.mark.asyncio
async def test_a_fresh_fetch_writes_the_cache_with_its_date():
    fetch, clock = _Fetch(), _Clock(T0)
    price_list = await _source(fetch, clock).current()
    assert fetch.calls == 1
    assert price_list.rows["gemma-4-31b"]["cached_in_rub_1m"] == 8.8
    assert price_list.fetched_at_iso == T0.isoformat()
    doc = json.loads(cache_path().read_text(encoding="utf-8"))
    assert doc == {"fetched_at": T0.isoformat(), "payload": PAYLOAD}
    assert not cache_path().with_name(CACHE_FILE_NAME + ".tmp").exists()


@pytest.mark.asyncio
async def test_within_a_day_there_is_no_refetch_even_across_a_restart():
    fetch, clock = _Fetch(), _Clock(T0)
    await _source(fetch, clock).current()
    clock.now = T0 + timedelta(hours=23)
    source = _source(fetch, clock)  # a restart: memory empty, cache on disk
    assert (await source.current()).fetched_at == T0
    assert (await source.current()).fetched_at == T0
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_after_a_day_the_list_is_fetched_again():
    fetch, clock = _Fetch(), _Clock(T0)
    source = _source(fetch, clock)
    await source.current()
    clock.now = T0 + timedelta(hours=24, minutes=1)
    assert (await source.current()).fetched_at == clock.now
    assert fetch.calls == 2
    assert json.loads(cache_path().read_text(encoding="utf-8"))["fetched_at"] == \
        clock.now.isoformat()


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("offline"),
    httpx.HTTPStatusError("503", request=httpx.Request("GET", PRICE_LIST_URL),
                          response=httpx.Response(503)),
    {"models": []},              # answered, wrong shape
    {"prices": [{"price": 1}]},  # a list with no model rows
])
@pytest.mark.asyncio
async def test_a_failed_fetch_prices_from_the_cache_and_says_how_old_it_is(failure, caplog):
    clock = _Clock(T0)
    await _source(_Fetch(), clock).current()
    clock.now = T0 + timedelta(hours=30)
    fetch = _Fetch(failure)
    with caplog.at_level(logging.WARNING, logger=neuraldeep_prices.__name__):
        price_list = await _source(fetch, clock).current()
    assert fetch.calls == 1
    assert price_list.fetched_at == T0
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "30.0 h old" in warning and T0.isoformat() in warning


@pytest.mark.asyncio
async def test_a_failed_fetch_without_a_cache_is_none_and_retried_later(caplog):
    fetch, clock = _Fetch(httpx.ConnectError("offline")), _Clock(T0)
    source = _source(fetch, clock)
    with caplog.at_level(logging.WARNING, logger=neuraldeep_prices.__name__):
        assert await source.current() is None
    assert "no cached list" in caplog.text
    assert await source.current() is None
    assert fetch.calls == 1  # not once per call while the vendor is down
    clock.now = T0 + timedelta(hours=1, minutes=1)
    fetch.result = PAYLOAD
    assert (await source.current()).fetched_at == clock.now
    assert fetch.calls == 2


@pytest.mark.asyncio
async def test_an_unreadable_cache_counts_as_none(caplog):
    cache_path().parent.mkdir(parents=True)
    cache_path().write_text("{not json", encoding="utf-8")
    fetch = _Fetch(httpx.ConnectError("offline"))
    assert await _source(fetch, _Clock(T0)).current() is None


@pytest.mark.asyncio
async def test_the_default_fetch_reads_the_public_url_without_a_key(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=PAYLOAD)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    assert await neuraldeep_prices._fetch_payload() == PAYLOAD
    assert seen == {"url": PRICE_LIST_URL, "auth": None}
