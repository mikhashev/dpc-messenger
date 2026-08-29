"""What the forms may say about a model's effort words.

The panel and the provider form used to state the words in prose, which read
as if something had asked the model and had not. The words now travel from
the model file, and only when they were read from it: a fallback table must
never reach a screen wearing the model's name.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.service import CoreService


def _effort_words(config):
    return CoreService._effort_words_by_alias(None, config)


class TestTheMapBesideTheConfig:
    def test_an_alias_with_no_model_file_is_absent(self):
        config = {"providers": [{"alias": "deepseek_flash", "type": "deepseek"}]}

        assert _effort_words(config) == {}

    def test_a_missing_file_is_absent_rather_than_guessed(self, tmp_path):
        config = {"providers": [{"alias": "gone", "gguf_path": str(tmp_path / "no.gguf")}]}

        assert _effort_words(config) == {}

    def test_the_map_never_lands_inside_the_config_the_editor_saves_back(self, tmp_path):
        config = {"providers": [{"alias": "a", "gguf_path": str(tmp_path / "x.gguf")}]}
        before = json.dumps(config, sort_keys=True)

        _effort_words(config)

        assert json.dumps(config, sort_keys=True) == before
