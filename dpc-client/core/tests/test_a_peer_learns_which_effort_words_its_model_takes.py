"""A model's own effort words stopped at the edge of this node.

`_provider_rows` reads the words a llama.cpp model's chat template accepts —
for the model in use `xhigh`/`medium`/`low` — and hands them to the local UI.
`build_p2p_provider_info`, the single source of the rows that travel in
PROVIDERS_RESPONSE, emitted six keys and none of them said anything about
effort. So an agent pinned to a peer's model was offered the generic fleet
scale, and the model's own `xhigh` was never on the list.

The guard is the point of the change, not the payload: the provider records
whether it read the words from the model or fell back to a constant table, and
only the first may travel wearing the model's name. A GGUF is not needed to
observe any of this, and must not be — the model lives on one machine in this
fleet and these tests run on three.

Fixtures come from tests/test_provider_metadata.py, which owns the other
assertions about this builder; the two files must not grow separate notions of
what a provider looks like.
"""

from tests.test_provider_metadata import FakeProvider, _llm_manager_with

from types import SimpleNamespace

from dpc_client_core.service import CoreService


class TemplateAwareProvider(FakeProvider):
    """A provider that has already asked its own template for the words.

    `source` is what `llamacpp_server_provider` writes: "model" when the words
    were read out of the GGUF, "fallback" when the constant table answered.
    """

    def __init__(self, efforts, default, source, **kwargs):
        super().__init__("gpt-oss-120b", ptype="llamacpp_server", **kwargs)
        self._template_efforts = efforts
        self._template_default = default
        self._template_efforts_source = source


def _info_for(provider):
    stub = SimpleNamespace(
        llm_manager=_llm_manager_with({"local_llama": provider}),
        _provider_supports_voice=lambda p: False,
    )
    return CoreService.build_p2p_provider_info(stub, "local_llama", provider)


def test_the_words_read_from_the_model_travel_to_the_peer():
    info = _info_for(TemplateAwareProvider(
        ["xhigh", "medium", "low"], "xhigh", "model",
    ))

    assert info["reasoning_words"] == ["xhigh", "medium", "low"]
    assert info["reasoning_default"] == "xhigh"


def test_a_default_the_template_never_named_travels_as_nothing():
    """A template may guard the words without naming a default. `None` is the
    honest answer; an invented one would be another model's habit."""
    info = _info_for(TemplateAwareProvider(["high", "low"], None, "model"))

    assert info["reasoning_words"] == ["high", "low"]
    assert info["reasoning_default"] is None


def test_the_list_that_travels_is_the_peers_to_keep():
    """The row crosses a serialisation boundary on this node's behalf; handing
    out the provider's own list would let a peer-facing edit reach it."""
    provider = TemplateAwareProvider(["xhigh", "low"], "xhigh", "model")

    info = _info_for(provider)
    info["reasoning_words"].append("invented")

    assert provider._template_efforts == ["xhigh", "low"]


def test_the_fallback_table_does_not_travel_as_the_models_own_words():
    info = _info_for(TemplateAwareProvider(
        ["max", "high", "medium", "low"], None, "fallback",
    ))

    assert "reasoning_words" not in info
    assert "reasoning_default" not in info


def test_a_provider_that_knows_nothing_of_templates_says_nothing():
    """Every other provider type — ollama, zai, anthropic — reaches this
    builder without the attribute at all, and must not raise on the way."""
    info = _info_for(FakeProvider("glm-5.1", ptype="zai", context_window=204800))

    assert "reasoning_words" not in info
    assert "reasoning_default" not in info
    assert info["model"] == "glm-5.1"
