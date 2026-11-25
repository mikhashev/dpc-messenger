"""`build.json` (PROVENANCE §2, Mike 2026-11-26): `tools/music/write_engine_build_json.py` writes
`dpc.engine-build/1` beside a built engine from `CMakeCache.txt` and git; DPC reads it once per engine
start, validates it, and puts it in each record's `build`. A missing or invalid file keeps `not_stored` and
names the file."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from dpc_client_core.music import engine_build as eb
from dpc_client_core.music import provenance as pv
from dpc_client_core.music.acestep import AceStepServer, read_config as ace_read_config
from tests.test_the_provenance_record_is_built_per_engine_and_refuses_private_or_oversized_content import (
    ACE_IDENTITY, acestep, yue2)

HERE = Path(__file__).resolve()
TOOL = HERE.parents[3] / "tools" / "music" / "write_engine_build_json.py"

CACHE = """\
# This is the CMakeCache file.
// Build type
CMAKE_BUILD_TYPE:STRING=Release
//CUDA architectures
CMAKE_CUDA_ARCHITECTURES:STRING=120a-real
//The CXX compiler
CMAKE_CXX_COMPILER:FILEPATH=C:/Program Files/Microsoft Visual Studio/2022/Community/VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64/cl.exe
GGML_CUDA:BOOL=ON
"""
CXX = 'set(CMAKE_CXX_COMPILER_ID "MSVC")\nset(CMAKE_CXX_COMPILER_VERSION "19.44.35228.0")\n'
CUDA = ('set(CMAKE_CUDA_COMPILER "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.3/bin/nvcc.exe")\n'
        'set(CMAKE_CUDA_COMPILER_VERSION "13.3.33")\nset(CMAKE_CUDA_COMPILER_TOOLKIT_VERSION "13.3")\n')
SERVER = "ace-server.exe" if sys.platform == "win32" else "ace-server"


def load_tool():
    spec = importlib.util.spec_from_file_location("write_engine_build_json", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)


def make_source(tmp_path, remote="https://example.invalid/someone/acestep.cpp.git"):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.cpp").write_text("int main(){}\n", encoding="utf-8")
    git(src, "init", "-q")
    git(src, "add", "main.cpp")
    git(src, "commit", "-q", "-m", "first")
    if remote:
        git(src, "remote", "add", "origin", remote)
    return src


def make_build(tmp_path, cache=CACHE, cuda=CUDA, binary=SERVER):
    build = tmp_path / "build"
    (build / "CMakeFiles" / "3.31.1").mkdir(parents=True)
    (build / "CMakeCache.txt").write_text(cache, encoding="utf-8")
    (build / "CMakeFiles" / "3.31.1" / "CMakeCXXCompiler.cmake").write_text(CXX, encoding="utf-8")
    if cuda:
        (build / "CMakeFiles" / "3.31.1" / "CMakeCUDACompiler.cmake").write_text(cuda, encoding="utf-8")
    if binary:
        (build / binary).write_bytes(b"MZ")
    return build


def head(src):
    return subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="git is not installed")


# -- the writer -----------------------------------------------------------------------------------------


def test_the_writer_reads_the_cmake_cache_and_git_and_writes_a_valid_file_beside_the_binary(tmp_path):
    src, build = make_source(tmp_path), make_build(tmp_path)
    assert load_tool().main([str(build), str(src)]) == 0
    written = json.loads((build / "build.json").read_text(encoding="utf-8"))
    assert written["schema"] == "dpc.engine-build/1" and written["engine"] == "acestep.cpp"   # from origin
    assert written["source_commit"] == head(src) and written["source_dirty"] is False
    assert written["cuda_architectures"] == "120a-real" and written["cuda_version"] == "13.3"
    assert written["compiler"] == "MSVC 19.44.35228.0" and written["build_type"] == "Release"
    assert written["built_at"].endswith("Z") and str(tmp_path) not in json.dumps(written)    # no local path in it
    ok, why = eb.validate_build(written, engine="acestep.cpp")
    assert ok == written and why is None                                                      # DPC accepts what the tool writes
    assert [p.name for p in build.iterdir() if p.name.endswith(".tmp")] == []                 # atomic: no temp left


def test_a_changed_tracked_file_makes_the_source_dirty(tmp_path):
    src, build = make_source(tmp_path), make_build(tmp_path)
    (src / "main.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
    built, folder = load_tool().make_build(build, src)
    assert built["source_dirty"] is True and folder == build


def test_an_engine_name_given_on_the_command_line_wins_and_no_remote_falls_back_to_the_folder_name(tmp_path):
    src = make_source(tmp_path, remote=None)
    build = make_build(tmp_path)
    tool = load_tool()
    assert tool.make_build(build, src)[0]["engine"] == "src"
    assert tool.make_build(build, src, "yue2.cpp")[0]["engine"] == "yue2.cpp"


def test_a_build_without_cuda_records_the_cuda_fields_as_null(tmp_path):
    src = make_source(tmp_path)
    build = make_build(tmp_path, cache=CACHE.replace("GGML_CUDA:BOOL=ON", "GGML_CUDA:BOOL=OFF"), cuda=None)
    built, _ = load_tool().make_build(build, src)
    assert built["cuda_architectures"] is None and built["cuda_version"] is None and built["build_type"] == "Release"
    assert eb.validate_build(built)[0] is not None


def test_the_compiler_falls_back_to_the_name_of_the_executable_never_its_path(tmp_path):
    src = make_source(tmp_path)
    build = make_build(tmp_path)
    (build / "CMakeFiles" / "3.31.1" / "CMakeCXXCompiler.cmake").unlink()
    built, _ = load_tool().make_build(build, src)
    assert built["compiler"] == "cl.exe"


def test_the_binary_in_bin_gets_the_file_beside_it_and_a_missing_one_gets_the_build_folder(tmp_path):
    src = make_source(tmp_path)
    build = make_build(tmp_path, binary=None)
    (build / "bin").mkdir()
    server = build / "bin" / ("yue-server.exe" if sys.platform == "win32" else "yue-server")
    server.write_bytes(b"x")
    assert load_tool().make_build(build, src)[1] == build / "bin"
    server.unlink()
    assert load_tool().make_build(build, src)[1] == build


def test_the_writer_refuses_without_a_cmake_cache_or_a_git_checkout_and_writes_nothing(tmp_path, capsys):
    tool = load_tool()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert tool.main([str(empty), str(make_source(tmp_path))]) == 2 and not (empty / "build.json").exists()
    build = make_build(tmp_path)
    not_git = tmp_path / "plain"
    not_git.mkdir()
    assert tool.main([str(build), str(not_git)]) == 2 and not (build / "build.json").exists()
    assert "write_engine_build_json" in capsys.readouterr().err


def test_a_second_run_replaces_the_file_whole(tmp_path):
    src, build = make_source(tmp_path), make_build(tmp_path)
    (build / "build.json").write_text("{old", encoding="utf-8")
    assert load_tool().main([str(build), str(src)]) == 0
    assert json.loads((build / "build.json").read_text(encoding="utf-8"))["source_commit"] == head(src)


def test_the_command_line_runs_as_a_script(tmp_path):
    src, build = make_source(tmp_path), make_build(tmp_path)
    run = subprocess.run([sys.executable, str(TOOL), str(build), str(src), "--engine", "acestep.cpp"],
                         capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, run.stderr
    assert json.loads((build / "build.json").read_text(encoding="utf-8"))["engine"] == "acestep.cpp"


# -- the reader -----------------------------------------------------------------------------------------

GOOD = {"schema": "dpc.engine-build/1", "engine": "acestep.cpp", "source_commit": "973517be024ff8b6bbd50e0adc9e308ab2ed71b3",
        "source_dirty": False, "cuda_architectures": "120a-real", "cuda_version": "13.3", "compiler": "MSVC 19.44.35228.0",
        "build_type": "Release", "built_at": "2026-11-26T10:00:00Z"}


def put(folder: Path, obj, name="build.json"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return folder / SERVER


def test_a_valid_file_beside_the_binary_is_read_and_unknown_keys_are_dropped(tmp_path):
    binary = put(tmp_path, {**GOOD, "extra": "x" * 100})
    assert eb.read_engine_build(binary, engine="acestep.cpp") == {"build": GOOD}


def test_the_file_is_found_one_or_two_folders_above_a_multi_config_binary(tmp_path):
    put(tmp_path, GOOD)
    assert "build" in eb.read_engine_build(tmp_path / "bin" / "Release" / SERVER, engine="acestep.cpp")


def test_a_missing_file_gives_no_build_and_a_note_that_names_it(tmp_path):
    got = eb.read_engine_build(tmp_path / SERVER, engine="acestep.cpp")
    assert "build" not in got and "build.json" in got["build_note"] and SERVER in got["build_note"]
    assert "write_engine_build_json" in got["build_note"]


@pytest.mark.parametrize("change", [
    {"schema": "dpc.engine-build/2"}, {"engine": "yue2.cpp"}, {"source_commit": "HEAD"}, {"source_commit": "ABC1234"},
    {"source_dirty": "no"}, {"built_at": "yesterday"}, {"compiler": "C:\\VS\\cl.exe"}, {"compiler": "/usr/bin/c++"},
    {"cuda_architectures": "x" * 80}, {"build_type": "Rel ease"}, {"cuda_version": ["13"]}])
def test_a_file_that_does_not_validate_gives_no_build_and_a_note_naming_the_file(tmp_path, change):
    binary = put(tmp_path, {**GOOD, **change})
    got = eb.read_engine_build(binary, engine="acestep.cpp")
    assert "build" not in got and "build.json" in got["build_note"] and "ignored" in got["build_note"]


@pytest.mark.parametrize("text", ["{not json", "[]", "null", '{"schema": "dpc.engine-build/1"}', "\u00ff\u00fe"])
def test_garbage_gives_a_note_not_an_exception(tmp_path, text):
    got = eb.read_engine_build(put(tmp_path, text), engine="acestep.cpp")
    assert "build" not in got and "build.json" in got["build_note"]


def test_a_file_over_the_cap_is_not_loaded(tmp_path):
    got = eb.read_engine_build(put(tmp_path, json.dumps({**GOOD, "pad": "x" * 9000})), engine="acestep.cpp")
    assert "build" not in got and "over" in got["build_note"]


def test_null_cuda_fields_are_valid_for_a_cpu_build():
    cpu = {**GOOD, "cuda_architectures": None, "cuda_version": None, "compiler": None, "build_type": None}
    assert eb.validate_build(cpu)[0] == cpu


# -- the engine and the record ----------------------------------------------------------------------------


def make_engine(tmp_path):
    home = tmp_path / "home"
    binary = home / "engines" / "acestep.cpp" / "build" / SERVER
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"x")
    cfg = ace_read_config(lambda s, k, f=None: f, home)
    return AceStepServer(cfg, dpc_home=home), binary


def test_the_engine_reads_the_file_once_until_its_next_start(tmp_path):
    engine, binary = make_engine(tmp_path)
    assert "build" not in engine.build_identity() and "build.json" in engine.build_identity()["build_note"]
    put(binary.parent, GOOD)                                   # written after the first read: not seen yet
    assert "build" not in engine.build_identity()
    engine._build = None                                       # what the next start does
    assert engine.build_identity()["build"]["source_commit"] == GOOD["source_commit"]
    (binary.parent / "build.json").unlink()                    # removed while the engine runs: the cached one stands
    assert engine.build_identity()["build"]["compiler"] == "MSVC 19.44.35228.0"


def test_the_identity_of_an_engine_carries_the_build_or_the_note(tmp_path):
    engine, binary = make_engine(tmp_path)
    assert "build_note" in engine.identity(model_sha256=False)
    engine._build = None
    put(binary.parent, GOOD)
    assert engine.identity(model_sha256=False)["build"]["cuda_architectures"] == "120a-real"


def test_a_record_holds_the_validated_build_and_no_note_and_the_ids_do_not_move():
    plain = pv.finalize(acestep())[0]
    with_build = pv.finalize(acestep(identity={**ACE_IDENTITY, "build": dict(GOOD)}))[0]
    assert with_build["build"] == GOOD and "build_note" not in with_build
    assert plain["build"] == pv.NOT_STORED and "build.json" in plain["build_note"]
    for key in ("genome_sha256", "intent_sha256", "render_sha256"):
        assert with_build[key] == plain[key], key              # `build` is in no id


def test_a_record_refuses_an_unvalidated_build_object_and_says_why():
    rec = acestep(identity={**ACE_IDENTITY, "build": {**GOOD, "compiler": "C:\\Windows\\cl.exe"}})
    assert rec["build"] == pv.NOT_STORED and "not a valid dpc.engine-build/1" in rec["build_note"]
    other = acestep(identity={**ACE_IDENTITY, "build": {**GOOD, "engine": "yue2.cpp"}})
    assert other["build"] == pv.NOT_STORED and "yue2.cpp" in other["build_note"]
    assert yue2()["build"] == pv.NOT_STORED


def test_an_engine_build_note_passes_the_privacy_guard_in_a_finished_record():
    note = eb.read_engine_build(Path(SERVER), engine="acestep.cpp")["build_note"]
    rec = acestep(identity={**ACE_IDENTITY, "build_note": note})
    finished, text = pv.finalize(rec)
    assert finished["build_note"] == note and "build.json" in text
