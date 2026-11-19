"""`add_folder` refuses what must not be watched: a UNC path or network drive, a
filesystem root, the home folder itself, the DPC home and anything inside it (its own
`music/` subtree excepted) or around it, and more than `MAX_FOLDERS` folders.
"""

from pathlib import Path

import pytest

from dpc_client_core.music import library as library_module
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MAX_FOLDERS, MusicLibrary, MusicLibraryError


def _library(home):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


async def _refused(lib, path):
    with pytest.raises(MusicLibraryError) as err:
        await lib.add_folder(str(path), scan=False)
    return err.value.code


@pytest.mark.asyncio
async def test_a_unc_path_is_refused_before_it_is_touched(tmp_path, monkeypatch):
    lib = _library(tmp_path / "dpc")
    await lib.open()

    def no_filesystem(*_args, **_kwargs):
        raise AssertionError("a UNC path must be refused by its spelling")

    monkeypatch.setattr(lib, "_check_watchable", no_filesystem)
    try:
        codes = [await _refused(lib, p) for p in (r"\\server\share\Music", "\\\\?\\UNC\\server\\share")]
    finally:
        await lib.stop()
    assert codes == ["network_path", "network_path"]
    assert lib.list_folders() == []


@pytest.mark.asyncio
async def test_a_network_drive_is_refused(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    monkeypatch.setattr(library_module, "_is_network_drive", lambda p: True)
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        code = await _refused(lib, music)
    finally:
        await lib.stop()
    assert code == "network_path"


def test_the_local_temp_folder_is_not_a_network_drive(tmp_path):
    assert library_module._is_network_drive(Path(tmp_path)) is False


def test_a_linux_network_mount_is_read_from_the_mount_table(monkeypatch):
    table = (
        "/dev/sda1 / ext4 rw 0 0\n"
        "//nas/music /mnt/nas\\040music cifs rw 0 0\n"
        "server:/export /srv/nfs nfs4 rw 0 0\n"
    )

    class _Table:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return table

    monkeypatch.setattr(library_module, "open", _Table, raising=False)
    assert library_module._mount_fs_type("/mnt/nas music/Albums") == "cifs"
    assert library_module._mount_fs_type("/srv/nfs") == "nfs4"
    assert library_module._mount_fs_type("/srv/nfsother") == "ext4"
    assert library_module._mount_fs_type("/home/me/Music") == "ext4"


@pytest.mark.asyncio
async def test_a_filesystem_root_and_the_home_folder_are_refused(tmp_path):
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        root_code = await _refused(lib, Path(tmp_path.anchor))
        home_code = await _refused(lib, Path.home())
        home_tilde = await _refused(lib, "~")
    finally:
        await lib.stop()
    assert root_code == home_code == home_tilde == "protected_folder"


@pytest.mark.asyncio
async def test_the_dpc_home_is_refused_but_its_own_music_subfolders_are_not(tmp_path):
    home = tmp_path / "dpc"
    (home / "conversations" / "peer").mkdir(parents=True)
    (home / "music" / "files").mkdir(parents=True)
    lib = _library(home)
    await lib.open()
    try:
        codes = [await _refused(lib, p) for p in (home, home / "conversations" / "peer", tmp_path)]
        allowed = await lib.add_folder(str(home / "music" / "files"), scan=False)
    finally:
        await lib.stop()
    assert codes == ["protected_folder"] * 3
    assert allowed["path"] == str(home / "music" / "files")


@pytest.mark.asyncio
async def test_more_than_the_cap_of_watched_folders_is_refused(tmp_path):
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        for i in range(MAX_FOLDERS):
            folder = tmp_path / f"M{i:02d}"
            folder.mkdir()
            await lib.add_folder(str(folder), scan=False)
        extra = tmp_path / "one-too-many"
        extra.mkdir()
        code = await _refused(lib, extra)
    finally:
        await lib.stop()
    assert MAX_FOLDERS == 32
    assert code == "too_many_folders"
    assert len(lib.list_folders()) == MAX_FOLDERS
