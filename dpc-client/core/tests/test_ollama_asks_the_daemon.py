"""What a model can do, and what temperature it actually runs at.

Both were decided in our code by values that could not say "nobody chose":
a name absent from a list meant "cannot", and the number 0.7 meant "unset".
"""

from __future__ import annotations

import pytest

from dpc_client_core.providers import ollama_provider as OP


class FakeShow:
    def __init__(self, capabilities, parameters=None):
        self.capabilities = capabilities
        if parameters is not None:
            self.parameters = parameters


class ShowWithoutTheField:
    """An older daemon: it answers, and its answer has no capabilities."""


class FakeClient:
    """Stands in for the daemon. `answer` may raise to mean 'not reachable'."""

    calls = 0
    timeout = None

    def __init__(self, host=None, timeout=None):
        self.host = host
        FakeClient.timeout = timeout

    def show(self, model):
        FakeClient.calls += 1
        if FakeClient.answer is None:
            raise RuntimeError("connection refused")
        if FakeClient.answer == "no-field":
            return ShowWithoutTheField()
        return FakeShow(FakeClient.answer, FakeClient.parameters)


@pytest.fixture(autouse=True)
def clean_capabilities(monkeypatch):
    OP._MODEL_INFO.clear()
    FakeClient.calls = 0
    FakeClient.answer = ["completion"]
    FakeClient.parameters = None
    monkeypatch.setattr(OP.ollama, "Client", FakeClient)
    yield
    OP._MODEL_INFO.clear()


def _provider(**config):
    config.setdefault("model", "muse-glimmer:latest")
    return OP.OllamaProvider("test_alias", config)


def test_a_model_in_no_list_is_still_seen_as_vision_capable():
    """muse-glimmer is in neither list, and the daemon says it takes images."""
    FakeClient.answer = ["completion", "vision", "tools", "thinking"]
    assert _provider().supports_vision() is True


def test_the_daemon_can_also_say_no_for_a_model_the_list_would_accept():
    """The list matches by substring, so it answers for names it has never
    seen. The daemon answers for the model actually installed."""
    FakeClient.answer = ["completion"]
    assert _provider(model="qwen3.5-something-textonly").supports_vision() is False


def test_an_unreachable_daemon_falls_back_to_the_list():
    FakeClient.answer = None
    assert _provider(model="qwen3-vl:8b").supports_vision() is True
    assert _provider(model="muse-glimmer:latest").supports_vision() is False


def test_a_daemon_that_answers_without_the_field_leaves_the_lists_in_charge():
    """An older daemon replies, and its reply has no capabilities at all.
    Reading that as "answered, named nothing" would strip vision from a model
    that has it — qwen3-vl would stop taking images and image QC would fail."""
    FakeClient.answer = "no-field"
    assert _provider(model="qwen3-vl:8b").supports_vision() is True
    assert _provider(model="muse-glimmer:latest").supports_vision() is False


def test_a_daemon_that_cannot_answer_is_asked_again_next_time():
    """A refusal is not an answer: the daemon may be starting up."""
    FakeClient.answer = None
    _provider().supports_vision()
    _provider().supports_vision()
    assert FakeClient.calls == 2


def test_the_question_carries_a_deadline():
    """This runs on the event loop, and `host` may be another machine. With
    no timeout a black-hole address hangs on the SYN for as long as the OS
    allows; measured with this one, the same call raises in 1.01 s."""
    _provider().supports_vision()
    assert FakeClient.timeout == OP._CAPABILITY_TIMEOUT_SECONDS


def test_the_daemon_is_asked_once_per_model():
    FakeClient.answer = ["completion", "vision"]
    p = _provider()
    p.supports_vision()
    p.supports_thinking()
    _provider().supports_vision()
    assert FakeClient.calls == 1


def test_a_configured_temperature_is_sent_even_when_it_equals_the_old_default():
    """0.7 used to mean "unset" and was dropped, so a provider asking for 0.7
    silently ran at whatever the model itself defaults to."""
    options = _provider(temperature=0.7)._build_options()
    assert options["temperature"] == 0.7


def test_no_configured_temperature_sends_none():
    assert (_provider()._build_options() or {}).get("temperature") is None


def test_an_explicit_argument_beats_the_configuration():
    options = _provider(temperature=0.7)._build_options(temperature=0.2)
    assert options["temperature"] == 0.2


def test_thinking_is_on_for_a_capable_model_when_nothing_says_otherwise():
    FakeClient.answer = ["completion", "thinking"]
    assert _provider()._think_flag() is True


def test_thinking_is_absent_for_a_model_that_cannot():
    FakeClient.answer = ["completion"]
    assert _provider()._think_flag() is None


def test_the_configuration_can_turn_thinking_off_on_a_capable_model():
    """The case that had no expression before: a model that can think, on a
    call where thinking eats the whole budget and leaves no answer."""
    FakeClient.answer = ["completion", "thinking"]
    assert _provider(think=False)._think_flag() is False


def test_the_configuration_can_turn_thinking_on_for_a_model_we_cannot_ask_about():
    FakeClient.answer = None
    assert _provider(think=True)._think_flag() is True


def test_the_log_says_which_flag_was_sent(caplog):
    """A model may reason after being told not to — qwen3-vl returned 6,725
    characters of thinking on a `think=False` call. Without this line there is
    nothing to tell that apart from the flag never having been sent."""
    FakeClient.answer = ["completion", "thinking"]
    with caplog.at_level("DEBUG", logger=OP.logger.name):
        _provider(context_window=16384, think=False)._build_options()
    assert "think=False" in caplog.text


# --- the per-call effort selector, wired 2026-08-15 ---
#
# Every number and refusal asserted below was measured against the live daemon
# (ollama 0.32.13) before the code was written; the review brief and both
# external reviews carry the payloads.


def test_a_level_reaches_the_daemon_unchanged():
    FakeClient.answer = ["completion", "thinking"]
    provider = _provider()
    assert provider._think_flag("low") == "low"
    assert provider._think_flag("medium") == "medium"
    assert provider._think_flag("high") == "high"


def test_max_is_sent_as_high_because_it_is_the_same_thing_here():
    """Measured by seed on qwen3.8: `high` and `max` produce byte-identical
    traces. And the SDK types the field `Literal['low','medium','high']`, so
    `max` would die in pydantic before a request left the process."""
    FakeClient.answer = ["completion", "thinking"]
    assert _provider()._think_flag("max") == "high"


def test_xhigh_becomes_high_and_not_max():
    """The vendor who publishes a table maps `xhigh` to *high*. Rewriting it to
    `max` sent a caller one notch above high to the dearest effort there is."""
    FakeClient.answer = ["completion", "thinking"]
    assert _provider()._think_flag("xhigh") == "high"


def test_an_effort_beats_the_configuration():
    """Nearer scope wins — the precedence `_think_flag`'s docstring promised."""
    FakeClient.answer = ["completion", "thinking"]
    assert _provider(think=False)._think_flag("high") == "high"


def test_a_level_is_dropped_for_a_model_that_cannot_think():
    """The daemon refuses it: 400 "<model> does not support thinking". A group
    knob must not be able to kill every call an agent makes."""
    FakeClient.answer = ["completion"]
    assert _provider()._think_flag("high") is None


def test_dropping_the_level_still_honours_an_explicit_configuration():
    FakeClient.answer = ["completion"]
    assert _provider(think=False)._think_flag("high") is False


def test_a_word_nobody_recognises_changes_nothing():
    """Unknown means unknown: fall back to what would have happened anyway,
    rather than guess at a level."""
    FakeClient.answer = ["completion", "thinking"]
    assert _provider()._think_flag("banana") is True
    assert _provider()._think_flag("") is True
    assert _provider()._think_flag(None) is True


def test_the_clamp_is_said_once_and_only_once(caplog):
    FakeClient.answer = ["completion", "thinking"]
    provider = _provider()
    with caplog.at_level("INFO"):
        provider._think_flag("max")
        provider._think_flag("max")
    assert sum("sent as 'high'" in r.getMessage() for r in caplog.records) == 1


def test_the_ignored_effort_is_said_once_and_only_once(caplog):
    FakeClient.answer = ["completion"]
    provider = _provider()
    with caplog.at_level("INFO"):
        provider._think_flag("high")
        provider._think_flag("low")
    assert sum("ignored" in r.getMessage() for r in caplog.records) == 1


def test_an_unknown_word_says_so_once(caplog):
    """The docstring of the shared normalizer promises the provider will say
    when it discards a caller's word. `none` and `minimal` are real DeepSeek
    levels that a group's stored effort can carry to an Ollama agent."""
    FakeClient.answer = ["completion", "thinking"]
    provider = _provider()
    with caplog.at_level("INFO"):
        assert provider._think_flag("none") is True
        assert provider._think_flag("minimal") is True
    assert sum("is not a level this provider knows" in r.getMessage()
               for r in caplog.records) == 1


def test_the_headers_own_config_value_is_not_an_unknown_word(caplog):
    """The selector's first option sends the empty string. Treating that as a
    discarded word would put a line in the log on every ordinary call."""
    FakeClient.answer = ["completion", "thinking"]
    provider = _provider()
    with caplog.at_level("INFO"):
        assert provider._think_flag("") is True
        assert provider._think_flag(None) is True
        assert provider._think_flag("   ") is True
    assert not any("is not a level this provider knows" in r.getMessage()
                   for r in caplog.records)


# --- a configured temperature displacing the model's own ---

MODELFILE = "temperature                    1\ntop_k                          20"


def test_the_log_names_both_numbers(caplog):
    """0.7 is the number the editor writes by itself; 1 is what the model asks
    for. Neither is legible without the other."""
    FakeClient.parameters = MODELFILE
    p = _provider(temperature=0.7)
    with caplog.at_level("INFO"):
        options = p._build_options()
    assert options["temperature"] == 0.7  # sent, not silently corrected
    line = [r.getMessage() for r in caplog.records if "Modelfile" in r.getMessage()]
    assert len(line) == 1
    assert "0.7" in line[0] and "asks for 1" in line[0]


def test_agreeing_with_the_model_is_not_worth_a_line(caplog):
    FakeClient.parameters = MODELFILE
    with caplog.at_level("INFO"):
        _provider(temperature=1)._build_options()
    assert not [r for r in caplog.records if "Modelfile" in r.getMessage()]


def test_a_model_that_names_no_temperature_is_not_guessed_at(caplog):
    """Silence from the daemon is not evidence of a default, so say nothing."""
    FakeClient.parameters = "top_k                          20"
    with caplog.at_level("INFO"):
        _provider(temperature=0.7)._build_options()
    assert not [r for r in caplog.records if "Modelfile" in r.getMessage()]


def test_an_unreachable_daemon_says_nothing_and_still_sends(caplog):
    FakeClient.answer = None  # connection refused
    with caplog.at_level("INFO"):
        options = _provider(temperature=0.7)._build_options()
    assert options["temperature"] == 0.7
    assert not [r for r in caplog.records if "Modelfile" in r.getMessage()]


def test_the_override_is_named_once(caplog):
    FakeClient.parameters = MODELFILE
    p = _provider(temperature=0.7)
    with caplog.at_level("INFO"):
        p._build_options()
        p._build_options()
    assert len([r for r in caplog.records if "Modelfile" in r.getMessage()]) == 1


def test_no_temperature_configured_is_still_silence(caplog):
    FakeClient.parameters = MODELFILE
    with caplog.at_level("INFO"):
        options = _provider()._build_options()
    assert "temperature" not in (options or {})
    assert not [r for r in caplog.records if "Modelfile" in r.getMessage()]


def test_one_show_call_answers_both_questions():
    """Capabilities and defaults come from the same response; asking twice
    would double a question the daemon has already answered."""
    FakeClient.parameters = MODELFILE
    p = _provider(temperature=0.7)
    p.supports_thinking()
    p._build_options()
    assert FakeClient.calls == 1


# --- Off: the foot of the scale, and the direction the header could not reach ---


def test_off_turns_thinking_off_on_a_model_that_can_think():
    FakeClient.answer = ["completion", "thinking"]
    assert _provider()._think_flag("off") is False


def test_off_beats_a_configuration_that_says_yes():
    """Nearer scope wins in both directions now, not only upward."""
    FakeClient.answer = ["completion", "thinking"]
    assert _provider(think=True)._think_flag("off") is False


def test_off_is_safe_on_a_model_that_cannot_think():
    """`think=False` is the one value every model accepts — measured against a
    daemon that answers a *level* on such a model with a 400. So `off` is not
    gated on the capability the way the levels are."""
    FakeClient.answer = ["completion"]
    assert _provider()._think_flag("off") is False


def test_off_says_nothing_in_the_log(caplog):
    """It is an understood word, not a discarded one."""
    FakeClient.answer = ["completion"]
    with caplog.at_level("INFO"):
        _provider()._think_flag("off")
    assert not [r for r in caplog.records if "ignored" in r.getMessage()]


def test_a_level_still_reaches_an_alias_that_configured_no():
    """The other half of the same precedence: config is a default, and the
    header is nearer. Off is what makes that symmetric rather than one-way."""
    FakeClient.answer = ["completion", "thinking"]
    assert _provider(think=False)._think_flag("high") == "high"
