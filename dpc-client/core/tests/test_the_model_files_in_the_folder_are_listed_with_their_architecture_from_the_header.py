"""`list_model_files`: the GGUFs in the model files folder, read by header, not by name.

Step 2 of the model files folder flow. The walk is the folder and one level of
subfolders (`strata` skipped); a split model is one row; `is_mmproj` is
`general.architecture == "clip"` from the header, so a projector without "mmproj" in
its name is found and a model with it in its name is not taken for one. A header that
cannot be read still gives a row (architecture null, is_mmproj false). Every file here
is a tiny synthetic header under tmp_path; nothing touches `~/.dpc`.
"""

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.managers.llama_server_supervisor import (
    gguf_string_key, model_files_in, run_configs_in,
)
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings


def _s(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _kv_string(key: str, value: str) -> bytes:
    return _s(key) + struct.pack("<I", 8) + _s(value)


def _kv_u32(key: str, value: int) -> bytes:
    return _s(key) + struct.pack("<I", 4) + struct.pack("<I", value)


def _kv_string_array(key: str, values) -> bytes:
    return _s(key) + struct.pack("<I", 9) + struct.pack("<IQ", 8, len(values)) + b"".join(_s(v) for v in values)


def _kv_int_array(key: str, values) -> bytes:
    return (_s(key) + struct.pack("<I", 9) + struct.pack("<IQ", 5, len(values))
            + b"".join(struct.pack("<i", v) for v in values))


def _gguf(*kvs: bytes, version: int = 3, pad: int = 0) -> bytes:
    return b"GGUF" + struct.pack("<IQQ", version, 0, len(kvs)) + b"".join(kvs) + b"\0" * pad


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _arch(name: str, pad: int = 0) -> bytes:
    return _gguf(_kv_string("general.architecture", name), _kv_u32("general.file_type", 7), pad=pad)


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "models"


def _rows(folder):
    return {(r["folder"], Path(r["path"]).name): r for r in model_files_in(folder)}


class TestTheArchitectureIsReadFromTheHeader:
    def test_the_first_key(self, tmp_path):
        p = _write(tmp_path / "a.gguf", _arch("qwen35"))
        assert gguf_string_key(str(p), "general.architecture") == "qwen35"

    def test_a_key_that_is_not_the_first_after_an_int_a_string_array_and_an_int_array(self, tmp_path):
        data = _gguf(
            _kv_u32("general.quantization_version", 2),
            _kv_string_array("tokenizer.ggml.tokens", ["<a>", "bb", ""]),
            _kv_int_array("tokenizer.ggml.token_type", [1, 2, 3]),
            _kv_string("general.name", "some name"),
            _kv_string("general.architecture", "deepseek2-ocr"),
        )
        p = _write(tmp_path / "late.gguf", data)
        assert gguf_string_key(str(p), "general.architecture") == "deepseek2-ocr"

    def test_a_missing_key_is_none(self, tmp_path):
        p = _write(tmp_path / "x.gguf", _gguf(_kv_string("general.name", "n")))
        assert gguf_string_key(str(p), "general.architecture") is None

    def test_a_key_that_is_not_a_string_is_none(self, tmp_path):
        p = _write(tmp_path / "x.gguf", _gguf(_kv_u32("general.architecture", 5)))
        assert gguf_string_key(str(p), "general.architecture") is None

    def test_version_one_is_none(self, tmp_path):
        p = _write(tmp_path / "v1.gguf", _gguf(_kv_string("general.architecture", "llama"), version=1))
        assert gguf_string_key(str(p), "general.architecture") is None

    def test_a_truncated_header_is_none_at_every_cut(self, tmp_path):
        data = _gguf(_kv_string("general.name", "abc"), _kv_string("general.architecture", "qwen35"))
        for cut in range(len(data) - 1):
            p = _write(tmp_path / "cut.gguf", data[:cut])
            assert gguf_string_key(str(p), "general.architecture") is None, cut
        assert gguf_string_key(str(_write(tmp_path / "whole.gguf", data)), "general.architecture") == "qwen35"

    def test_a_length_that_runs_past_the_end_is_none(self, tmp_path):
        lying = _s("general.name") + struct.pack("<I", 8) + struct.pack("<Q", 10**12)
        p = _write(tmp_path / "lie.gguf", b"GGUF" + struct.pack("<IQQ", 3, 0, 2) + lying)
        assert gguf_string_key(str(p), "general.architecture") is None

    def test_a_file_that_is_not_gguf_or_does_not_exist_is_none(self, tmp_path):
        p = _write(tmp_path / "t.gguf", b"this is not a model at all, just text")
        assert gguf_string_key(str(p), "general.architecture") is None
        assert gguf_string_key(str(tmp_path / "gone.gguf"), "general.architecture") is None


class TestTheWalk:
    def test_files_in_the_folder_and_one_level_of_subfolders_deeper_ones_are_ignored(self, folder):
        _write(folder / "top.gguf", _arch("llama"))
        _write(folder / "owner--repo" / "m.gguf", _arch("qwen35"))
        _write(folder / "owner--repo" / "deeper" / "too-deep.gguf", _arch("llama"))
        _write(folder / "owner--repo" / "notes.txt", b"x")
        rows = _rows(folder)
        assert set(rows) == {("", "top.gguf"), ("owner--repo", "m.gguf")}
        assert rows[("", "top.gguf")]["folder"] == ""
        assert rows[("owner--repo", "m.gguf")]["architecture"] == "qwen35"

    def test_the_strata_subfolder_is_skipped(self, folder):
        _write(folder / "strata" / "pack.gguf", _arch("llama"))
        _write(folder / "other" / "m.gguf", _arch("llama"))
        assert set(_rows(folder)) == {("other", "m.gguf")}

    def test_a_row_carries_exactly_the_agreed_fields_and_an_absolute_path(self, folder):
        _write(folder / "r" / "m.gguf", _arch("qwen35", pad=1000))
        (row,) = model_files_in(folder)
        assert set(row) == {"path", "folder", "size_bytes", "architecture", "is_mmproj", "shard_count"}
        assert Path(row["path"]).is_absolute() and Path(row["path"]) == (folder / "r" / "m.gguf").resolve()
        assert row["size_bytes"] == (folder / "r" / "m.gguf").stat().st_size
        assert row["shard_count"] == 1 and row["is_mmproj"] is False

    def test_a_missing_folder_is_empty_and_is_not_created(self, folder):
        assert model_files_in(folder) == []
        assert run_configs_in(folder) == []
        assert not folder.exists()

    def test_rows_are_sorted_by_folder_then_file_name(self, folder):
        for rel in ("zeta/b.gguf", "alpha/z.gguf", "alpha/a.gguf", "top-b.gguf", "top-a.gguf", "mid/m.gguf"):
            _write(folder / rel, _arch("llama"))
        got = [(r["folder"], Path(r["path"]).name) for r in model_files_in(folder)]
        assert got == [("", "top-a.gguf"), ("", "top-b.gguf"), ("alpha", "a.gguf"), ("alpha", "z.gguf"),
                       ("mid", "m.gguf"), ("zeta", "b.gguf")]


class TestAProjectorIsFoundByItsArchitecture:
    def test_clip_is_a_projector_whatever_the_file_is_called(self, folder):
        _write(folder / "r" / "Qwen3.8-27B-Uncensored-vision-f16.gguf", _arch("clip"))
        _write(folder / "r" / "mmproj-looks-like-one.gguf", _arch("qwen35"))
        rows = _rows(folder)
        assert rows[("r", "Qwen3.8-27B-Uncensored-vision-f16.gguf")]["is_mmproj"] is True
        assert rows[("r", "Qwen3.8-27B-Uncensored-vision-f16.gguf")]["architecture"] == "clip"
        assert rows[("r", "mmproj-looks-like-one.gguf")]["is_mmproj"] is False

    def test_an_unreadable_header_still_has_a_row_with_null_and_false(self, folder):
        _write(folder / "r" / "text.gguf", b"not a gguf")
        _write(folder / "r" / "cut.gguf", _arch("clip")[:20])
        _write(folder / "r" / "empty.gguf", b"")
        rows = _rows(folder)
        assert len(rows) == 3
        for row in rows.values():
            assert row["architecture"] is None
            assert row["is_mmproj"] is False, "false, never null"


class TestASplitModelIsOneRow:
    def _split(self, folder, sub, stem, count, arch="qwen35moe", missing=()):
        sizes = []
        for i in range(1, count + 1):
            if i in missing:
                continue
            data = _arch(arch if i == 1 else "ignored-in-later-parts", pad=100 * i)
            _write(folder / sub / f"{stem}-{i:05d}-of-{count:05d}.gguf", data)
            sizes.append(len(data))
        return sizes

    def test_only_the_first_part_is_listed_with_the_summed_size_and_the_shard_count(self, folder):
        sizes = self._split(folder, "big", "Model-Q4", 3)
        _write(folder / "big" / "other.gguf", _arch("llama"))
        rows = _rows(folder)
        assert set(rows) == {("big", "Model-Q4-00001-of-00003.gguf"), ("big", "other.gguf")}
        row = rows[("big", "Model-Q4-00001-of-00003.gguf")]
        assert row["shard_count"] == 3
        assert row["size_bytes"] == sum(sizes)
        assert row["architecture"] == "qwen35moe"
        assert rows[("big", "other.gguf")]["shard_count"] == 1

    def test_two_split_models_beside_each_other_do_not_sum_into_one(self, folder):
        a = self._split(folder, "s", "A", 2)
        b = self._split(folder, "s", "B", 2)
        rows = _rows(folder)
        assert rows[("s", "A-00001-of-00002.gguf")]["size_bytes"] == sum(a)
        assert rows[("s", "B-00001-of-00002.gguf")]["size_bytes"] == sum(b)

    def test_a_missing_later_part_sums_what_is_there(self, folder):
        sizes = self._split(folder, "s", "M", 3, missing=(3,))
        row = _rows(folder)[("s", "M-00001-of-00003.gguf")]
        assert row["shard_count"] == 3 and row["size_bytes"] == sum(sizes)


class TestRunConfigs:
    def test_strata_run_configs_are_listed_sorted_and_absolute(self, folder):
        d = folder / "strata" / "run-configs"
        for name in ("strata-b.json", "strata-a.json", "notes.json", "strata-c.txt"):
            _write(d / name, b"{}")
        (d / "strata-dir.json").mkdir()
        got = run_configs_in(folder)
        assert got == [str((d / "strata-a.json").resolve()), str((d / "strata-b.json").resolve())]
        assert all(Path(p).is_absolute() for p in got)

    def test_no_run_configs_folder_is_an_empty_list(self, folder):
        _write(folder / "m" / "a.gguf", _arch("llama"))
        assert run_configs_in(folder) == []


def _host(settings):
    return SimpleNamespace(settings=settings)


class TestTheCommand:
    def test_the_command_is_allowed_and_is_a_service_method(self):
        assert "list_model_files" in ALLOWED_COMMANDS
        assert callable(getattr(CoreService, "list_model_files", None))

    @pytest.mark.asyncio
    async def test_the_reply_carries_the_resolved_folder_the_rows_and_the_run_configs(self, tmp_path):
        settings = Settings(tmp_path / "home")
        chosen = tmp_path / "gguf"
        settings.set_model_files_dir(str(chosen))
        _write(chosen / "o--r" / "m.gguf", _arch("qwen35"))
        _write(chosen / "o--r" / "p.gguf", _arch("clip"))
        _write(chosen / "strata" / "run-configs" / "strata-x.json", b"{}")
        out = await CoreService.list_model_files(_host(settings))
        assert out["status"] == "success"
        assert out["model_files_dir"] == str(chosen)
        assert [(r["folder"], r["architecture"], r["is_mmproj"]) for r in out["model_files"]] == [
            ("o--r", "qwen35", False), ("o--r", "clip", True)]
        assert out["run_configs"] == [str((chosen / "strata" / "run-configs" / "strata-x.json").resolve())]

    @pytest.mark.asyncio
    async def test_the_default_folder_missing_gives_empty_lists_and_is_not_created(self, tmp_path):
        settings = Settings(tmp_path / "home")
        out = await CoreService.list_model_files(_host(settings))
        assert out == {"status": "success", "model_files_dir": str(tmp_path / "home" / "models"),
                       "model_files": [], "run_configs": []}
        assert not (tmp_path / "home" / "models").exists()

    @pytest.mark.asyncio
    async def test_a_failure_in_the_walk_is_an_error_reply_not_a_raise(self, tmp_path, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as sup

        def boom(folder):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(sup, "model_files_in", boom)
        out = await CoreService.list_model_files(_host(Settings(tmp_path / "home")))
        assert out["status"] == "error" and "disk gone" in out["message"]
