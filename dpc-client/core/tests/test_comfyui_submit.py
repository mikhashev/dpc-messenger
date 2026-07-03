"""Tests for comfyui_submit path resolution and UI->API conversion guard.

Covers the S11 fix (AI Studio): absolute-path workflow resolution and a loud
error when a UI-format graph carries unsupported custom-node widgets (instead
of silently dropping them, which produced invalid Bernini-R graphs).
"""

import json
import types

import pytest

import dpc_client_core.dpc_agent.tools.comfyui as cf
from dpc_client_core.dpc_agent.tools.comfyui import (
    _convert_ui_to_api,
    _format_progress_timing,
    _is_ui_format,
    _queue_item_label,
    _resolve_workflow_path,
)


def test_is_ui_format():
    assert _is_ui_format({"nodes": [], "links": []}) is True
    # API format is a flat {node_id: {class_type, inputs}} dict — no "nodes"
    assert _is_ui_format({"1": {"class_type": "KSampler", "inputs": {}}}) is False
    assert _is_ui_format({"nodes": "notalist"}) is False


def test_convert_ui_known_nodes_ok():
    wf = {
        "nodes": [
            {"id": 1, "type": "CLIPTextEncode", "widgets_values": ["a prompt"], "inputs": []},
            {"id": 2, "type": "SaveImage", "widgets_values": ["out"], "inputs": []},
        ],
        "links": [],
    }
    api = _convert_ui_to_api(wf)
    assert api["1"]["class_type"] == "CLIPTextEncode"
    assert api["1"]["inputs"]["text"] == "a prompt"
    assert api["2"]["inputs"]["filename_prefix"] == "out"


def test_convert_ui_unsupported_custom_node_raises():
    # A Bernini-R node carrying widget values is not in the whitelist:
    # converting would drop them, so the tool must refuse loudly.
    wf = {
        "nodes": [
            {"id": 1, "type": "BerniniRGuider", "widgets_values": ["auto", 4.5], "inputs": []},
            {"id": 2, "type": "LoadImage", "widgets_values": ["start.png"], "inputs": []},
        ],
        "links": [],
    }
    with pytest.raises(ValueError) as exc:
        _convert_ui_to_api(wf)
    msg = str(exc.value)
    assert "BerniniRGuider" in msg and "LoadImage" in msg
    assert "API format" in msg


def test_convert_ui_node_without_widgets_not_flagged():
    # A custom node with NO widget values loses nothing — allowed.
    wf = {
        "nodes": [
            {"id": 1, "type": "VAEDecode", "widgets_values": [], "inputs": []},
        ],
        "links": [],
    }
    api = _convert_ui_to_api(wf)
    assert api["1"]["class_type"] == "VAEDecode"


def test_resolve_workflow_absolute_path(tmp_path):
    wf_file = tmp_path / "graph.json"
    wf_file.write_text(json.dumps({"1": {"class_type": "KSampler", "inputs": {}}}), encoding="utf-8")
    ctx = types.SimpleNamespace(agent_root=tmp_path / "agent")
    resolved = _resolve_workflow_path(ctx, str(wf_file))
    assert resolved == wf_file


def test_resolve_workflow_under_agent_root(tmp_path):
    wf_dir = tmp_path / "comfy-ui-workflows"
    wf_dir.mkdir()
    (wf_dir / "g.json").write_text("{}", encoding="utf-8")
    ctx = types.SimpleNamespace(agent_root=tmp_path)
    resolved = _resolve_workflow_path(ctx, "g.json")
    assert resolved == wf_dir / "g.json"


def test_resolve_workflow_not_found(tmp_path):
    ctx = types.SimpleNamespace(agent_root=tmp_path)
    assert _resolve_workflow_path(ctx, "missing.json") is None


def _queue_entry(prompt_id, save_class=None, prefix=None, load_image=None):
    """Build a /queue entry: [number, prompt_id, prompt_dict, extra, outputs]."""
    prompt = {"1": {"class_type": "KSampler", "inputs": {"steps": 40}}}
    if load_image is not None:
        prompt["3"] = {"class_type": "LoadImage", "inputs": {"image": load_image}}
    if save_class:
        prompt["9"] = {"class_type": save_class, "inputs": {"filename_prefix": prefix}}
    return [0, prompt_id, prompt, {}, []]


def test_queue_item_label_prefers_input_frame():
    # Input frame (LoadImage.image) identifies the clip; it wins over the
    # save prefix, which is often a constant across clips.
    item = _queue_entry(
        "abc123def456", "BerniniRSaveVideo", "BerniniR_i2v_1.3B", load_image="C3_00012.png"
    )
    pid, label = _queue_item_label(item)
    assert pid == "abc123def456"  # full id preserved (feeds comfyui_check/_wait)
    assert label == "C3_00012.png"


def test_queue_item_label_falls_back_to_save_prefix():
    # No LoadImage → fall back to the save node's filename_prefix.
    item = _queue_entry("pid-1", "SaveImage", "C3_iceberg")
    pid, label = _queue_item_label(item)
    assert pid == "pid-1" and label == "C3_iceberg"


def test_queue_item_label_fallback_is_duck_typed():
    # The save-node fallback is detected by the presence of a filename_prefix
    # input, NOT a hardcoded class list — an unknown/custom saver still works.
    item = _queue_entry("pid-1c", "SomeFutureCustomSaver", "C9_label")
    pid, label = _queue_item_label(item)
    assert pid == "pid-1c" and label == "C9_label"


def test_queue_item_label_ignores_linked_image_input():
    # LoadImage.image as a [node, slot] link (not a filename) is skipped;
    # label falls back to the save prefix.
    item = _queue_entry("pid-1b", "SaveVideo", "out", load_image=["12", 0])
    pid, label = _queue_item_label(item)
    assert pid == "pid-1b" and label == "out"


def test_queue_item_label_no_label_sources():
    # No LoadImage and no save node → id only, empty label.
    pid, label = _queue_item_label(_queue_entry("pid-2"))
    assert pid == "pid-2" and label == ""


def test_queue_item_label_malformed_item():
    # Short/garbage entries degrade to ('', '') rather than raising.
    assert _queue_item_label([]) == ("", "")
    assert _queue_item_label([0]) == ("", "")
    assert _queue_item_label("notalist") == ("", "")
    pid, label = _queue_item_label([0, "pid-3", "promptnotadict"])
    assert pid == "pid-3" and label == ""


# ---- _format_progress_timing: s/it estimator (prompt_id boundary guard) ----
# Regression for the "384/250 s-per-step ghost": a healthy ~28s/step render
# looked "stuck" because the cross-call rate divided a whole clip-switch wall
# gap (tail + VAE + model reload + partial next clip) by a tiny step delta,
# since _LAST_PROGRESS was keyed by api_url only and ignored which clip the
# stored sample belonged to.


def test_progress_timing_within_call_two_samples_accurate():
    # Two real steps timed in one call -> exact delta, no history needed.
    cf._LAST_PROGRESS.clear()
    out = _format_progress_timing([(3, 20, 0.0), (5, 20, 56.0)], "http://x", "pC")
    assert "step 5/20" in out
    assert "~28.0s/it" in out      # 56s / 2 steps
    assert "ETA" in out            # (20-5)*28 = 420s


def test_progress_timing_crosscall_same_prompt_accurate():
    # One step per call, but the previous poll was the SAME running job ->
    # the cross-call delta is a trustworthy per-step rate.
    cf._LAST_PROGRESS.clear()
    first = _format_progress_timing([(5, 20, 100.0)], "http://x", "pA")
    assert "step 5/20" in first and "poll again" in first  # nothing to compare yet
    second = _format_progress_timing([(6, 20, 128.0)], "http://x", "pA")
    assert "~28.0s/it" in second   # 28s / 1 step, same prompt


def test_progress_timing_crosscall_different_prompt_rejected():
    # The queue advanced to the next clip between polls: prev sample is pA,
    # current is pB with a huge wall gap. Must NOT emit an inflated s/it.
    cf._LAST_PROGRESS.clear()
    _format_progress_timing([(11, 20, 100.0)], "http://x", "pA")
    out = _format_progress_timing([(16, 20, 1350.0)], "http://x", "pB")
    assert "step 16/20" in out
    assert "~" not in out          # the bug produced a ~250s/it rate here
    assert "poll again" in out
    # And the new sample is stored under pB for the next same-prompt poll.
    assert cf._LAST_PROGRESS["http://x"] == (16, 20, 1350.0, "pB")


def test_progress_timing_unknown_prompt_id_no_crosscall():
    # If ComfyUI never tags events with a prompt_id, the cross-call fallback is
    # disabled (empty id never matches) — report step, ask to re-poll, never
    # emit a possibly-wrong rate.
    cf._LAST_PROGRESS.clear()
    _format_progress_timing([(5, 20, 100.0)], "http://x", "")
    out = _format_progress_timing([(6, 20, 128.0)], "http://x", "")
    assert "step 6/20" in out and "poll again" in out
