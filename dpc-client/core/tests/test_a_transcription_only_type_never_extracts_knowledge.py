"""The extraction chain's local types are the firewall's, minus transcription-only.

`provider_facts` reads the same two classes for `provider_kind`, by import.

One list of local provider types lives in `firewall.py`. `knowledge_routing`
takes it and removes the types that transcribe and do not chat: a Whisper
alias first in the registry must not be picked as the cold fallback.
"""

from types import SimpleNamespace

from dpc_client_core import firewall, gateway, knowledge_routing as KR
from dpc_client_core.dpc_agent import provider_facts


def test_the_set_is_derived_not_a_third_literal():
    assert KR.LOCAL_PROVIDER_TYPES == (
        firewall.LOCAL_PROVIDER_TYPES - firewall.TRANSCRIPTION_ONLY_PROVIDER_TYPES)
    assert gateway.TRANSCRIPTION_ONLY_PROVIDER_TYPES is firewall.TRANSCRIPTION_ONLY_PROVIDER_TYPES


def test_provider_kind_reads_the_firewalls_classes_not_copies():
    assert provider_facts.LOCAL_TYPES is firewall.LOCAL_PROVIDER_TYPES
    assert provider_facts.VENDOR_TYPES is firewall.VENDOR_PROVIDER_TYPES


def test_a_whisper_alias_first_in_the_registry_is_skipped():
    providers = {
        "whisper": SimpleNamespace(config={"type": "local_whisper"}),
        "local_qwen": SimpleNamespace(config={"type": "ollama"}),
    }

    assert KR.first_local_provider(providers) == "local_qwen"
