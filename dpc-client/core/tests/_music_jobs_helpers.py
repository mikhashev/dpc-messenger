"""Shared setup for the music job store tests: a store whose engine probe says the engine
is installed (so jobs queue), and a runner stand-in that drives a job to review."""

from dpc_client_core.music.capabilities import EngineStatus, MusicCapabilities
from dpc_client_core.music.jobs import MusicJobStore

LM_BODY = '{"caption":"indie rock","lyrics":"[verse]\\nТысячи умов","duration":30,"seed":42}'.encode("utf-8")


def installed_probe():
    return EngineStatus(state="stopped", reason="installed; DPC starts it on demand", supervised=True)


def store_at(home, probe=installed_probe):
    store = MusicJobStore(home, capabilities=MusicCapabilities(probe))
    store.open()
    return store


def from_text_params(**over):
    params = {"lyrics": "[verse]\nline one\n", "style": "indie rock, warm guitars"}
    params.update(over)
    return params


def queued_job(store, **over):
    return store.create_job("generate_from_text", from_text_params(**over))


def running_job(store, **over):
    job = queued_job(store, **over)
    store.write_payload(job["id"], "lm.request.json", LM_BODY, endpoint="/lm")
    return store.start_job(job["id"])


def add_variant(store, job_id, variant, *, batch_size=2):
    return store.add_take(
        job_id, variant=variant, file_uri=f"file:///music/generated/{job_id}/variant_{variant}.flac",
        content_hash=f"sha256:{variant:064x}", title=f"variant {variant}", seed=42,
        synth_batch_size=batch_size, synth_batch_index=(variant - 1) % batch_size,
        payload="lm.request.json", sample_rate=48000, channels=2, duration_s=119.8)


def job_in_review(store, variants=2):
    job = running_job(store, variants=variants)
    takes = [add_variant(store, job["id"], v) for v in range(1, variants + 1)]
    return store.finish_job(job["id"]), takes
