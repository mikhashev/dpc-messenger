"""`prepare_music_lyrics` (music/commands.py): the read-only lyrics preparation on the
`local_api` door. The dictionary is a tiny fixture under the model files folder the settings name."""

import asyncio
from pathlib import Path

import pytest

from dpc_client_core import local_api
from dpc_client_core.music import lyrics_prep as lp
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from tests.test_lyrics_prep_counts_syllables_flags_meter_audits_marks_and_resolves_stress_from_a_dictionary import (
    A, build_dictionary)


class _Settings:
    def __init__(self, home, models=None):
        self.dpc_home_dir = home
        self._models = models

    def get_model_files_dir(self):
        return self._models if self._models is not None else self.dpc_home_dir / "models"

    def get(self, section, key, fallback=None):
        return fallback


class _Service(MusicCommandsMixin):
    def __init__(self, home, models=None):
        self.settings = _Settings(home, models)


@pytest.fixture(autouse=True)
def _close_dictionaries():
    yield
    lp.close_dictionaries()


def install_dictionary(models: Path) -> Path:
    target = lp.default_dictionary_path(models)
    target.parent.mkdir(parents=True, exist_ok=True)
    return build_dictionary(target)


def test_the_command_is_on_the_door_and_a_coroutine():
    assert "prepare_music_lyrics" in MUSIC_COMMANDS and "prepare_music_lyrics" in local_api.ALLOWED_COMMANDS
    assert asyncio.iscoroutinefunction(MusicCommandsMixin.prepare_music_lyrics)
    assert getattr(MusicCommandsMixin.prepare_music_lyrics, "dpc_slow", False)


@pytest.mark.asyncio
async def test_it_reads_the_dictionary_under_the_model_files_folder_and_answers_the_report(tmp_path):
    install_dictionary(tmp_path / "models")
    service = _Service(tmp_path)
    text = f"[verse]\nТысячи огонь\nв замке и дома"
    answer = await service.prepare_music_lyrics(text=text, language="auto", engine="yue2")
    assert answer["status"] == "ok"
    report = answer["report"]
    assert report["dictionary"]["state"] == "ok" and len(report["dictionary"]["sha256"]) == 64
    assert report["marked_text"].startswith(f"[verse]\nТы{A}сячи ого{A}нь")
    assert report["records"]["marked"]["engine_text"] == "marked"
    # nothing is stored: no file appeared under the home besides the dictionary
    assert sorted(p.name for p in tmp_path.iterdir()) == ["models"]


@pytest.mark.asyncio
async def test_a_pick_for_a_homograph_comes_back_in_the_marked_text(tmp_path):
    install_dictionary(tmp_path / "models")
    answer = await _Service(tmp_path).prepare_music_lyrics(
        text="и дома", engine="yue2", choices={"1:2": f"до{A}ма"})
    assert answer["report"]["marked_text"] == f"и до{A}ма"
    assert answer["report"]["summary"]["marks_chosen"] == 1


@pytest.mark.asyncio
async def test_without_the_dictionary_it_still_answers_the_meter_and_says_the_dictionary_is_missing(tmp_path):
    answer = await _Service(tmp_path).prepare_music_lyrics(text="[verse]\nдорога домой\nдорога домой\nдорога домой\nа")
    assert answer["status"] == "ok"
    report = answer["report"]
    assert report["dictionary"]["state"] == "missing"
    assert report["sections"][0]["checked"] is True
    assert report["summary"]["pronunciation"] == "dictionary_missing"


@pytest.mark.asyncio
async def test_a_stand_in_settings_without_the_model_folder_getter_falls_back_to_the_dpc_home(tmp_path):
    install_dictionary(tmp_path / "models")

    class Bare:
        dpc_home_dir = tmp_path

    service = MusicCommandsMixin()
    service.settings = Bare()
    answer = await service.prepare_music_lyrics(text="огонь")
    assert answer["report"]["dictionary"]["state"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,code", [
    ({"text": None}, "invalid_text"),
    ({"text": 5}, "invalid_text"),
    ({"text": "x" * 50001}, "text_too_long"),
    ({"text": "a", "language": 7}, "invalid_language"),
    ({"text": "a", "choices": ["1:0"]}, "invalid_choices"),
    ({"text": "a", "engine": 3}, "invalid_engine"),
])
async def test_a_bad_argument_is_a_mapped_error_not_a_traceback(tmp_path, kwargs, code):
    answer = await _Service(tmp_path).prepare_music_lyrics(**kwargs)
    assert answer["status"] == "error" and answer["code"] == code


REAL = Path.home() / ".dpc" / "models" / "lyrics" / "stress" / "ru" / "forms.sqlite"


@pytest.mark.skipif(not REAL.is_file(), reason="the Zaliznyak dictionary is not downloaded on this machine")
def test_the_real_dictionary_resolves_common_words_and_names_the_homographs():
    d, state, _ = lp.open_dictionary(REAL)
    assert state == "ok"
    r = lp.prepare_lyrics("Тысячи умов, замок и любовь", "ru", dictionary=d, engine="yue2")
    w = {x["text"]: x for x in r["lines"][0]["words"]}
    assert w["любовь"]["mark"] == f"любо{A}вь" and w["умов"]["mark"] == f"умо{A}в"
    assert w["замок"]["status"] == "ambiguous" and set(w["замок"]["options"]) == {f"за{A}мок", f"замо{A}к"}
    info = r["dictionary"]
    assert info["licence"].startswith("CC BY-NC") and len(info["sha256"]) == 64
