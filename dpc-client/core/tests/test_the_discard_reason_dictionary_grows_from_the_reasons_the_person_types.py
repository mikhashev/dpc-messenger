"""The Discard reasons are a dictionary the person's own typing grows (SPEC 35, ADR-051 D32):
empty at first, one entry per reason up to case and whitespace, ordered by use then last use,
with an optional failure class that a later Discard carries to its take.
"""

import itertools
import threading

import pytest

from dpc_client_core.music.capabilities import MusicCapabilities
from dpc_client_core.music.jobs import MusicJobError, MusicJobStore
from tests._music_jobs_helpers import installed_probe, job_in_review, queued_job, running_job, add_variant


def make_store(home):
    ticks = itertools.count(1)
    store = MusicJobStore(home, capabilities=MusicCapabilities(installed_probe),
                          now=lambda: f"2026-11-21T10:00:{next(ticks):02d}Z")
    store.open()
    return store


def pending_takes(store, count):
    """`count` pending takes, over as many review jobs as needed (two takes a job)."""
    out = []
    while len(out) < count:
        _, takes = job_in_review(store)
        out.extend(takes)
    return out[:count]


def keys(store, **kw):
    return [r["key"] for r in store.list_reasons(**kw)]


def test_the_dictionary_starts_empty(tmp_path):
    store = make_store(tmp_path)
    assert store.list_reasons() == []
    assert not store.reasons_path.exists()


def test_a_typed_reason_appears_with_its_first_spelling(tmp_path):
    store = make_store(tmp_path)
    take = pending_takes(store, 1)[0]
    store.discard_take(take["id"], reason_text="  Muddy   mix ")
    (row,) = store.list_reasons()
    assert row["text"] == "Muddy mix" and row["key"] == "muddy mix"
    assert row["uses"] == 1 and row["reason_class"] is None
    assert row["first_used_at"] == row["last_used_at"]


def test_a_class_alone_adds_nothing_to_the_dictionary(tmp_path):
    store = make_store(tmp_path)
    take = pending_takes(store, 1)[0]
    store.discard_take(take["id"], reason_class="listen")
    assert store.list_reasons() == []


def test_case_and_whitespace_variants_merge_and_keep_the_first_spelling(tmp_path):
    store = make_store(tmp_path)
    a, b, c = pending_takes(store, 3)
    store.discard_take(a["id"], reason_text="Muddy mix")
    store.discard_take(b["id"], reason_text="muddy   MIX")
    store.discard_take(c["id"], reason_text=" MUDDY mix ")
    (row,) = store.list_reasons()
    assert row["text"] == "Muddy mix" and row["uses"] == 3
    assert row["first_used_at"] < row["last_used_at"]


def test_the_order_follows_uses_then_last_use(tmp_path):
    store = make_store(tmp_path)
    takes = pending_takes(store, 5)
    for take, text in zip(takes, ["thin", "muddy", "off key", "muddy", "thin"]):
        store.discard_take(take["id"], reason_text=text)
    # thin and muddy used twice; thin was used last, so it leads; off key (once) trails
    assert keys(store) == ["thin", "muddy", "off key"]
    assert keys(store, limit=2) == ["thin", "muddy"]
    assert keys(store, prefix="M") == ["muddy"]
    assert keys(store, prefix="  TH") == ["thin"]
    assert keys(store, prefix="zzz") == []


def test_reject_all_counts_one_use_and_gives_every_take_the_reason(tmp_path):
    store = make_store(tmp_path)
    job, takes = job_in_review(store)
    store.reject_all_takes(job["id"], reason_text="none fit")
    (row,) = store.list_reasons()
    assert row["uses"] == 1
    assert {t["reason_text"] for t in store.list_takes(job["id"])} == {"none fit"}


def test_the_class_is_carried_to_later_takes(tmp_path):
    store = make_store(tmp_path)
    a, b, c, d = pending_takes(store, 4)
    store.discard_take(a["id"], reason_text="muddy mix")
    entry = store.set_reason_class("Muddy Mix", "opt")
    assert entry["reason_class"] == "opt"
    carried = store.discard_take(b["id"], reason_text="MUDDY mix")
    assert carried["reason_class"] == "opt"
    # a class the call gives wins for the take, and does not overwrite the entry's
    own = store.discard_take(c["id"], reason_text="muddy mix", reason_class="listen")
    assert own["reason_class"] == "listen"
    assert store.list_reasons()[0]["reason_class"] == "opt"
    # reject-all carries it too
    job, _ = job_in_review(store)
    store.reject_all_takes(job["id"], reason_text="muddy mix")
    assert {t["reason_class"] for t in store.list_takes(job["id"])} == {"opt"}
    # the first earlier take, decided before the class existed, keeps what it had
    assert store.get_take(a["id"])["reason_class"] is None


def test_a_class_given_with_a_new_reason_is_attached_to_its_entry_once(tmp_path):
    store = make_store(tmp_path)
    a, b = pending_takes(store, 2)
    store.discard_take(a["id"], reason_text="sibilant", reason_class="prep")
    assert store.list_reasons()[0]["reason_class"] == "prep"
    assert store.discard_take(b["id"], reason_text="sibilant")["reason_class"] == "prep"


def test_the_class_is_optional_and_can_be_cleared(tmp_path):
    store = make_store(tmp_path)
    a, b = pending_takes(store, 2)
    assert store.discard_take(a["id"], reason_text="flat")["reason_class"] is None
    store.set_reason_class("flat", "sel")
    assert store.set_reason_class("flat", None)["reason_class"] is None
    assert store.discard_take(b["id"], reason_text="flat")["reason_class"] is None


def test_an_invalid_class_is_refused_and_an_unknown_key_is_not_found(tmp_path):
    store = make_store(tmp_path)
    store.discard_take(pending_takes(store, 1)[0]["id"], reason_text="flat")
    with pytest.raises(MusicJobError) as bad:
        store.set_reason_class("flat", "vibes")
    assert bad.value.code == "invalid_params" and bad.value.field == "reason_class"
    assert store.list_reasons()[0]["reason_class"] is None
    with pytest.raises(MusicJobError) as missing:
        store.set_reason_class("never typed", "opt")
    assert missing.value.code == "not_found"


def test_the_dictionary_persists_across_a_store_restart(tmp_path):
    store = make_store(tmp_path)
    a, b = pending_takes(store, 2)
    store.discard_take(a["id"], reason_text="Muddy mix")
    store.discard_take(b["id"], reason_text="muddy mix")
    store.set_reason_class("muddy mix", "opt")
    before = store.list_reasons()

    again = make_store(tmp_path)
    assert again.list_reasons() == before
    assert before[0]["uses"] == 2 and before[0]["reason_class"] == "opt"


def test_an_unreadable_dictionary_is_set_aside_and_starts_empty(tmp_path):
    store = make_store(tmp_path)
    store.discard_take(pending_takes(store, 1)[0]["id"], reason_text="flat")
    store.reasons_path.write_text("{not json", encoding="utf-8")
    again = make_store(tmp_path)
    assert again.list_reasons() == []
    assert list(store.root.glob("reasons.json.corrupt-*"))


def test_concurrent_discards_keep_the_counts_right(tmp_path):
    store = make_store(tmp_path)
    takes = pending_takes(store, 12)
    variants = ["Muddy mix", "muddy  mix", "MUDDY MIX"]
    errors = []

    def discard(i, take):
        try:
            store.discard_take(take["id"], reason_text=variants[i % 3])
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=discard, args=(i, t)) for i, t in enumerate(takes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    (row,) = store.list_reasons()
    assert row["uses"] == 12 and row["text"] in variants
    assert make_store(tmp_path).list_reasons()[0]["uses"] == 12


@pytest.mark.asyncio
async def test_the_reason_commands_list_and_classify_what_a_discard_typed(tmp_path):
    from tests._music_job_commands_helpers import review_job, service_with_engine
    service = service_with_engine(tmp_path / "dpc")
    assert (await service.list_music_discard_reasons()) == {"status": "ok", "reasons": []}
    _, takes = review_job(service._music_jobs_instance, [None, None])
    await service.discard_music_take(take_id=takes[0]["id"], reason_text="Muddy mix")

    listed = await service.list_music_discard_reasons(prefix="mud", limit=5)
    assert listed["status"] == "ok" and [r["text"] for r in listed["reasons"]] == ["Muddy mix"]
    set_ = await service.set_music_discard_reason_class(key="muddy MIX", reason_class="opt")
    assert set_["status"] == "ok" and set_["reason"]["reason_class"] == "opt"
    again = await service.discard_music_take(take_id=takes[1]["id"], reason_text="muddy mix")
    assert again["take"]["reason_class"] == "opt"

    bad = await service.set_music_discard_reason_class(key="muddy mix", reason_class="vibes")
    assert bad["status"] == "error" and bad["code"] == "invalid_params" and bad["field"] == "reason_class"
    assert (await service.set_music_discard_reason_class(key="nope", reason_class=None))["code"] == "not_found"
    assert (await service.list_music_discard_reasons(limit=0))["code"] == "invalid_params"
    assert (await service.set_music_discard_reason_class())["code"] == "invalid_params"
