"""AIProvider._is_retryable — the shared classifier (B, retry-classifier fix).

Was four near-identical copies, each substring-matching the stringified
exception: "you requested 42900 tokens" (a deterministic 400) contains "429"
and was retried for the full backoff budget. Fixed by preferring a real
status code, then a known exception type, and only falling back to text
matching with a context-word guard around the bare number.

No network."""

from types import SimpleNamespace

from dpc_client_core.providers.base import AIProvider
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.zai_provider import ZaiProvider


def _err_with_status(status_code, text="error"):
    err = Exception(text)
    err.status_code = status_code
    return err


def _err_with_response_status(status_code, text="error"):
    err = Exception(text)
    err.response = SimpleNamespace(status_code=status_code)
    return err


def test_a_deterministic_400_mentioning_42900_is_not_retryable():
    err = _err_with_status(400, "you requested 42900 tokens, which exceeds the limit")
    assert AIProvider._is_retryable(err) is False


def test_a_400_with_no_status_code_and_the_string_42900_is_not_retryable():
    # No status_code attribute at all: falls through to text matching, where
    # "42900" must not be read as the word "429".
    err = Exception("you requested 42900 tokens, which exceeds the limit")
    assert AIProvider._is_retryable(err) is False


def test_status_code_429_is_retryable():
    assert AIProvider._is_retryable(_err_with_status(429)) is True


def test_status_code_on_response_attribute_is_retryable():
    assert AIProvider._is_retryable(_err_with_response_status(503)) is True


def test_status_code_404_is_not_retryable():
    assert AIProvider._is_retryable(_err_with_status(404, "not found")) is False


def test_api_connection_error_type_is_retryable_with_no_status():
    class APIConnectionError(Exception):
        pass

    assert AIProvider._is_retryable(APIConnectionError("could not connect")) is True


def test_bare_number_with_context_word_is_retryable_as_text_fallback():
    assert AIProvider._is_retryable(Exception("request failed with http status 429")) is True


def test_deepseek_still_retries_its_own_extra_phrases():
    assert DeepSeekProvider._is_retryable(Exception("internal network failure, retry")) is True
    assert DeepSeekProvider._is_retryable(Exception("high traffic on the endpoint")) is True
    # And the shared status-code path still applies through the subclass.
    assert DeepSeekProvider._is_retryable(_err_with_status(400, "42900 tokens requested")) is False


def test_zai_1313_stays_non_retryable_even_with_a_429_status_code():
    err = _err_with_status(429, "code 1313 fair usage")
    assert ZaiProvider._is_retryable(err) is False


def test_deepseek_deterministic_400_quoting_a_phrase_is_not_retried():
    # A status code is decisive; a phrase in the body must not override it.
    err = _err_with_status(400, "high traffic on our end, please slow down")
    assert DeepSeekProvider._is_retryable(err) is False


def test_deepseek_phrase_without_a_status_code_is_still_retried():
    err = Exception("high traffic on our end, please slow down")
    assert DeepSeekProvider._is_retryable(err) is True


def test_zai_deterministic_400_quoting_a_phrase_is_not_retried():
    err = _err_with_status(400, "high traffic on our end, please slow down")
    assert ZaiProvider._is_retryable(err) is False


def test_zai_phrase_without_a_status_code_is_still_retried():
    err = Exception("high traffic on our end, please slow down")
    assert ZaiProvider._is_retryable(err) is True
