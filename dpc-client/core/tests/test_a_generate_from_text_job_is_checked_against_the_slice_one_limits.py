"""A `generate_from_text` job is checked against the slice-1 limits (SPEC §12 decisions
33-34): variants 1-4 (default 2), duration 10-600 s (default 120), a seed in the engine's
range or null, no source track. A bad param is refused with its name, and the lyrics are
kept verbatim.
"""

import pytest

from dpc_client_core.music.jobs import MusicJobError
from tests._music_jobs_helpers import from_text_params, store_at


def _refused(store, **params):
    with pytest.raises(MusicJobError) as err:
        store.create_job("generate_from_text", params)
    return err.value


def test_defaults_fill_variants_duration_seed_prep_and_engine(tmp_path):
    store = store_at(tmp_path)
    job = store.create_job("generate_from_text", {"lyrics": "la la", "style": "dnb"})
    assert job["params"] == {"lyrics": "la la", "style": "dnb", "variants": 2, "duration_s": 120, "seed": None,
                             "lyrics_prep": {}, "engine": "ace-step-1.5:text2music"}
    assert job["track_id"] is None and job["base_version_id"] is None
    assert job["comment_id"] is None and job["action"] is None and job["region"] is None
    assert job["prompt"] == "dnb"


@pytest.mark.parametrize("variants", [1, 4])
def test_variants_at_the_edges_are_accepted(tmp_path, variants):
    assert store_at(tmp_path).create_job("generate_from_text", from_text_params(variants=variants))["params"]["variants"] == variants


@pytest.mark.parametrize("variants", [0, 5, -1, 2.0, "2", True, None])
def test_variants_outside_one_to_four_are_refused(tmp_path, variants):
    err = _refused(store_at(tmp_path), **from_text_params(variants=variants))
    assert err.code == "invalid_params" and err.field == "variants"
    assert "1 to 4" in err.message


@pytest.mark.parametrize("duration", [10, 10.0, 120, 599.5, 600])
def test_durations_from_ten_to_six_hundred_seconds_are_accepted(tmp_path, duration):
    assert store_at(tmp_path).create_job("generate_from_text", from_text_params(duration_s=duration))["params"]["duration_s"] == duration


@pytest.mark.parametrize("duration", [9.99, 0, 600.01, -5, float("nan"), float("inf"), "120", True])
def test_durations_outside_the_limits_are_refused(tmp_path, duration):
    err = _refused(store_at(tmp_path), **from_text_params(duration_s=duration))
    assert err.field == "duration_s" and "10 to 600" in err.message


@pytest.mark.parametrize("seed", [0, 42, 2 ** 32 - 1, None])
def test_a_seed_in_the_engine_range_or_null_is_accepted(tmp_path, seed):
    assert store_at(tmp_path).create_job("generate_from_text", from_text_params(seed=seed))["params"]["seed"] == seed


@pytest.mark.parametrize("seed", [-1, 2 ** 32, 1.5, "42", False])
def test_a_seed_outside_the_engine_range_is_refused(tmp_path, seed):
    assert _refused(store_at(tmp_path), **from_text_params(seed=seed)).field == "seed"


def test_lyrics_are_required_and_null_means_instrumental(tmp_path):
    store = store_at(tmp_path)
    assert _refused(store, style="ambient").field == "lyrics"
    assert store.create_job("generate_from_text", {"lyrics": None, "style": "ambient"})["params"]["lyrics"] is None
    blank = _refused(store, lyrics="  \n ", style="ambient")
    assert blank.field == "lyrics" and "null" in blank.message
    assert _refused(store, lyrics=["a line"], style="ambient").field == "lyrics"


def test_the_lyrics_are_kept_verbatim_with_stress_marks_and_spacing(tmp_path):
    text = "  [verse]\r\nМоло́ко и ё́жик  \n\n[chorus]\nla la\t\n"
    store = store_at(tmp_path)
    job = store.create_job("generate_from_text", from_text_params(lyrics=text))
    assert job["params"]["lyrics"] == text
    assert store_at(tmp_path).get_job(job["id"])["params"]["lyrics"] == text


def test_style_is_required(tmp_path):
    store = store_at(tmp_path)
    for style in (None, "", "   ", 5):
        assert _refused(store, lyrics="x", style=style).field == "style"


def test_unknown_params_another_engine_and_a_source_track_are_refused(tmp_path):
    store = store_at(tmp_path)
    unknown = _refused(store, **from_text_params(steps=50))
    assert unknown.field == "steps" and "unknown" in unknown.message
    assert _refused(store, **from_text_params(engine="yue2-studio")).field == "engine"
    assert _refused(store, **from_text_params(lyrics_prep="verbatim")).field == "lyrics_prep"
    assert _refused(store, **from_text_params(lyrics_prep={"x": float("nan")})).field == "lyrics_prep"
    with pytest.raises(MusicJobError) as err:
        store.create_job("generate_from_text", from_text_params(), track_id="trk_0123456789abcdef")
    assert err.value.code == "invalid_source"
    with pytest.raises(MusicJobError) as err:
        store.create_job("generate_from_text", ["not", "an", "object"])
    assert err.value.field == "params"


def test_an_unknown_job_type_is_refused(tmp_path):
    with pytest.raises(MusicJobError) as err:
        store_at(tmp_path).create_job("remix", {})
    assert err.value.code == "invalid_type" and err.value.field == "type"


def test_a_refused_request_leaves_nothing_on_disk(tmp_path):
    store = store_at(tmp_path)
    _refused(store, **from_text_params(variants=9))
    assert store.list_jobs() == []
    jobs_dir = tmp_path / "music" / "jobs"
    assert not jobs_dir.exists() or list(jobs_dir.iterdir()) == []


def test_the_same_idempotency_key_gives_the_same_job_and_other_params_are_a_conflict(tmp_path):
    store = store_at(tmp_path)
    first = store.create_job("generate_from_text", from_text_params(), idempotency_key="dialog-1")
    assert store.create_job("generate_from_text", from_text_params(), idempotency_key="dialog-1") == first
    assert len(store.list_jobs()) == 1
    with pytest.raises(MusicJobError) as err:
        store.create_job("generate_from_text", from_text_params(variants=3), idempotency_key="dialog-1")
    assert err.value.code == "idempotency_conflict"
