"""
Tests for the agent vision tool (dpc_agent/tools/vision.py).

Cover the vision-provider call, path/type/size gating, prompt selection
(question vs default description), the inline JSON output contract, and
failure paths. The LLM manager is faked — these tests must not load a model.
"""

import base64
import json

import pytest

from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.dpc_agent.tools.vision import get_tools, describe_image


class FakeLLMManager:
    def __init__(self, text="A blue brain-with-circuits icon.", model="qwen3-vl:8b"):
        self.calls = []
        self._text = text
        self._model = model

    async def query(self, prompt, provider_alias=None, return_metadata=False, images=None, **kwargs):
        self.calls.append({
            "prompt": prompt,
            "provider_alias": provider_alias,
            "images": images,
            "return_metadata": return_metadata,
        })
        if return_metadata:
            return {"response": self._text, "model": self._model}
        return self._text


class FakeService:
    def __init__(self, llm_manager):
        self.llm_manager = llm_manager


@pytest.fixture
def agent_root(tmp_path):
    root = tmp_path / "agent_001"
    root.mkdir()
    return root


@pytest.fixture
def image_file(agent_root):
    path = agent_root / "render.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")
    return path


def make_ctx(agent_root, llm_manager):
    return ToolContext(agent_root=agent_root, dpc_service=FakeService(llm_manager))


class TestOutputContract:
    @pytest.mark.asyncio
    async def test_returns_description_inline_as_json(self, agent_root, image_file):
        llm = FakeLLMManager(text="A red arrow pointing up.")
        ctx = make_ctx(agent_root, llm)

        raw = await describe_image(ctx, "render.png")
        result = json.loads(raw)

        assert result["description"] == "A red arrow pointing up."
        assert result["model"] == "qwen3-vl:8b"
        assert result["question"] is None
        assert result["image_path"].endswith("render.png")

    @pytest.mark.asyncio
    async def test_image_passed_as_base64_with_mime(self, agent_root, image_file):
        llm = FakeLLMManager()
        ctx = make_ctx(agent_root, llm)

        await describe_image(ctx, "render.png")

        img = llm.calls[0]["images"][0]
        assert img["mime_type"] == "image/png"
        assert base64.b64decode(img["base64"]) == image_file.read_bytes()
        assert llm.calls[0]["return_metadata"] is True


class TestPromptSelection:
    @pytest.mark.asyncio
    async def test_question_is_used_in_prompt(self, agent_root, image_file):
        llm = FakeLLMManager()
        ctx = make_ctx(agent_root, llm)

        result = json.loads(await describe_image(ctx, "render.png", question="Is it symmetric?"))

        assert "Is it symmetric?" in llm.calls[0]["prompt"]
        assert result["question"] == "Is it symmetric?"

    @pytest.mark.asyncio
    async def test_default_prompt_when_no_question(self, agent_root, image_file):
        llm = FakeLLMManager()
        ctx = make_ctx(agent_root, llm)

        await describe_image(ctx, "render.png")

        assert "comprehensive description" in llm.calls[0]["prompt"].lower()


class TestProviderSelection:
    @pytest.mark.asyncio
    async def test_model_forwarded_as_provider_alias(self, agent_root, image_file):
        llm = FakeLLMManager()
        ctx = make_ctx(agent_root, llm)

        await describe_image(ctx, "render.png", model="qwen3-vl:8b")

        assert llm.calls[0]["provider_alias"] == "qwen3-vl:8b"

    @pytest.mark.asyncio
    async def test_default_auto_selects_vision_provider(self, agent_root, image_file):
        llm = FakeLLMManager()
        ctx = make_ctx(agent_root, llm)

        await describe_image(ctx, "render.png")

        assert llm.calls[0]["provider_alias"] is None


class TestGating:
    @pytest.mark.asyncio
    async def test_missing_file(self, agent_root):
        result = await describe_image(make_ctx(agent_root, FakeLLMManager()), "nope.png")
        assert "File not found" in result

    @pytest.mark.asyncio
    async def test_directory_rejected(self, agent_root):
        (agent_root / "adir").mkdir()
        result = await describe_image(make_ctx(agent_root, FakeLLMManager()), "adir")
        assert "Not a file" in result

    @pytest.mark.asyncio
    async def test_absolute_path_outside_sandbox_denied(self, agent_root, tmp_path):
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"fake")
        result = await describe_image(make_ctx(agent_root, FakeLLMManager()), str(outside))
        assert "Access denied" in result

    @pytest.mark.asyncio
    async def test_unsupported_extension_rejected(self, agent_root):
        (agent_root / "doc.pdf").write_bytes(b"%PDF-fake")
        result = await describe_image(make_ctx(agent_root, FakeLLMManager()), "doc.pdf")
        assert result.startswith("⚠️")
        assert "Unsupported" in result

    @pytest.mark.asyncio
    async def test_oversize_image_rejected(self, agent_root):
        big = agent_root / "big.png"
        big.write_bytes(b"\x89PNG" + b"0" * (21 * 1024 * 1024))
        result = await describe_image(make_ctx(agent_root, FakeLLMManager()), "big.png")
        assert result.startswith("⚠️")
        assert "too large" in result


class TestFailures:
    @pytest.mark.asyncio
    async def test_no_llm_manager(self, agent_root, image_file):
        ctx = ToolContext(agent_root=agent_root, dpc_service=None)
        result = await describe_image(ctx, "render.png")
        assert result.startswith("⚠️")
        assert "Vision unavailable" in result

    @pytest.mark.asyncio
    async def test_vision_query_error_surfaced(self, agent_root, image_file):
        llm = FakeLLMManager()

        async def boom(*a, **k):
            raise RuntimeError("cuda oom")

        llm.query = boom
        result = await describe_image(make_ctx(agent_root, llm), "render.png")
        assert result.startswith("⚠️")
        assert "cuda oom" in result

    @pytest.mark.asyncio
    async def test_empty_description_reported(self, agent_root, image_file):
        llm = FakeLLMManager(text="   ")
        result = await describe_image(make_ctx(agent_root, llm), "render.png")
        assert result.startswith("⚠️")
        assert "no description" in result


class TestRegistration:
    def test_tool_is_opt_in(self):
        entry = get_tools()[0]
        assert entry.name == "describe_image"
        assert entry.default_enabled is False

    def test_schema_requires_image_path_only(self):
        schema = get_tools()[0].schema
        assert schema["parameters"]["required"] == ["image_path"]
        assert set(schema["parameters"]["properties"]) == {"image_path", "question", "model"}
