"""The song lineage graph (`music/lineage.py`, `mp.lineage/1`; EVOLUTION.md, ADR-051 D29): built from
the jobs and takes the core stores, read-only, with the reason on every rejected edge.

The fake store is a list of job and take dicts: two generate jobs, a Keep and a Discard with a reason,
and one edit job with two sibling takes on the kept take.
"""

import copy
import json

import pytest

from dpc_client_core.music import lineage
from dpc_client_core.music.commands import MusicCommandsMixin
from dpc_client_core.music.schemas import SchemaError


def job(jid, *, type="generate_from_text", state="accepted", created="2026-11-25T10:00:00.000Z", track_id=None,
        action=None, region=None, params=None, takes=()):
    p = {"lyrics": "[verse]\nline one\nline two\n", "style": "indie rock, warm guitars", "variants": 2,
         "duration_s": 120, "seed": None, "engine": "yue2", "backbone": "stock", "cot": "full", "adapters": []}
    if params is not None:
        p = params
    return {"schema": "mp.job/1", "id": jid, "type": type, "track_id": track_id, "base_version_id": None,
            "comment_id": None, "action": action, "region": region, "params": p, "state": state,
            "state_reason": None, "created_at": created, "take_ids": list(takes), "requested_by": {"kind": "person"}}


def take(tid, jid, variant, *, kind="new_track", decision="pending", reason_text=None, reason_class=None,
         lib=None, created="2026-11-25T10:05:00.000Z", decided=None, region=None, qa="passed", engine="yue2.cpp",
         content_hash=None):
    gates = {g: {"status": "pass" if qa == "passed" else ("fail" if g == "clipping" else "pass")}
             for g in ("length", "clipping", "true_peak", "silence")}
    t = {"schema": "mp.take/1", "id": tid, "kind": kind, "job_id": jid, "variant": variant, "title": f"song v{variant}",
         "track_id": None, "base_version_id": None, "source_track_id": None, "region": region,
         "file": {"uri": f"file:///x/{tid}.flac", "content_hash": content_hash, "duration_s": 118.0},
         "qa": None, "qa_report": {"verdict": qa, "gates": gates, "advisory": {}, "measurements": {}},
         "decision": decision, "created_at": created, "decided_at": decided, "seed": 42, "engine": engine,
         "library_track_id": lib, "reason_text": reason_text, "reason_class": reason_class,
         "loudness": {"lufs_i": -14.2, "lra": 5.0, "true_peak_dbtp": -1.2, "measured_by": "t", "measured_at": "t"}}
    return t


A, B, E = "job_" + "a" * 16, "job_" + "b" * 16, "job_" + "e" * 16
T1, T2, T3 = "tak_" + "1" * 16, "tak_" + "2" * 16, "tak_" + "3" * 16
E1, E2 = "tak_" + "e" * 15 + "1", "tak_" + "e" * 15 + "2"


def store():
    jobs = [
        job(A, takes=[T1, T2], created="2026-11-25T10:00:00.000Z"),
        job(B, takes=[T3], state="rejected", created="2026-11-25T11:00:00.000Z", params={
            "lyrics": None, "style": "ambient", "variants": 1, "duration_s": 60, "seed": 7, "engine": "acestep"}),
        job(E, type="edit", action="regenerate_region", track_id="trk_one", region={"t_start": 20.0, "t_end": 30.0},
            created="2026-11-25T12:00:00.000Z", takes=[E1, E2],
            params={"takes": 2, "crossfade_ms": 40, "cut_with_shift": False, "engine": "acestep"}),
    ]
    takes = [
        take(T1, A, 1, decision="accepted", lib="trk_one", decided="2026-11-25T10:30:00.000Z",
             content_hash="sha256:" + "1" * 64),
        take(T2, A, 2, decision="rejected", reason_text="vocals sound robotic", reason_class="opt",
             lib="trk_two", decided="2026-11-25T10:31:00.000Z", qa="failed"),
        take(T3, B, 1, decision="rejected", reason_class="struct", lib=None, decided="2026-11-25T11:20:00.000Z",
             engine="acestep.cpp"),
        take(E1, E, 1, kind="edit", lib="trk_e1", region={"t_start": 20.0, "t_end": 30.0}, engine="acestep.cpp"),
        take(E2, E, 2, kind="edit", lib="trk_e2", region={"t_start": 20.0, "t_end": 30.0}, engine="acestep.cpp",
             decision="rejected", reason_text="seam clicks", decided="2026-11-25T12:30:00.000Z"),
    ]
    return jobs, takes


def build(**kw):
    jobs, takes = store()
    return lineage.build_lineage(jobs, takes, **kw)


def by_id(doc, key="nodes"):
    return {x["id"]: x for x in doc[key]}


def test_a_track_of_the_chain_gets_the_whole_branch_graph_with_its_edit_siblings():
    doc = build(track_id="trk_one")
    nodes, edges = by_id(doc), by_id(doc, "edges")
    assert doc["schema"] == "mp.lineage/1"
    assert set(nodes) == {f"intent:{A}", T1, T2, E1, E2}          # job B is another song
    assert doc["root"] == {"track_id": "trk_one", "job_id": None, "node_id": T1}
    assert (edges[f"intent:{A}>{T1}"]["kind"], edges[f"intent:{A}>{T1}"]["op"]) == ("generate", "generate_from_text")
    for e in (E1, E2):
        edge = edges[f"{T1}>{e}"]
        assert edge["kind"] == "edit" and edge["op"] == "regenerate_region" and edge["group"] == E
        assert edge["region"] == {"t_start": 20.0, "t_end": 30.0}
    assert [nodes[n]["generation"] for n in (f"intent:{A}", T1, E1)] == [0, 1, 2]
    assert doc["stats"] == {"jobs": 2, "takes": 4, "kept": 1, "discarded": 2, "pending": 1, "edits": 2,
                            "branches": 3, "max_generation": 2}


def test_a_node_carries_engine_params_qa_and_the_decision_with_its_reason():
    nodes = by_id(build(track_id="trk_one"))
    keep, drop, intent = nodes[T1], nodes[T2], nodes[f"intent:{A}"]
    assert keep["decision"] == "keep" and keep["reason"] is None and keep["on_kept_path"] is True
    assert keep["engine"] == "yue2.cpp" and keep["qa"]["verdict"] == "passed" and keep["lufs_i"] == -14.2
    assert keep["params"]["backbone"] == "stock" and keep["params"]["seed"] == 42
    assert drop["decision"] == "discard" and drop["reason"] == {"text": "vocals sound robotic", "class": "opt"}
    assert drop["qa"]["verdict"] == "failed" and drop["qa"]["failed"] == ["clipping"] and drop["on_kept_path"] is False
    assert nodes[E1]["decision"] == "pending" and nodes[E1]["on_kept_path"] is False
    assert nodes[E1]["params"]["action"] == "regenerate_region" and nodes[E1]["take_kind"] == "edit"
    # Live intent carries both the lyrics shape and the full lyrics text, plus resolved seed.
    assert intent["params"]["style"] == "indie rock, warm guitars"
    assert intent["params"]["lyrics"] == {"instrumental": False, "lines": 2}
    assert intent["params"]["lyrics_text"] == "[verse]\nline one\nline two\n"
    assert intent["params"]["seed"] == "random (42 on v1, v2)"


def test_a_rejected_edge_names_the_reason_and_a_kept_or_pending_edge_names_none():
    edges = by_id(build(track_id="trk_one"), "edges")
    assert edges[f"intent:{A}>{T2}"]["rejected"] is True
    assert edges[f"intent:{A}>{T2}"]["reason"] == {"text": "vocals sound robotic", "class": "opt"}
    assert edges[f"{T1}>{E2}"]["reason"] == {"text": "seam clicks", "class": None}
    assert edges[f"intent:{A}>{T1}"]["rejected"] is False and edges[f"intent:{A}>{T1}"]["reason"] is None
    assert edges[f"{T1}>{E1}"]["rejected"] is False


def test_selection_events_are_the_keep_with_its_a_b_pick_and_the_discards_with_reasons():
    events = by_id(build(track_id="trk_one"), "events")
    keep = events[f"sel:{T1}"]
    assert keep["kind"] == "keep" and keep["over_take_ids"] == [T2] and keep["ab"] is True
    assert keep["decided_by"] == {"kind": "person"} and keep["reason"] is None
    drop = events[f"sel:{T2}"]
    assert drop["kind"] == "discard" and drop["reason"]["text"] == "vocals sound robotic" and drop["over_take_ids"] == []
    assert f"sel:{E1}" not in events                                # pending: no event
    ordered = [e["id"] for e in build(track_id="trk_one")["events"]]
    assert ordered == [f"sel:{T1}", f"sel:{T2}", f"sel:{E2}"]       # by time of decision


def test_a_second_song_is_its_own_graph_and_a_job_id_asks_for_it_directly():
    doc = build(job_id=B)
    assert {n["id"] for n in doc["nodes"]} == {f"intent:{B}", T3}
    assert doc["root"]["node_id"] == f"intent:{B}"
    node = by_id(doc)[T3]
    assert node["decision"] == "discard" and node["reason"] == {"text": None, "class": "struct"}
    assert by_id(doc)[f"intent:{B}"]["params"]["lyrics"] == {"instrumental": True, "lines": 0}
    assert by_id(doc)[f"intent:{B}"]["engine"] == "acestep.cpp"
    assert doc["stats"]["kept"] == 0 and doc["stats"]["branches"] == 1


def test_an_edit_job_id_gives_the_chain_it_belongs_to():
    doc = build(job_id=E)
    assert {n["id"] for n in doc["nodes"]} >= {T1, E1, E2, f"intent:{A}"}
    assert doc["root"]["node_id"] == E1


def test_an_edit_whose_track_no_take_explains_starts_from_a_source_node():
    jobs, takes = store()
    jobs.append(job("job_" + "f" * 16, type="edit", action="remove_region", track_id="trk_imported",
                    region={"t_start": 1.0, "t_end": 2.0}, takes=["tak_" + "f" * 16], created="2026-11-25T13:00:00.000Z"))
    takes.append(take("tak_" + "f" * 16, "job_" + "f" * 16, 1, kind="edit", lib="trk_f", engine="ffmpeg",
                      region={"t_start": 1.0, "t_end": 2.0}))
    doc = lineage.build_lineage(jobs, takes, track_id="trk_imported",
                                entry_of=lambda i: {"title": "My old demo"} if i == "trk_imported" else None)
    nodes = by_id(doc)
    assert nodes["source:trk_imported"]["kind"] == "source" and nodes["source:trk_imported"]["label"] == "My old demo"
    assert doc["root"]["node_id"] == "source:trk_imported" and len(doc["nodes"]) == 2
    assert doc["edges"][0]["op"] == "remove_region"
    assert lineage.build_lineage(jobs, takes, track_id="trk_none")["nodes"][0]["kind"] == "source"


def test_the_parent_is_found_by_file_hash_when_the_library_id_changed_and_the_take_is_in_the_graph():
    jobs, takes = store()
    jobs[2]["track_id"] = "trk_relinked"
    jobs[2]["params"]["source_content_hash"] = "sha256:" + "1" * 64
    doc = lineage.build_lineage(jobs, takes, track_id="trk_one", job_id=E)    # A and E both in the graph
    assert by_id(doc, "edges")[f"{T1}>{E1}"]["kind"] == "edit"
    assert "source:trk_relinked" not in by_id(doc)
    assert not [n for n in doc["notes"] if n.startswith(lineage.HASH_NOTE)]


def _relinked_store():
    """E's source track was relinked (a new library id), so only the file hash of T1 names its parent."""
    jobs, takes = store()
    jobs[2]["track_id"] = "trk_relinked"
    jobs[2]["params"]["source_content_hash"] = "sha256:" + "1" * 64      # T1's hash, T1 is job A's take
    return jobs, takes


@pytest.mark.parametrize("ask", [{"job_id": E}, {"track_id": "trk_relinked"}, {"job_id": E, "track_id": "trk_relinked"}])
def test_a_graph_seeded_by_the_descendant_pulls_in_the_parent_job_found_by_a_unique_hash(ask):
    """Follow-up 19 (Zcode #199 finding 4): the hash link only worked when the parent's job was already in
    the graph, so a graph seeded by the descendant itself always said "outside this graph". The link now
    joins both jobs, whichever end the graph is seeded from."""
    jobs, takes = _relinked_store()
    doc = lineage.build_lineage(jobs, takes, **ask)
    nodes, edges = by_id(doc), by_id(doc, "edges")
    assert set(nodes) == {f"intent:{A}", T1, T2, E1, E2}               # A's song comes in with its own branch
    assert edges[f"{T1}>{E1}"]["kind"] == "edit" and f"source:trk_relinked>{E1}" not in edges
    assert "source:trk_relinked" not in nodes
    assert not [n for n in doc["notes"] if n.startswith(lineage.HASH_NOTE)]
    assert {j["id"] for j in doc["jobs"]} == {A, E}
    assert [nodes[n]["generation"] for n in (f"intent:{A}", T1, E1)] == [0, 1, 2]


def test_a_graph_seeded_by_the_parent_pulls_in_the_descendant_found_by_a_unique_hash():
    jobs, takes = _relinked_store()
    doc = lineage.build_lineage(jobs, takes, track_id="trk_one")
    assert {j["id"] for j in doc["jobs"]} == {A, E}
    assert by_id(doc, "edges")[f"{T1}>{E1}"]["kind"] == "edit"
    assert not [n for n in doc["notes"] if n.startswith(lineage.HASH_NOTE)]
    # an unrelated song (job B) is still its own graph
    assert B not in {j["id"] for j in doc["jobs"]}


def test_a_hash_two_takes_share_is_no_parent_and_no_graph_is_joined_by_it():
    """Zcode #191 on 4.2 (still true): a hash two takes share is not unique, so no parent is guessed."""
    jobs, takes = _relinked_store()
    next(t for t in takes if t["id"] == T2)["file"]["content_hash"] = "sha256:" + "1" * 64
    for ask in ({"job_id": E}, {"track_id": "trk_relinked"}):
        doc = lineage.build_lineage(jobs, takes, **ask)
        assert set(by_id(doc)) == {"source:trk_relinked", E1, E2}          # A's song is not pulled in
        assert f"source:trk_relinked>{E1}" in by_id(doc, "edges")
        [note] = [n for n in doc["notes"] if n.startswith(lineage.HASH_NOTE)]
        assert "matches 2 takes" in note and T1 in note and T2 in note
    assert E not in {j["id"] for j in lineage.build_lineage(jobs, takes, track_id="trk_one")["jobs"]}
    # even with both jobs seeded, the branch stays at its source node
    doc = lineage.build_lineage(jobs, takes, track_id="trk_one", job_id=E)
    assert f"source:trk_relinked>{E1}" in by_id(doc, "edges")


def test_a_unique_hash_match_in_a_job_with_no_lineage_stays_outside_the_graph_with_a_note():
    """What cannot be pulled in: the matching take belongs to a note-only edit job, which has no graph."""
    jobs, takes = _relinked_store()
    N, N1 = "job_" + "9" * 16, "tak_" + "9" * 16
    jobs.append(job(N, type="edit", action="note", track_id="trk_noted", created="2026-11-25T09:00:00.000Z",
                    takes=[N1], params={"takes": 1}))
    takes.append(take(N1, N, 1, kind="edit", lib="trk_n1", content_hash="sha256:" + "9" * 64))
    jobs[2]["params"]["source_content_hash"] = "sha256:" + "9" * 64
    doc = lineage.build_lineage(jobs, takes, job_id=E)
    assert set(by_id(doc)) == {"source:trk_relinked", E1, E2}
    [note] = [n for n in doc["notes"] if n.startswith(lineage.HASH_NOTE)]
    assert N1 in note and N in note and "outside this graph" in note


def test_the_parent_is_the_stored_source_track_before_any_take_with_the_same_hash():
    """Review #180 4.2: the parent was looked up by content hash and the stored source_track_id was ignored."""
    jobs, takes = store()
    t2 = next(t for t in takes if t["id"] == T2)
    t2["file"]["content_hash"] = "sha256:" + "2" * 64
    jobs[2]["track_id"] = "trk_relinked"                      # the job's own id no longer names a take
    jobs[2]["params"]["source_content_hash"] = "sha256:" + "2" * 64   # and the hash names another take
    for t in takes:
        if t["job_id"] == E:
            t["source_track_id"] = "trk_one"                  # what the takes stored when they were made
    edges = by_id(lineage.build_lineage(jobs, takes, job_id=E), "edges")
    assert f"{T1}>{E1}" in edges and f"{T2}>{E1}" not in edges
    # with no stored source the hash still finds the parent, when its take is in the graph
    for t in takes:
        t["source_track_id"] = None
    assert f"{T2}>{E1}" in by_id(lineage.build_lineage(jobs, takes, track_id="trk_two", job_id=E), "edges")


def test_a_cycle_in_the_stored_records_is_broken_and_said_and_the_rest_is_drawn():
    """Review #180 4.3: one cycle failed the whole graph with no partial render."""
    X, Y = "job_" + "c" * 16, "job_" + "d" * 16
    X1, Y1 = "tak_" + "c" * 16, "tak_" + "d" * 16
    jobs = [job(X, type="edit", action="regenerate_region", track_id="trk_y1", region={"t_start": 1.0, "t_end": 2.0},
                takes=[X1], created="2026-11-25T10:00:00.000Z", params={"takes": 1}),
            job(Y, type="edit", action="regenerate_region", track_id="trk_x1", region={"t_start": 3.0, "t_end": 4.0},
                takes=[Y1], created="2026-11-25T11:00:00.000Z", params={"takes": 1})]
    takes = [take(X1, X, 1, kind="edit", lib="trk_x1", created="2026-11-25T10:05:00.000Z"),
             take(Y1, Y, 1, kind="edit", lib="trk_y1", created="2026-11-25T11:05:00.000Z")]
    doc = lineage.build_lineage(jobs, takes, job_id=X)
    lineage.validate_lineage(doc)
    assert set(by_id(doc)) == {X1, Y1} and len(doc["edges"]) == 1
    [note] = [n for n in doc["notes"] if n.startswith(lineage.CYCLE_NOTE)]
    assert Y1 in note and X1 in note and "left out" in note
    gen = {n["id"]: n["generation"] for n in doc["nodes"]}
    assert gen[X1] == 0 and gen[Y1] == 1
    # a graph with no cycle carries no such note
    assert not [n for n in build(track_id="trk_one")["notes"] if n.startswith(lineage.CYCLE_NOTE)]


def test_a_comment_only_edit_job_and_other_job_types_are_not_in_the_graph():
    jobs, takes = store()
    jobs.append(job("job_" + "c" * 16, type="edit", action="note", track_id="trk_one"))
    jobs.append(job("job_" + "d" * 16, type="qc", track_id="trk_one"))
    doc = lineage.build_lineage(jobs, takes, track_id="trk_one")
    assert {j["id"] for j in doc["jobs"]} == {A, E}
    with pytest.raises(lineage.LineageError) as err:
        lineage.build_lineage(jobs, takes, job_id="job_" + "d" * 16)
    assert err.value.code == "not_found"
    with pytest.raises(lineage.LineageError) as err:
        lineage.build_lineage(jobs, takes)
    assert err.value.code == "invalid_params"


def test_a_real_job_store_with_a_keep_and_a_discard_gives_the_same_shape(tmp_path):
    from tests._music_jobs_helpers import job_in_review, store_at
    real = store_at(tmp_path / "dpc")
    j, takes = job_in_review(real, variants=2)
    real.keep_take(takes[0]["id"])
    real.discard_take(takes[1]["id"], reason_text="muddy low end", reason_class="sel")
    jobs = real.list_jobs()
    doc = lineage.build_lineage(jobs, [t for x in jobs for t in real.list_takes(x["id"])], job_id=j["id"])
    nodes = by_id(doc)
    assert nodes[takes[0]["id"]]["decision"] == "keep" and nodes[takes[0]["id"]]["qa"]["verdict"] == "not_measured"
    assert nodes[takes[1]["id"]]["reason"] == {"text": "muddy low end", "class": "sel"}
    assert nodes[takes[1]["id"]]["params"]["synth_batch_index"] == 1
    assert nodes[f"intent:{j['id']}"]["job_state"] == "accepted"
    assert [e["kind"] for e in doc["events"]] == ["keep", "discard"]


def test_building_the_graph_changes_nothing_it_reads():
    jobs, takes = store()
    before = copy.deepcopy((jobs, takes))
    lineage.build_lineage(jobs, takes, track_id="trk_one")
    assert (jobs, takes) == before


def test_the_document_validates_and_a_broken_one_is_refused():
    doc = build(track_id="trk_one")
    lineage.validate_lineage(doc)
    lineage.validate_lineage(json.loads(json.dumps(doc)))
    for mutate, text in (
        (lambda d: d.update(schema="mp.lineage/2"), "schema"),
        (lambda d: d["edges"][0].update({"to": "tak_missing"}), "from and to"),
        (lambda d: d["nodes"].append(dict(d["nodes"][0])), "duplicate node"),
        (lambda d: next(n for n in d["nodes"] if n.get("decision") == "discard").update(reason=None), "reason"),
        (lambda d: next(n for n in d["nodes"] if n.get("decision") == "keep").update(reason={"text": "x", "class": None}), "only a discarded"),
        (lambda d: d["events"][0].update(decided_by={"kind": "agent"}), "only a person"),
        (lambda d: d["edges"][0].update(kind="clone"), "'kind'"),
        (lambda d: d["edges"][0].update(rejected=True), "reason"),
        (lambda d: next(n for n in d["nodes"] if n["kind"] == "take").update(generation=0), "generation"),
        (lambda d: d.pop("stats"), "stats"),
    ):
        broken = copy.deepcopy(doc)
        mutate(broken)
        with pytest.raises(SchemaError, match=text):
            lineage.validate_lineage(broken)


def test_the_layout_gives_each_generation_a_column_and_centres_a_parent_on_its_children():
    doc = build(track_id="trk_one")
    pos = lineage.layout(doc)
    assert pos[f"intent:{A}"][0] < pos[T1][0] < pos[E1][0]
    assert pos[E1][1] != pos[E2][1]
    assert pos[T1][1] == (pos[E1][1] + pos[E2][1]) / 2        # T1's only children are the two edit siblings
    assert len({(round(x), round(y)) for x, y in pos.values()}) == len(pos)      # nothing on top of another


def test_the_static_page_shows_the_branches_and_the_reason_on_a_rejected_edge_and_escapes_text():
    jobs, takes = store()
    takes[1]["reason_text"] = "<b>robotic</b> & thin"
    doc = lineage.build_lineage(jobs, takes, track_id="trk_one")
    page = lineage.render_html(doc)
    assert page.startswith("<!doctype html>") and "<script" not in page and "http" not in page.replace("http-equiv", "")
    assert "rejected: &lt;b&gt;robotic&lt;/b&gt; &amp; thin" in page
    assert "<b>robotic</b>" not in page
    assert page.count('class="edge rejected"') == 2 and "regenerate region 20.0-30.0 s" in page
    assert "<b>keep</b>" in page and "No Keep or Discard yet" not in page


def test_the_files_are_written_beside_the_job_and_replaced_whole(tmp_path):
    doc = build(track_id="trk_one")
    folder = tmp_path / "generated" / A
    out = lineage.write_lineage_files(doc, folder)
    assert json.loads((folder / "lineage.json").read_text(encoding="utf-8")) == lineage.redact_lineage_for_export(doc)
    assert (folder / "lineage.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert out == {"json": str(folder / "lineage.json"), "html": str(folder / "lineage.html")}
    lineage.write_lineage_files(doc, folder)                         # again: replaced, no temp files left
    assert sorted(p.name for p in folder.iterdir()) == ["lineage.html", "lineage.json"]
    with pytest.raises(SchemaError):
        lineage.write_lineage_files({"schema": "nope"}, tmp_path / "x")


# -- the command ----------------------------------------------------------------------------------


class _Store:
    def __init__(self):
        self.jobs, self.takes = store()

    def list_jobs(self, **_kw):
        return copy.deepcopy(self.jobs)

    def list_takes(self, job_id):
        return copy.deepcopy([t for t in self.takes if t["job_id"] == job_id])


class _Library:
    async def start(self):
        return None

    def get_entry(self, track_id):
        from dpc_client_core.music.library import MusicLibraryError
        if track_id in ("trk_one", "trk_two"):
            return {"id": track_id, "title": track_id}
        raise MusicLibraryError("unknown_track", f"no track {track_id}")


class _Settings:
    def __init__(self, home):
        self.dpc_home_dir = home


class _Service(MusicCommandsMixin):
    def __init__(self, home):
        self.settings = _Settings(home)
        self._music_jobs_instance = _Store()
        self._music_library_instance = _Library()


@pytest.mark.asyncio
async def test_the_command_answers_the_lineage_for_a_track_or_a_job_and_maps_errors(tmp_path):
    service = _Service(tmp_path / "dpc")
    ok = await service.get_music_lineage(track_id="trk_two")
    assert ok["status"] == "ok" and ok["lineage"]["root"]["node_id"] == T2 and ok["lineage"]["stats"]["jobs"] == 2
    lineage.validate_lineage(ok["lineage"])
    assert (await service.get_music_lineage(job_id=B))["lineage"]["stats"]["takes"] == 1
    for bad, code in (({}, "invalid_params"), ({"job_id": "job_nope"}, "not_found"),
                      ({"track_id": "trk_unknown"}, "unknown_track"), ({"track_id": 5}, "invalid_params")):
        answer = await service.get_music_lineage(**bad)
        assert answer["status"] == "error" and answer["code"] == code, (bad, answer)


@pytest.mark.asyncio
async def test_the_write_command_puts_both_files_in_the_generated_folder_of_the_job(tmp_path):
    home = tmp_path / "dpc"
    service = _Service(home)
    answer = await service.write_music_lineage_files(track_id="trk_one")
    assert answer["status"] == "ok"
    folder = home / "music" / "generated" / A
    assert (folder / "lineage.json").is_file() and (folder / "lineage.html").is_file()
    assert answer["files"]["json"] == str(folder / "lineage.json")
    bad = await service.write_music_lineage_files()
    assert bad["status"] == "error" and bad["code"] == "invalid_params"


def test_the_lineage_commands_are_on_the_door_list_and_the_local_api_allowlist():
    from dpc_client_core.local_api import ALLOWED_COMMANDS
    from dpc_client_core.music.commands import MUSIC_COMMANDS
    for name in ("get_music_lineage", "write_music_lineage_files"):
        assert name in MUSIC_COMMANDS and name in ALLOWED_COMMANDS


def test_selections_in_the_same_millisecond_follow_the_store_order_never_the_take_id():
    """Follow-up 17: events were sorted by (decided_at, event id); a Keep and a Discard stamped in the same
    millisecond came out in either order, because take ids are random."""
    J = "job_" + "9" * 16
    late_id, early_id = "tak_" + "f" * 16, "tak_" + "0" * 16       # the id order is the reverse of the store's
    same = "2026-11-25T10:30:00.000Z"
    jobs = [job(J, takes=[late_id, early_id])]
    takes = [take(late_id, J, 1, decision="accepted", decided=same),
             take(early_id, J, 2, decision="rejected", reason_text="flat", decided=same)]
    events = lineage.build_lineage(jobs, takes, job_id=J)["events"]
    assert [(e["kind"], e["take_id"]) for e in events] == [("keep", late_id), ("discard", early_id)]
    # the same records in another order give the same events
    again = lineage.build_lineage(jobs, list(reversed(takes)), job_id=J)["events"]
    assert [e["id"] for e in again] == [e["id"] for e in events]
    # a later decision still comes after an earlier one whatever the take order
    takes[0]["decided_at"] = "2026-11-25T10:30:00.001Z"
    assert [e["kind"] for e in lineage.build_lineage(jobs, takes, job_id=J)["events"]] == ["discard", "keep"]


def test_a_real_store_keep_and_discard_in_the_same_millisecond_come_out_in_one_order(tmp_path):
    from tests._music_jobs_helpers import job_in_review, store_at
    for n in range(8):                                   # fresh random take ids each round
        real = store_at(tmp_path / f"dpc{n}")
        j, takes = job_in_review(real, variants=2)
        real._now = lambda: "2026-11-25T10:30:00.000Z"
        real.keep_take(takes[0]["id"])
        real.discard_take(takes[1]["id"], reason_text="muddy low end", reason_class="sel")
        jobs = real.list_jobs()
        doc = lineage.build_lineage(jobs, [t for x in jobs for t in real.list_takes(x["id"])], job_id=j["id"])
        assert [e["take_id"] for e in doc["events"]] == [takes[0]["id"], takes[1]["id"]]


def test_live_lineage_carries_full_lyrics_resolved_random_seed_adapter_strengths_and_who_asked(tmp_path):
    """Card 8: live lineage carries full lyrics text, random seed resolved from takes, adapter
    strengths, resolved requested_by (agent in group and person in player), and forward-compatible
    request block."""
    home = tmp_path / "dpc_home"
    agent_id = "agent_studio_01"
    conv_id = "group-a1b2c3d4e5f6"
    (home / "agents" / agent_id).mkdir(parents=True)
    (home / "agents" / agent_id / "config.json").write_text(
        json.dumps({"agent_id": agent_id, "name": "StudioBot"}), encoding="utf-8"
    )
    (home / "conversations" / conv_id).mkdir(parents=True)
    (home / "conversations" / conv_id / "metadata.json").write_text(
        json.dumps({"group_id": conv_id, "name": "Music Room"}), encoding="utf-8"
    )

    J_AGENT = "job_" + "1" * 16
    T_AGENT = "tak_" + "a" * 16
    J_MULTI = "job_" + "2" * 16
    T_M1, T_M2 = "tak_" + "b" * 15 + "1", "tak_" + "b" * 15 + "2"
    J_PLAYER = "job_" + "3" * 16
    T_PLAYER = "tak_" + "c" * 16

    lyrics_body = "[verse]\nNeon rain on empty streets\nEchoes under silver lights\n"
    adapters = [{"name": "voice-demo-bass", "music": 0.45, "sound": 0.35}]
    request_block = {
        "message_id": "msg-abc-123",
        "title": "Neon Rain",
        "text": "Please generate a synthwave track for tonight's demo.",
        "asked": {"duration_s": 180, "style": "synthwave"},
        "changes": ["duration_s: 180 -> 240"],
    }

    j_agent = job(
        J_AGENT,
        takes=[T_AGENT],
        params={
            "lyrics": lyrics_body,
            "style": "synthwave",
            "variants": 1,
            "duration_s": 240,
            "seed": None,
            "engine": "yue2",
            "backbone": "stock",
            "cot": "full",
            "adapters": adapters,
        },
    )
    j_agent["requested_by"] = {
        "kind": "agent",
        "agent_id": agent_id,
        "conversation_id": conv_id,
        "turn_id": "turn-42",
    }
    j_agent["request"] = request_block
    t_agent = take(T_AGENT, J_AGENT, 1, lib="trk_agent")
    t_agent["seed"] = 3555429983

    j_multi = job(
        J_MULTI,
        takes=[T_M1, T_M2],
        params={"lyrics": None, "style": "ambient", "variants": 2, "duration_s": 60, "seed": None, "engine": "yue2"},
    )
    t_m1 = take(T_M1, J_MULTI, 1, lib="trk_m1")
    t_m1["seed"] = 101
    t_m2 = take(T_M2, J_MULTI, 2, lib="trk_m2")
    t_m2["seed"] = 202

    j_player = job(
        J_PLAYER,
        takes=[T_PLAYER],
        params={"lyrics": None, "style": "piano solo", "variants": 1, "duration_s": 60, "seed": 0, "engine": "acestep"},
    )
    t_player = take(T_PLAYER, J_PLAYER, 1, lib="trk_p1", engine="acestep.cpp")
    t_player["seed"] = 0

    doc = lineage.build_lineage(
        [j_agent, j_multi, j_player],
        [t_agent, t_m1, t_m2, t_player],
        job_id=J_AGENT,
        dpc_home_dir=home,
    )
    lineage.validate_lineage(doc)
    nodes = by_id(doc)
    intent = nodes[f"intent:{J_AGENT}"]
    assert intent["params"]["lyrics"] == {"instrumental": False, "lines": 2}
    assert intent["params"]["lyrics_text"] == lyrics_body
    assert intent["params"]["seed"] == "random (3555429983 on v1)"
    assert intent["params"]["adapters"] == adapters
    assert nodes[T_AGENT]["params"]["adapters"] == adapters
    assert intent["requested_by"]["label"] == "asked by StudioBot in Music Room"
    assert intent["requested_by"]["agent_name"] == "StudioBot"
    assert intent["requested_by"]["conversation_name"] == "Music Room"
    assert intent["request"] == request_block

    jobs_by_id = by_id(doc, "jobs")
    assert jobs_by_id[J_AGENT]["requested_by"]["label"] == "asked by StudioBot in Music Room"
    assert jobs_by_id[J_AGENT]["request"] == request_block

    # Fallback when disk has no agent/group metadata
    empty_home = tmp_path / "empty_home"
    fallback_doc = lineage.build_lineage([j_agent], [t_agent], job_id=J_AGENT, dpc_home_dir=empty_home)
    assert by_id(fallback_doc, "jobs")[J_AGENT]["requested_by"]["label"] == f"asked by {agent_id} in {conv_id}"

    # Multi-take distinct random seeds
    multi_doc = lineage.build_lineage([j_multi], [t_m1, t_m2], job_id=J_MULTI, dpc_home_dir=home)
    assert by_id(multi_doc)[f"intent:{J_MULTI}"]["params"]["seed"] == "random (v1: 101, v2: 202)"

    # Explicit zero seed and person player requester
    player_doc = lineage.build_lineage([j_player], [t_player], job_id=J_PLAYER, dpc_home_dir=home)
    assert by_id(player_doc)[f"intent:{J_PLAYER}"]["params"]["seed"] == 0
    assert by_id(player_doc, "jobs")[J_PLAYER]["requested_by"] == {
        "kind": "person",
        "surface": "player",
        "label": "asked by you in Player",
    }


def test_shareable_lineage_html_keeps_shape_only_lyrics_and_omits_private_chat_and_lyrics_text(tmp_path):
    """Card 8 privacy invariant: lineage.html must NEVER expose full lyrics text, conversation_id,
    conversation_name, turn_id, message_id, or request.text, while keeping shape-only lyrics and
    resolved seed / adapters."""
    home = tmp_path / "dpc_home"
    agent_id = "agent_studio_01"
    conv_id = "group-f1e2d3c4b5a6"
    (home / "agents" / agent_id).mkdir(parents=True)
    (home / "agents" / agent_id / "config.json").write_text(
        json.dumps({"agent_id": agent_id, "name": "StudioBot"}), encoding="utf-8"
    )
    (home / "conversations" / conv_id).mkdir(parents=True)
    (home / "conversations" / conv_id / "metadata.json").write_text(
        json.dumps({"group_id": conv_id, "name": "Private Group Title"}), encoding="utf-8"
    )

    J = "job_" + "5" * 16
    T = "tak_" + "5" * 16
    j = job(
        J,
        takes=[T],
        params={
            "lyrics": "[verse]\nSecret private stanza one\nSecret private stanza two\n",
            "style": "acoustic folk",
            "variants": 1,
            "duration_s": 120,
            "seed": None,
            "engine": "yue2",
            "adapters": [{"name": "voice-demo-bass", "music": 0.45, "sound": 0.35}],
        },
    )
    j["requested_by"] = {
        "kind": "agent",
        "agent_id": agent_id,
        "conversation_id": conv_id,
        "turn_id": "turn-secret-99",
    }
    j["request"] = {
        "message_id": "msg-secret-77",
        "title": "Acoustic Demo",
        "text": "Private personal request message that must stay local",
        "asked": {"duration_s": 90},
        "changes": ["duration_s: 90 -> 120"],
    }
    t = take(T, J, 1, lib="trk_priv")
    t["seed"] = 3555429983

    doc = lineage.build_lineage([j], [t], job_id=J, dpc_home_dir=home)
    out = lineage.write_lineage_files(doc, tmp_path / "out")
    page = (tmp_path / "out" / "lineage.html").read_text(encoding="utf-8")
    assert out["html"].endswith("lineage.html")

    # Shape-only lyrics, resolved seed, adapters, and safe requester/title/changes are present
    assert "2 lines" in page
    assert "random (3555429983 on v1)" in page
    assert "voice-demo-bass (music 0.45, sound 0.35)" in page
    assert "asked by StudioBot" in page
    assert "Acoustic Demo" in page

    # Private fields must never leak into shareable lineage.html
    for forbidden in (
        "Secret private stanza",
        conv_id,
        "Private Group Title",
        "turn-secret-99",
        "msg-secret-77",
        "Private personal request message",
    ):
        assert forbidden not in page


def test_write_lineage_files_redacts_lineage_json_like_html_without_mutating_live_doc(tmp_path):
    """CC #51: on-disk lineage.json beside lineage.html must redact the same private fields as
    lineage.html (no lyrics_text, conversation_id/name, turn_id, message_id, or request.text),
    while keeping shape-only lyrics, safe provenance, and the caller's live doc unmutated."""
    home = tmp_path / "dpc_home"
    agent_id = "agent_studio_01"
    conv_id = "group-f1e2d3c4b5a6"
    (home / "agents" / agent_id).mkdir(parents=True)
    (home / "agents" / agent_id / "config.json").write_text(
        json.dumps({"agent_id": agent_id, "name": "StudioBot"}), encoding="utf-8"
    )
    (home / "conversations" / conv_id).mkdir(parents=True)
    (home / "conversations" / conv_id / "metadata.json").write_text(
        json.dumps({"group_id": conv_id, "name": "Private Group Title"}), encoding="utf-8"
    )

    J = "job_" + "6" * 16
    T = "tak_" + "6" * 16
    lyrics_body = "[verse]\nSecret private stanza one\nSecret private stanza two\n"
    j = job(
        J,
        takes=[T],
        params={
            "lyrics": lyrics_body,
            "style": "acoustic folk",
            "variants": 1,
            "duration_s": 120,
            "seed": None,
            "engine": "yue2",
            "adapters": [{"name": "voice-demo-bass", "music": 0.45, "sound": 0.35}],
        },
    )
    j["requested_by"] = {
        "kind": "agent",
        "agent_id": agent_id,
        "conversation_id": conv_id,
        "turn_id": "turn-secret-99",
    }
    j["request"] = {
        "message_id": "msg-secret-77",
        "title": "Acoustic Demo",
        "text": "Private personal request message that must stay local",
        "asked": {"duration_s": 90},
        "changes": ["duration_s: 90 -> 120"],
    }
    t = take(T, J, 1, lib="trk_priv")
    t["seed"] = 3555429983

    doc = lineage.build_lineage([j], [t], job_id=J, dpc_home_dir=home)
    snapshot = copy.deepcopy(doc)
    out = lineage.write_lineage_files(doc, tmp_path / "out")

    # Caller's in-memory live doc is untouched
    assert doc == snapshot
    assert doc["nodes"][0]["params"]["lyrics_text"] == lyrics_body

    raw_json = (tmp_path / "out" / "lineage.json").read_text(encoding="utf-8")
    exported = json.loads(raw_json)
    assert out["json"].endswith("lineage.json")
    lineage.validate_lineage(exported)

    for forbidden in (
        "Secret private stanza",
        conv_id,
        "Private Group Title",
        "turn-secret-99",
        "msg-secret-77",
        "Private personal request message",
    ):
        assert forbidden not in raw_json

    exp_intent = {n["id"]: n for n in exported["nodes"]}[f"intent:{J}"]
    assert exp_intent["params"]["lyrics"] == {"instrumental": False, "lines": 2}
    assert "lyrics_text" not in exp_intent["params"]
    assert exp_intent["params"]["seed"] == "random (3555429983 on v1)"
    assert exp_intent["requested_by"] == {
        "kind": "agent",
        "agent_id": agent_id,
        "agent_name": "StudioBot",
        "label": "asked by StudioBot",
    }
    assert exp_intent["request"] == {
        "title": "Acoustic Demo",
        "asked": {"duration_s": 90},
        "changes": ["duration_s: 90 -> 120"],
    }

    exp_job = {x["id"]: x for x in exported["jobs"]}[J]
    assert exp_job["requested_by"] == exp_intent["requested_by"]
    assert exp_job["request"] == exp_intent["request"]


@pytest.mark.asyncio
async def test_music_commands_pass_dpc_home_dir_to_build_lineage_and_redact_written_json(tmp_path):
    """CC #51: MusicCommandsMixin._music_lineage passes dpc_home_dir so requested_by resolves
    from settings.dpc_home_dir, live API keeps full fields, and written lineage.json is redacted."""
    home = tmp_path / "dpc_home"
    agent_id = "agent_studio_02"
    conv_id = "group-001122334455"
    (home / "agents" / agent_id).mkdir(parents=True)
    (home / "agents" / agent_id / "config.json").write_text(
        json.dumps({"agent_id": agent_id, "name": "MixerBot"}), encoding="utf-8"
    )
    (home / "conversations" / conv_id).mkdir(parents=True)
    (home / "conversations" / conv_id / "metadata.json").write_text(
        json.dumps({"group_id": conv_id, "name": "Studio Room"}), encoding="utf-8"
    )

    J = "job_" + "7" * 16
    T = "tak_" + "7" * 16
    j = job(J, takes=[T], params={"lyrics": "[verse]\nPrivate lyric line\n", "style": "lo-fi", "engine": "yue2"})
    j["requested_by"] = {
        "kind": "agent",
        "agent_id": agent_id,
        "conversation_id": conv_id,
        "turn_id": "turn-88",
    }
    j["request"] = {
        "message_id": "msg-88",
        "title": "Lo-Fi Sketch",
        "text": "Private chat prompt text",
    }
    t = take(T, J, 1, lib="trk_one")

    service = _Service(home)
    service._music_jobs_instance.jobs = [j]
    service._music_jobs_instance.takes = [t]

    live = await service.get_music_lineage(job_id=J)
    assert live["status"] == "ok"
    live_intent = {n["id"]: n for n in live["lineage"]["nodes"]}[f"intent:{J}"]
    assert live_intent["requested_by"]["agent_name"] == "MixerBot"
    assert live_intent["requested_by"]["conversation_name"] == "Studio Room"
    assert live_intent["requested_by"]["label"] == "asked by MixerBot in Studio Room"
    assert live_intent["params"]["lyrics_text"] == "[verse]\nPrivate lyric line\n"
    assert live_intent["request"]["text"] == "Private chat prompt text"

    written = await service.write_music_lineage_files(job_id=J)
    assert written["status"] == "ok"
    # Live response returned to UI still has full fields
    written_live_intent = {n["id"]: n for n in written["lineage"]["nodes"]}[f"intent:{J}"]
    assert written_live_intent["params"]["lyrics_text"] == "[verse]\nPrivate lyric line\n"
    assert written_live_intent["requested_by"]["label"] == "asked by MixerBot in Studio Room"

    # On-disk lineage.json is redacted
    from pathlib import Path
    disk_json = Path(written["files"]["json"]).read_text(encoding="utf-8")
    for forbidden in ("Private lyric line", conv_id, "Studio Room", "turn-88", "msg-88", "Private chat prompt text"):
        assert forbidden not in disk_json
