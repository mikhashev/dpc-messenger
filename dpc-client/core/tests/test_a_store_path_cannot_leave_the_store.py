"""A conversation id becomes a directory name, and one of them arrives on the wire.

`GROUP_HISTORY_RESPONSE` carries the group id it wants merged, and that id
travelled to `resolve_store_dir` unexamined: shown on 2026-08-28 in a
substituted HOME, `group-../../../escaped` resolved outside `conversations/`
and `merge_history` wrote `history.json` and `.chain_meta.json` there, parent
directories and all. An absolute id needs no `..` at all — `Path.__truediv__`
discards the base.

The roster gate refuses such an id one layer up, because no group is called
that. This is the second layer, where the path is built, so a future caller
does not have to remember.
"""
from pathlib import Path

import pytest

from dpc_client_core.conversation_paths import (
    is_safe_conversation_id,
    resolve_store_dir,
)

GROUP = "group-970e5c7006a0"
PEER = "dpc-node-" + "a" * 32


class TestWhatMayBecomeAFolderName:
    @pytest.mark.parametrize("conversation_id", [
        GROUP, PEER, "agent_iris_63f1b6bf", "telegram-429727247", "local_ai",
    ])
    def test_the_ids_this_product_actually_makes(self, conversation_id):
        assert is_safe_conversation_id(conversation_id)

    @pytest.mark.parametrize("conversation_id", [
        "group-../../../escaped",
        "group-..",
        "..",
        ".",
        "../secrets",
        "a/b",
        "a\\b",
        "C:/Windows/Temp/evil",
        "/etc/passwd",
        "",
        None,
    ])
    def test_and_the_ones_that_would_leave(self, conversation_id):
        assert not is_safe_conversation_id(conversation_id)


class TestTheStorePath:
    def test_a_real_id_lands_under_the_base(self, tmp_path):
        chosen = resolve_store_dir(tmp_path, GROUP, "work")

        assert chosen == tmp_path / f"{GROUP}-work"
        assert tmp_path.resolve() in chosen.resolve().parents

    def test_a_traversal_is_refused_rather_than_resolved(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_store_dir(tmp_path, "group-../../../escaped")

    def test_an_absolute_id_is_refused_even_though_it_needs_no_dots(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_store_dir(tmp_path, str(tmp_path.parent / "elsewhere"))

    def test_an_existing_store_still_wins(self, tmp_path):
        """The rule this module exists for must survive the new guard."""
        existing = tmp_path / GROUP
        existing.mkdir()
        (existing / "history.json").write_text('{"messages": []}', encoding="utf-8")

        assert resolve_store_dir(tmp_path, GROUP, "a-new-name") == existing

    def test_nothing_is_created_by_asking(self, tmp_path):
        resolve_store_dir(tmp_path, GROUP, "work")

        assert list(tmp_path.iterdir()) == []
