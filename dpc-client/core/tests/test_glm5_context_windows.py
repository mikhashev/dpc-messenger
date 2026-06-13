"""GLM-5 series context windows are registered as conservative fallbacks."""

from dpc_client_core.llm_manager import MODEL_CONTEXT_WINDOWS


def test_glm5_context_windows_present():
    for model in ("glm-5", "glm-5.1", "glm-5.2", "glm-5-turbo"):
        assert model in MODEL_CONTEXT_WINDOWS, f"{model} missing from MODEL_CONTEXT_WINDOWS"
        assert MODEL_CONTEXT_WINDOWS[model] >= 200000
