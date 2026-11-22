"""The suite must not take a CUDA context: the card belongs to the live local engines."""
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DPC_TESTS_ALLOW_GPU") == "1",
    reason="DPC_TESTS_ALLOW_GPU=1 opts this run in to the GPU",
)


def test_cuda_is_hidden_from_the_test_process():
    assert os.environ["CUDA_VISIBLE_DEVICES"] == "-1"


def test_torch_sees_no_cuda_device():
    torch = pytest.importorskip("torch")
    assert torch.cuda.is_available() is False
