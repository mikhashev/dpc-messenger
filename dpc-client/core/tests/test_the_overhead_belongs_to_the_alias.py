"""What a context costs beyond weights and KV is a property of the model.

The admission check adds a fixed figure to weights and attention-KV and
refuses a KV rung that does not fit. That figure was measured on one model at
one context size, and every term in it — compute buffers, the MTP draft
context, the hybrid blocks' recurrent state, the CUDA context — is
model-shaped. An alias serving something else now carries its own.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.managers.llama_server_supervisor import (
    _DEFAULT_OVERHEAD_MIB,
    LlamaServerSupervisor,
)


def _supervisor(**over):
    config = {"gguf_path": "C:/nowhere/model.gguf", "n_ctx": 4096}
    config.update(over)
    return LlamaServerSupervisor("test-alias", config)


class TestWhichNumberIsInForce:
    def test_an_alias_that_says_nothing_gets_the_measured_default(self):
        assert _supervisor()._overhead_mib() == _DEFAULT_OVERHEAD_MIB

    def test_an_alias_with_its_own_figure_is_believed(self):
        assert _supervisor(vram_overhead_mib=2048)._overhead_mib() == 2048

    def test_a_figure_that_is_not_a_number_falls_back_rather_than_raising(self):
        assert _supervisor(vram_overhead_mib="plenty")._overhead_mib() == _DEFAULT_OVERHEAD_MIB

    def test_zero_is_a_choice_and_not_an_absence(self):
        assert _supervisor(vram_overhead_mib=0)._overhead_mib() == 0
