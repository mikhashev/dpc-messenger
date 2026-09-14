"""numba's JIT pipeline flooded the owner's log, not the code.

On 2026-09-14, one transcription (whisper_provider.py calling
librosa.load()) pushed the log from 21 900 to 61 500 lines in about 40
minutes: numba's bytecode/SSA/type-inference dumps at DEBUG, with no root
logger override for numba anywhere. The 10MB rotation buried real lines
under it. run_service.py's setup_logging() quiets a short list of
known-noisy third-party loggers to WARNING regardless of the root level,
applied before the user's own `[logging.modules]` overrides in
config.ini so an explicit override still wins.
"""

import logging

import pytest

from dpc_client_core.settings import Settings
import run_service


QUIETED = run_service.QUIET_THIRD_PARTY_LOGGERS


@pytest.fixture
def clean_logging_state():
    """setup_logging() mutates global logger state; restore it after."""
    root = logging.getLogger()
    saved_root_level = root.level
    saved_root_handlers = list(root.handlers)
    saved_child_levels = {
        name: logging.getLogger(name).level
        for name in (*QUIETED, "dpc_client_core.p2p_manager")
    }
    yield
    for handler in list(root.handlers):
        if handler not in saved_root_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(saved_root_level)
    for name, level in saved_child_levels.items():
        logging.getLogger(name).setLevel(level)


def _settings(tmp_path, extra_logging_modules=""):
    config_text = (
        "[logging]\n"
        "level = DEBUG\n"
        "console = false\n"
        f"file = {(tmp_path / 'dpc-client.log').as_posix()}\n"
        "max_bytes = 10485760\n"
        "backup_count = 1\n"
    )
    if extra_logging_modules:
        config_text += "\n[logging.modules]\n" + extra_logging_modules
    (tmp_path / "config.ini").write_text(config_text, encoding="utf-8")
    return Settings(tmp_path)


class TestTheQuietList:

    def test_a_quieted_logger_is_raised_above_debug(self, tmp_path, clean_logging_state):
        settings = _settings(tmp_path)
        run_service.setup_logging(settings)

        for name in QUIETED:
            effective = logging.getLogger(name).getEffectiveLevel()
            assert effective >= logging.WARNING, (
                f"{name} effective level is {logging.getLevelName(effective)}, "
                "still low enough to emit DEBUG"
            )

    def test_a_child_of_a_quieted_logger_is_quieted_too(self, tmp_path, clean_logging_state):
        # This is the exact shape of the incident: numba.core.byteflow has no
        # level of its own and must inherit numba's WARNING.
        settings = _settings(tmp_path)
        run_service.setup_logging(settings)

        child = logging.getLogger("numba.core.byteflow")
        assert child.level == logging.NOTSET, "test assumes the child has no explicit level"
        assert child.getEffectiveLevel() >= logging.WARNING

    def test_a_user_override_in_config_ini_still_wins(self, tmp_path, clean_logging_state):
        settings = _settings(tmp_path, extra_logging_modules="numba = DEBUG\n")
        run_service.setup_logging(settings)

        assert logging.getLogger("numba").getEffectiveLevel() == logging.DEBUG

    def test_project_loggers_are_not_touched_by_the_quiet_list(self, tmp_path, clean_logging_state):
        settings = _settings(tmp_path)
        run_service.setup_logging(settings)

        # The quiet list must not reach into dpc_client_core.* — only the
        # root level (here DEBUG, from config) governs it.
        assert (
            logging.getLogger("dpc_client_core.p2p_manager").getEffectiveLevel()
            == logging.DEBUG
        )
