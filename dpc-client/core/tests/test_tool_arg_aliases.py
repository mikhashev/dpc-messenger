"""A model reaching for `file_path=` means `path=`; that should not cost a round."""
import pytest

from dpc_client_core.dpc_agent.tools.registry import _resolve_arg_aliases


def _takes_path(ctx, path, offset=None):
    return path


def _takes_both(ctx, path=None, file_path=None):
    return path, file_path


def _takes_kwargs(ctx, **kwargs):
    return kwargs


def test_alias_is_renamed_onto_the_real_parameter():
    out = _resolve_arg_aliases(_takes_path, {"file_path": "notes.md"}, "read_file")
    assert out == {"path": "notes.md"}


def test_filepath_variant_is_also_accepted():
    out = _resolve_arg_aliases(_takes_path, {"filepath": "notes.md"}, "read_file")
    assert out == {"path": "notes.md"}


def test_canonical_name_wins_when_both_are_given():
    args = {"path": "real.md", "file_path": "other.md"}
    assert _resolve_arg_aliases(_takes_path, args, "read_file") == args


def test_handler_that_declares_the_alias_is_left_alone():
    args = {"file_path": "notes.md"}
    assert _resolve_arg_aliases(_takes_both, args, "custom") == args


def test_handler_taking_kwargs_is_left_alone():
    args = {"file_path": "notes.md"}
    assert _resolve_arg_aliases(_takes_kwargs, args, "custom") == args


def test_unrelated_arguments_untouched():
    args = {"path": "notes.md", "offset": 10}
    assert _resolve_arg_aliases(_takes_path, args, "read_file") == args


def test_input_is_not_mutated():
    args = {"file_path": "notes.md"}
    _resolve_arg_aliases(_takes_path, args, "read_file")
    assert args == {"file_path": "notes.md"}


def test_alias_for_a_parameter_the_handler_lacks_is_left_to_fail():
    """A tool with no `path` should still raise, not silently absorb the argument."""
    def _no_path(ctx, query):
        return query

    args = {"file_path": "notes.md"}
    assert _resolve_arg_aliases(_no_path, args, "search") == args
