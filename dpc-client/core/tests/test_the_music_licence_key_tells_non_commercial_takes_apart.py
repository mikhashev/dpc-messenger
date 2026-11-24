"""Mike, 2026-11-24 (group #32; ADR-051 D17): a take's provenance carries a licence key, so takes made with
non-commercial weights can be told apart later.

The key is written from sources, never guessed: acestep.cpp and yue2.cpp code and acestep.cpp's GGUF weights
are MIT (the BUILD.md of each engine), the stock YuE2 backbone is CC BY-NC 4.0, the RS-TW backbone CC BY-NC-SA
4.0, the instrumental adapter `ar_lora_inst_v3abc` CC BY-NC 4.0. What has no source here (the YuE2 VAE, the
Ruach voice adapters, an engine this code has not heard of) is `unknown`.
"""

from dpc_client_core.music import provenance as prov
from dpc_client_core.music.yue2 import DEFAULT_INSTRUMENTAL_ADAPTER

INSTRUMENTAL = {"name": "YuE2-instrumental-cot-full-loras",
                "source": {"file": "ar_lora_inst_v3abc.bf16.safetensors", "bytes": 1, "sha256": "sha256:" + "0" * 64}}
VOICE = {"name": "ruach-voice-1", "source": {"file": "voice.safetensors"}}


def _by_role(record):
    return {c["role"]: c["licence"] for c in record["components"]}


def test_the_default_instrumental_adapter_name_here_is_the_one_the_engine_uses():
    assert prov.INSTRUMENTAL_ADAPTER_NAME == DEFAULT_INSTRUMENTAL_ADAPTER == "YuE2-instrumental-cot-full-loras"


def test_an_acestep_take_is_mit_and_not_non_commercial():
    record = prov.licence_record("acestep.cpp")
    assert record["key"] == "mit" and record["non_commercial"] is False
    assert _by_role(record) == {"engine": "mit", "weights": "mit"}


def test_a_yue2_take_is_non_commercial_by_its_backbone_whatever_the_adapters_are():
    plain = prov.licence_record("yue2.cpp", backbone="stock")
    assert plain["non_commercial"] is True
    assert _by_role(plain) == {"engine": "mit", "backbone": "cc-by-nc-4.0", "vae": "unknown"}
    assert plain["key"] == "cc-by-nc-4.0+mit+unknown"
    rs = prov.licence_record("yue2.cpp", backbone="rs-tw")
    assert _by_role(rs)["backbone"] == "cc-by-nc-sa-4.0" and rs["non_commercial"] is True
    assert rs["key"] == "cc-by-nc-sa-4.0+mit+unknown"


def test_the_instrumental_adapter_is_cc_by_nc_by_its_name_or_by_its_source_file():
    record = prov.licence_record("yue2.cpp", backbone="stock", adapters=[INSTRUMENTAL])
    adapter = next(c for c in record["components"] if c["role"] == "adapter")
    assert adapter == {"role": "adapter", "name": INSTRUMENTAL["name"], "licence": "cc-by-nc-4.0"}
    renamed = {"name": "my-copy", "source": {"file": "ar_lora_inst_v3abc.bf16.safetensors"}}
    assert next(c for c in prov.licence_record("yue2.cpp", backbone="stock", adapters=[renamed])["components"]
                if c["role"] == "adapter")["licence"] == "cc-by-nc-4.0"


def test_a_voice_adapter_is_unknown_never_guessed():
    record = prov.licence_record("yue2.cpp", backbone="stock", adapters=[VOICE])
    assert next(c for c in record["components"] if c["role"] == "adapter")["licence"] == "unknown"


def test_an_unknown_backbone_or_engine_is_unknown_and_not_called_commercial():
    assert _by_role(prov.licence_record("yue2.cpp", backbone="mystery"))["backbone"] == "unknown"
    other = prov.licence_record("some-new.cpp")
    assert other["key"] == "unknown" and other["non_commercial"] is None, "unknown parts leave the answer open"
    assert _by_role(other) == {"engine": "unknown", "weights": "unknown"}


def test_the_licence_changes_no_id_and_the_library_summary_carries_the_key():
    rec = {"schema": prov.SCHEMA, "engine": "yue2.cpp", "seed": 1, "semantic_tokens": "1,2",
           "semantic_tokens_sha256": prov.text_sha256("1,2"),
           "licence": prov.licence_record("yue2.cpp", backbone="stock")}
    without = {k: v for k, v in rec.items() if k != "licence"}
    assert prov.derive_ids(rec) == prov.derive_ids(without)
    summary = prov.summarize(rec)
    assert summary["licence_key"] == "cc-by-nc-4.0+mit+unknown" and summary["non_commercial"] is True
    assert "licence_key" not in prov.summarize(without), "an older record has no key and the summary invents none"
    # A hostile record cannot put free text into the entry.
    rec["licence"] = {"key": "C:\\Users\\x\\secret", "non_commercial": "yes"}
    assert "licence_key" not in prov.summarize(rec)
    rec["licence"] = {"key": "mit", "non_commercial": "yes"}
    assert prov.summarize(rec)["non_commercial"] is None
