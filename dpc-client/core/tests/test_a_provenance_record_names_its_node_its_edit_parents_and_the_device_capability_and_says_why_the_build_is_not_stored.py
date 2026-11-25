"""Backlog card THE-PROVENANCE-RECORD-STILL-LEAVES-THE-WAV-CONTAINER-THE-BUILD-AND-THE-LINEAGE-FIELDS-OPEN
(PROVENANCE §2): what needs no decision of Mike's is filled. `parents` of a region edit (`id`,
`edge_kind`, `op`, `N_delta`, in `lineage.py`'s vocabulary); `node_id` (the public id only, off in the
lossy export, switchable); the device's compute capability; and the build, which stays `not_stored`
with the reason because the engines do not report it."""

import json
import subprocess

import pytest

from dpc_client_core.music import provenance as pv
from dpc_client_core.music.commands import MusicCommandsMixin
from tests.test_the_provenance_record_is_built_per_engine_and_refuses_private_or_oversized_content import (
    ACE_IDENTITY, FACTS, acestep, yue2)

NODE = "dpc-node-3f9a1c7e5b2d4a6081c3e5f7a9b1d3f5"


@pytest.fixture(autouse=True)
def _no_node_source():
    pv.set_node_id_source(None)
    yield
    pv.set_node_id_source(None)


PARENT = {"role": "base", "library_track_id": "trk_base", "content_hash": "md5:" + "ab" * 16,
          "version_id": "ver_0", "take_id": "tak_0123456789abcdef", "job_id": "job_0123456789abcdef",
          "comment_id": None, "licence_key": "mit"}
EDIT = {"action": "regenerate_region", "region": {"t_start": 10.0, "t_end": 14.0},
        "caption_from": "the base's record", "lyrics_from": "the base's record"}


def edit_record(**over):
    kw = dict(job_id="job_00000000000000aa", variant=1, created_at="2026-11-25T10:00:00Z", action="regenerate_region",
              task_type="repaint", identity=ACE_IDENTITY, parent=PARENT, edit=EDIT, duration_requested_s=60.0,
              duration_received_s=60.0, loudness=None, device=FACTS["device"], engine_output=FACTS["engine_output"],
              postprocess=FACTS["postprocess"], output=FACTS["output"], seed=7, batch_size=2, batch_index=0)
    kw.update(over)
    return pv.build_edit_record(**kw)


# -- parents, edge_kind, op, N_delta ------------------------------------------------------------------


def test_a_generated_song_has_no_parent():
    for rec in (acestep(), yue2()):
        assert rec["parents"] == [] and rec["relation"] == "song_from_own_text"


def test_a_region_edit_names_its_parent_take_with_the_lineage_vocabulary():
    entry = edit_record()["parents"][0]
    assert entry["id"] == "tak_0123456789abcdef"            # the node id lineage.py gives the parent take
    assert entry["edge_kind"] == "edit" and entry["op"] == "regenerate_region"
    assert entry["N_delta"] == 1 and "N_delta_note" not in entry
    assert entry["library_track_id"] == "trk_base" and entry["content_hash"] == PARENT["content_hash"]
    assert entry["take_id"] == "tak_0123456789abcdef" and entry["licence_key"] == "mit"


def test_the_op_is_the_jobs_action_and_a_removal_has_one_changed_field_too():
    removal = edit_record(action="remove_region", task_type="remove_region", identity={"engine": "dpc"},
                          edit={"action": "remove_region", "region": EDIT["region"], "caption_from": None,
                                "lyrics_from": None})
    entry = removal["parents"][0]
    assert entry["op"] == "remove_region" and entry["edge_kind"] == "edit" and entry["N_delta"] == 1
    assert edit_record(action="fix_seam")["parents"][0]["op"] == "fix_seam"


def test_a_parent_that_is_no_take_is_a_source_node_as_in_the_lineage_graph():
    entry = edit_record(parent={**PARENT, "take_id": None, "job_id": None})["parents"][0]
    assert entry["id"] == "source:trk_base" and entry["take_id"] is None


def test_n_delta_is_not_stored_with_the_reason_when_the_text_came_with_the_job():
    for field in ("caption_from", "lyrics_from"):
        entry = edit_record(edit={**EDIT, field: "params"})["parents"][0]
        assert entry["N_delta"] == pv.NOT_STORED
        assert "parent's text" in entry["N_delta_note"]
        assert entry["edge_kind"] == "edit"             # the kind is known whatever N_delta is


def test_a_finished_edit_record_passes_the_guard_and_the_reader_and_its_ids_are_unchanged_by_the_parents():
    rec, text = pv.finalize(edit_record())
    parsed, problem = pv.parse_embedded(text)
    assert problem is None and parsed["parents"][0]["N_delta"] == 1
    other = edit_record(parent={**PARENT, "take_id": "tak_fedcba9876543210"})
    assert pv.derive_ids(other)["render_sha256"] == pv.derive_ids(edit_record())["render_sha256"]


# -- node_id ------------------------------------------------------------------------------------------


def test_node_id_is_the_public_id_the_source_gives_in_every_record():
    pv.set_node_id_source(lambda: NODE)
    for rec in (acestep(), yue2(), edit_record()):
        assert rec["node_id"] == NODE
        pv.finalize(dict(rec))                              # the privacy guard accepts it


def test_node_id_is_not_stored_without_a_source_or_when_the_source_gives_no_node_id():
    assert acestep()["node_id"] == pv.NOT_STORED
    for given in (None, "", 7, "C:/Users/someone/.dpc/node.key", "dpc-node-", "dpc-node-" + "a" * 65,
                  "-----BEGIN PRIVATE KEY-----", "dpc-node-abc def"):
        pv.set_node_id_source(lambda given=given: given)
        assert pv.node_identity() == pv.NOT_STORED, given
    pv.set_node_id_source(lambda: (_ for _ in ()).throw(RuntimeError("no p2p yet")))
    assert acestep()["node_id"] == pv.NOT_STORED            # a failing source never stops a take


def test_node_id_is_in_no_id_so_the_genome_and_render_ids_do_not_move():
    before = pv.derive_ids(acestep())
    pv.set_node_id_source(lambda: NODE)
    assert pv.derive_ids(acestep()) == before


def test_a_record_read_from_a_file_must_carry_a_node_id_or_a_marker():
    pv.set_node_id_source(lambda: NODE)
    rec, _ = pv.finalize(acestep())
    pv.validate_embedded(rec)
    pv.validate_embedded({**rec, "node_id": pv.NOT_STORED})
    pv.validate_embedded({**rec, "node_id": pv.NODE_ID_WITHHELD})
    with pytest.raises(ValueError, match="node_id"):
        pv.validate_embedded({**rec, "node_id": "../../etc/passwd"})


def test_the_export_copy_withholds_the_node_id_and_the_original_keeps_it():
    pv.set_node_id_source(lambda: NODE)
    rec = acestep()
    out = pv.withhold_node_id(rec)
    assert out["node_id"] == pv.NODE_ID_WITHHELD and rec["node_id"] == NODE
    assert {k: v for k, v in out.items() if k != "node_id"} == {k: v for k, v in rec.items() if k != "node_id"}
    assert pv.withhold_node_id({"schema": pv.SCHEMA, "node_id": pv.NOT_STORED})["node_id"] == pv.NOT_STORED
    assert "node_id" not in pv.withhold_node_id({"schema": pv.SCHEMA})


def test_the_mp3_export_carries_the_record_without_the_node_id(tmp_path):
    from tests.test_the_mp3_export_carries_the_source_record_and_the_lyrics_and_old_files_are_re_tagged_by_a_script import (
        _export, _source, _tagged_flac)
    pv.set_node_id_source(lambda: NODE)
    rec, text = pv.finalize(acestep())
    assert rec["node_id"] == NODE
    flac = _tagged_flac(tmp_path, text)
    sidecar = {**rec, "take_id": "tak_0123456789abcdef", "library_track_id": "trk_x", "embedded_in_file": True}
    _, tags, _ = _export(tmp_path, _source(flac, take_record=sidecar, origin="generated"))
    exported = json.loads(tags["DPC_PROVENANCE"])
    assert exported["source_record"]["node_id"] == pv.NODE_ID_WITHHELD
    assert NODE not in tags["DPC_PROVENANCE"]
    assert exported["source_record"]["audio_codes_sha256"] == rec["audio_codes_sha256"]
    # an old sidecar-only take (no embedded record) is withheld too
    path2 = _tagged_flac(tmp_path / "two")
    _, tags2, _ = _export(tmp_path / "two", _source(path2, take_record=sidecar, origin="generated"))
    assert NODE not in tags2["DPC_PROVENANCE"]


class _Settings:
    def __init__(self, values):
        self.values = values

    def get(self, section, key, fallback=None):
        return self.values.get((section, key), fallback)


class _Service(MusicCommandsMixin):
    def __init__(self, node_id, settings=None):
        self.settings = _Settings(settings or {})
        if node_id is not None:
            self.p2p_manager = type("P", (), {"node_id": node_id})()


def test_the_service_gives_the_p2p_managers_public_id_unless_the_setting_is_off():
    assert _Service(NODE)._music_provenance_node_id() == NODE
    assert _Service(NODE, {("music", "provenance_node_id"): "false"})._music_provenance_node_id() is None
    assert _Service(NODE, {("music", "provenance_node_id"): "on"})._music_provenance_node_id() == NODE
    assert _Service(None)._music_provenance_node_id() is None          # no p2p manager yet
    pv.set_node_id_source(_Service(NODE)._music_provenance_node_id)
    assert acestep()["node_id"] == NODE


# -- build, device ------------------------------------------------------------------------------------


def test_the_build_stays_not_stored_and_the_record_says_why():
    for rec in (acestep(), yue2()):
        assert rec["build"] == pv.NOT_STORED
        assert "/health" in rec["build_note"] and "CUDA architecture" in rec["build_note"]
        assert "engine_binaries" in rec["build_note"]               # what does identify the binary
    removal = edit_record(identity={"engine": "dpc"})
    assert removal["build"] == pv.NOT_STORED and "no engine binary" in removal["build_note"]


class _Smi:
    """A stand-in `nvidia-smi` runner: answers by the fields asked for."""

    def __init__(self, with_cap=True, line="NVIDIA GeForce RTX 5090, 596.86, 12.0"):
        self.with_cap, self.line, self.calls = with_cap, line, []

    def __call__(self, cmd, *, timeout):
        self.calls.append(list(cmd))
        if "compute_cap" in cmd[1]:
            if not self.with_cap:
                return subprocess.CompletedProcess(cmd, 2, b"", b'Field "compute_cap" is not a valid field')
            return subprocess.CompletedProcess(cmd, 0, (self.line + "\n").encode(), b"")
        name, driver = self.line.split(", ")[:2]
        return subprocess.CompletedProcess(cmd, 0, f"{name}, {driver}\n".encode(), b"")


def test_the_device_records_its_compute_capability_beside_the_driver(monkeypatch):
    monkeypatch.setattr(pv.shutil, "which", lambda name: "nvidia-smi")
    smi = _Smi()
    assert pv.device_identity(smi) == {"gpu": "NVIDIA GeForce RTX 5090", "driver": "596.86",
                                       "compute_capability": "12.0", "source": "nvidia-smi"}
    assert len(smi.calls) == 1


def test_an_old_driver_without_compute_cap_still_gives_the_name_and_driver(monkeypatch):
    monkeypatch.setattr(pv.shutil, "which", lambda name: "nvidia-smi")
    smi = _Smi(with_cap=False)
    assert pv.device_identity(smi) == {"gpu": "NVIDIA GeForce RTX 5090", "driver": "596.86", "source": "nvidia-smi"}
    assert len(smi.calls) == 2


def test_a_capability_that_is_not_a_number_is_left_out(monkeypatch):
    monkeypatch.setattr(pv.shutil, "which", lambda name: "nvidia-smi")
    out = pv.device_identity(_Smi(line="Some GPU, 1.2, [N/A]"))
    assert out["gpu"] == "Some GPU" and "compute_capability" not in out


def test_no_nvidia_smi_is_still_not_stored(monkeypatch):
    monkeypatch.setattr(pv.shutil, "which", lambda name: None)
    assert pv.device_identity(_Smi())["gpu"] == pv.NOT_STORED
