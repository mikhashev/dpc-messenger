"""Write `build.json` next to a freshly built music engine, so DPC can record the build in each take.

    python tools/music/write_engine_build_json.py <build_dir> <source_dir> [--engine NAME]

Run it after `cmake --build`. `<build_dir>` is the CMake build folder (it holds `CMakeCache.txt`),
`<source_dir>` the engine's git checkout. The file is `dpc.engine-build/1` (read and validated by
`dpc_client_core/music/engine_build.py`; the record is `ai-studio/experiments/music-player/docs/
PROVENANCE.md`):

    engine, source_commit, source_dirty, cuda_architectures, cuda_version, compiler, build_type, built_at

- `source_commit` is `git -C <source_dir> rev-parse HEAD`; `source_dirty` is true when
  `git -C <source_dir> status --porcelain` prints anything.
- `cuda_architectures` and `build_type` are `CMAKE_CUDA_ARCHITECTURES` and `CMAKE_BUILD_TYPE` of
  `CMakeCache.txt`; `compiler` and `cuda_version` come from the files CMake wrote when it configured
  (`CMakeFiles/<version>/CMakeCXXCompiler.cmake`, `CMakeCUDACompiler.cmake`): a name and a version, never a
  path. A build without CUDA (`GGML_CUDA` off or no CUDA compiler) records the CUDA fields as null.
- `engine` is `--engine`, else the repository name of `origin` (`acestep.cpp`, `yue2.cpp`), else the source
  folder's name.
- `built_at` is the time of the engine's server binary (`ace-server` or `yue-server`) when it is found,
  else now; UTC.
- The file goes beside the server binary (the build folder, or its `bin/` or `Release/`), written to a
  temporary name and moved over any old one, so a reader never sees half of it.

Standard library only; works on Windows, Linux and macOS. Exit status 0 when written, 2 when it could not
be (no `CMakeCache.txt`, no git, a value that is not safe to record); nothing is guessed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

SCHEMA = "dpc.engine-build/1"
FILE_NAME = "build.json"
SERVER_BINARIES = ("ace-server", "yue-server")


class BuildFileError(Exception):
    pass


def parse_cmake_cache(path: Path) -> Dict[str, str]:
    """`{NAME: value}` of a `CMakeCache.txt` (`NAME:TYPE=value` lines; comments and blanks skipped)."""
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BuildFileError(f"cannot read {path.name} in the build folder: {exc.strerror or exc}") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("//", "#")) or "=" not in line:
            continue
        head, _, value = line.partition("=")
        name = head.partition(":")[0].strip()
        if name:
            out[name] = value.strip()
    return out


def _is_on(value: Optional[str]) -> bool:
    return (value or "").strip().upper() in ("ON", "TRUE", "1", "YES", "Y")


def _cmake_set(build_dir: Path, file_name: str, *variables: str) -> Optional[str]:
    """The first non-empty `set(<variable> "value")` of `CMakeFiles/<cmake version>/<file_name>`."""
    for path in sorted((build_dir / "CMakeFiles").glob(f"*/{file_name}")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for var in variables:
            m = re.search(r'^\s*set\(\s*' + re.escape(var) + r'\s+"([^"]*)"\s*\)', text, re.MULTILINE)
            if m and m.group(1).strip():
                return m.group(1).strip()
    return None


def _compiler(build_dir: Path, cache: Dict[str, str]) -> Optional[str]:
    ident = _cmake_set(build_dir, "CMakeCXXCompiler.cmake", "CMAKE_CXX_COMPILER_ID")
    version = _cmake_set(build_dir, "CMakeCXXCompiler.cmake", "CMAKE_CXX_COMPILER_VERSION")
    if ident:
        return f"{ident} {version}" if version else ident
    exe = cache.get("CMAKE_CXX_COMPILER")
    if exe:
        return re.split(r"[\\/]", exe.strip())[-1] or None   # the name only: a path stays on this machine
    return None


def _cuda(build_dir: Path, cache: Dict[str, str]) -> tuple:
    """`(architectures, toolkit version)`, both None for a build without CUDA."""
    has_cuda = _is_on(cache.get("GGML_CUDA")) or _cmake_set(build_dir, "CMakeCUDACompiler.cmake",
                                                            "CMAKE_CUDA_COMPILER") is not None
    if "GGML_CUDA" in cache and not _is_on(cache.get("GGML_CUDA")):
        has_cuda = False
    if not has_cuda:
        return None, None
    arch = cache.get("CMAKE_CUDA_ARCHITECTURES") or _cmake_set(build_dir, "CMakeCUDACompiler.cmake",
                                                                "CMAKE_CUDA_ARCHITECTURES_DEFAULT")
    version = _cmake_set(build_dir, "CMakeCUDACompiler.cmake", "CMAKE_CUDA_COMPILER_TOOLKIT_VERSION",
                         "CMAKE_CUDA_COMPILER_VERSION")
    return (arch or None), version


def _git(source_dir: Path, *args: str) -> str:
    try:
        run = subprocess.run(["git", "-C", str(source_dir), *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=60)
    except FileNotFoundError:
        raise BuildFileError("git is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise BuildFileError(f"git {args[0]} timed out") from None
    if run.returncode != 0:
        raise BuildFileError(f"git {' '.join(args)} failed in the source folder: {(run.stderr or '').strip()[:200]}")
    return run.stdout


def _engine_name(source_dir: Path, given: Optional[str]) -> str:
    if given:
        return given
    try:
        url = _git(source_dir, "remote", "get-url", "origin").strip()
    except BuildFileError:
        url = ""
    name = re.split(r"[\\/:]", url.rstrip("/\\"))[-1] if url else ""
    name = name[:-4] if name.endswith(".git") else name
    return name or source_dir.resolve().name


def _server_binary(build_dir: Path) -> Optional[Path]:
    suffixes = (".exe", "") if sys.platform == "win32" else ("",)
    for folder in (build_dir, build_dir / "bin", build_dir / "Release", build_dir / "bin" / "Release"):
        for stem in SERVER_BINARIES:
            for suffix in suffixes:
                p = folder / (stem + suffix)
                if p.is_file():
                    return p
    return None


def make_build(build_dir: Path, source_dir: Path, engine: Optional[str] = None) -> tuple:
    """`(build object, folder to write it in)`. Raises `BuildFileError`."""
    build_dir, source_dir = Path(build_dir), Path(source_dir)
    cache = parse_cmake_cache(build_dir / "CMakeCache.txt")
    commit = _git(source_dir, "rev-parse", "HEAD").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        raise BuildFileError("git rev-parse HEAD did not print a commit id")
    dirty = bool(_git(source_dir, "status", "--porcelain").strip())
    arch, cuda_version = _cuda(build_dir, cache)
    binary = _server_binary(build_dir)
    when = datetime.fromtimestamp(binary.stat().st_mtime, timezone.utc) if binary else datetime.now(timezone.utc)
    build = {
        "schema": SCHEMA,
        "engine": _engine_name(source_dir, engine),
        "source_commit": commit,
        "source_dirty": dirty,
        "cuda_architectures": arch,
        "cuda_version": cuda_version,
        "compiler": _compiler(build_dir, cache),
        "build_type": cache.get("CMAKE_BUILD_TYPE") or None,
        "built_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return build, (binary.parent if binary else build_dir)


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("build_dir", type=Path, help="the CMake build folder (holds CMakeCache.txt)")
    ap.add_argument("source_dir", type=Path, help="the engine's git checkout")
    ap.add_argument("--engine", help="engine id for the file (default: the repository name of origin)")
    args = ap.parse_args(argv)
    try:
        build, folder = make_build(args.build_dir, args.source_dir, args.engine)
        target = folder / FILE_NAME
        write_atomic(target, json.dumps(build, indent=2) + "\n")
    except (BuildFileError, OSError) as exc:
        print(f"write_engine_build_json: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {target}")
    print(json.dumps(build, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
