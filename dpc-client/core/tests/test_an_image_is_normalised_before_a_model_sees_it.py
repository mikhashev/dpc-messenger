"""An image is normalised once, in one place, before a model sees it.

Step 3 of AN-IMAGE-REACHES-THE-VISION-MODEL-THROUGH-FOUR-DOORS-...: EXIF
orientation first, then the mode traps (CMYK, 16-bit, palette), an animated
image cut to frame 1 with a note, a downscale only when the provider names a
pixel limit, the byte cap judged on what would be sent. An unknown limit never
downscales and an ordinary picture leaves byte-identical.

Pillow only, no engine: the llama-server provider's limit is read from a
hand-written GGUF header in tmp_path.
"""

import asyncio
import json
import struct
import threading
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from dpc_client_core.dpc_agent import llm_adapter as adapter_module
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.tools import document as D
from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.providers import AIProvider
from dpc_client_core.providers.base import image_pixel_limit_of
from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider
from dpc_client_core.utils import image_utils
from dpc_client_core.utils.image_utils import (
    ImageTooLarge,
    normalise_flat_images,
    normalise_image,
    normalise_turn_images,
)


def _encode(img, **kw):
    buffer = BytesIO()
    img.save(buffer, **kw)
    return buffer.getvalue()


def _opened(data):
    return Image.open(BytesIO(data))


# ------------------------------------------------------------ the mode traps

def test_a_cmyk_jpeg_becomes_rgb():
    data = _encode(Image.new("CMYK", (40, 30), (10, 20, 30, 0)), format="JPEG")
    assert _opened(data).mode == "CMYK"
    out, mime, note = normalise_image(data, "image/jpeg")
    assert mime == "image/jpeg" and _opened(out).mode == "RGB"
    assert "CMYK" in note


def test_a_16_bit_png_becomes_8_bit_and_can_be_saved_as_a_picture():
    data = _encode(Image.new("I;16", (40, 30), 40000), format="PNG")
    out, mime, note = normalise_image(data, "image/png")
    result = _opened(out)
    assert mime == "image/png" and result.mode == "L"
    assert result.getpixel((0, 0)) == 40000 // 256
    assert "16-bit" in note


def test_a_palette_png_with_transparency_keeps_its_alpha_in_png():
    palette = Image.new("P", (8, 8), 0)
    palette.putpalette([255, 0, 0, 0, 255, 0] + [0] * 250 * 3)
    palette.putpixel((1, 1), 1)
    data = _encode(palette, format="PNG", transparency=0)
    assert _opened(data).mode == "P", "the premise: this PNG opens as a palette image"
    out, mime, note = normalise_image(data, "image/png")
    result = _opened(out)
    assert mime == "image/png" and result.mode == "RGBA"
    assert result.getpixel((0, 0))[3] == 0 and result.getpixel((1, 1)) == (0, 255, 0, 255)
    assert "palette" in note


def test_a_palette_png_without_transparency_becomes_rgb():
    palette = Image.new("P", (8, 8), 0)
    data = _encode(palette, format="PNG")
    out, _mime, note = normalise_image(data, "image/png")
    assert _opened(out).mode == "RGB" and note


def test_an_animated_gif_is_cut_to_its_first_frame_and_says_so():
    first = Image.new("RGB", (20, 20), (255, 0, 0))
    second = Image.new("RGB", (20, 20), (0, 0, 255))
    data = _encode(first, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    assert getattr(_opened(data), "n_frames", 1) == 2
    out, mime, note = normalise_image(data, "image/gif")
    result = _opened(out).convert("RGB")
    assert mime == "image/png" and getattr(_opened(out), "n_frames", 1) == 1
    r, g, b = result.getpixel((5, 5))
    assert r > 200 and b < 60, "frame 1 (red) was kept, not frame 2"
    assert "frame 1 of 2" in note


# ------------------------------------------------- orientation, limit, bytes

def _rotated_jpeg(size=(300, 100), orientation=6):
    exif = Image.Exif()
    exif[0x0112] = orientation
    return _encode(Image.new("RGB", size, (200, 100, 50)), format="JPEG", exif=exif)


def test_an_exif_rotated_jpeg_is_turned_before_the_limit_reads_its_size():
    data = _rotated_jpeg((300, 100), orientation=6)
    out, _mime, note = normalise_image(data, "image/jpeg", pixel_limit=10_000)
    result = _opened(out)
    assert result.width < result.height, "upright: taller than wide"
    assert result.width * result.height <= 10_000
    assert "EXIF" in note and "downscaled from 100x300" in note  # the swap came first


def test_an_exif_rotation_alone_swaps_the_dimensions_when_no_limit_is_known():
    out, _mime, note = normalise_image(_rotated_jpeg((300, 100)), "image/jpeg", pixel_limit=None)
    assert _opened(out).size == (100, 300) and "EXIF" in note


def test_an_unknown_limit_passes_an_ordinary_picture_byte_for_byte():
    jpeg = _encode(Image.new("RGB", (3000, 2000), (1, 2, 3)), format="JPEG")
    png = _encode(Image.new("RGB", (500, 400), (1, 2, 3)), format="PNG")
    assert normalise_image(jpeg, "image/jpeg", None) == (jpeg, "image/jpeg", None)
    assert normalise_image(png, "image/png", None) == (png, "image/png", None)


def test_a_picture_under_the_limit_is_left_as_it_is():
    jpeg = _encode(Image.new("RGB", (100, 100), (1, 2, 3)), format="JPEG")
    assert normalise_image(jpeg, "image/jpeg", 1_000_000) == (jpeg, "image/jpeg", None)


def test_a_larger_picture_is_downscaled_to_fit_and_the_note_says_to_what_and_why():
    data = _encode(Image.new("RGB", (2000, 1000), (9, 9, 9)), format="JPEG")
    out, mime, note = normalise_image(data, "image/jpeg", pixel_limit=500_000)
    result = _opened(out)
    assert mime == "image/jpeg" and result.width * result.height <= 500_000
    assert abs(result.width / result.height - 2.0) < 0.02
    assert note == (f"downscaled from 2000x1000 to {result.width}x{result.height} "
                    "to fit the route's 0.5 MP image limit")


def test_a_picture_that_cannot_be_decoded_comes_back_untouched_with_a_note():
    out, mime, note = normalise_image(b"this is not an image", "image/png", 1000)
    assert out == b"this is not an image" and mime == "image/png"
    assert "could not be normalised" in note


def test_a_truncated_picture_that_needed_a_change_comes_back_untouched_too():
    data = _encode(Image.new("CMYK", (200, 200), (1, 2, 3, 4)), format="JPEG")
    out, _mime, note = normalise_image(data[:len(data) // 2], "image/jpeg")
    assert out == data[:len(data) // 2] and "could not be normalised" in note


def test_the_byte_cap_is_judged_on_what_would_be_sent():
    noise = Image.frombytes("RGB", (600, 600), bytes(range(256)) * (600 * 600 * 3 // 256 + 1))
    data = _encode(noise, format="PNG")
    cap = len(data) - 1
    with pytest.raises(ImageTooLarge):
        normalise_image(data, "image/png", pixel_limit=None, max_bytes=cap)
    out, _mime, note = normalise_image(data, "image/png", pixel_limit=90_000, max_bytes=cap)
    assert len(out) <= cap and "downscaled" in note


# ------------------------------------------------- off the loop, and cached

def test_the_decode_runs_off_the_event_loop_and_a_repeat_is_cached(monkeypatch):
    image_utils._cache.clear()
    seen = []
    real = image_utils.normalise_image

    def spy(data, mime, pixel_limit, max_bytes):
        seen.append(threading.get_ident())
        return real(data, mime, pixel_limit, max_bytes)

    monkeypatch.setattr(image_utils, "normalise_image", spy)
    data = _encode(Image.new("RGB", (400, 400), (5, 5, 5)), format="JPEG")

    async def run():
        loop_thread = threading.get_ident()
        first = await image_utils.normalise_image_async(data, "image/jpeg", 10_000)
        again = await image_utils.normalise_image_async(data, "image/jpeg", 10_000)
        other = await image_utils.normalise_image_async(data, "image/jpeg", 20_000)
        return loop_thread, first, again, other

    loop_thread, first, again, other = asyncio.run(run())
    assert len(seen) == 2, "the same bytes and limit were decoded once, a new limit again"
    assert loop_thread not in seen
    assert first == again and first[0] != other[0]


def test_the_turn_walker_reaches_an_image_inside_a_tool_result_and_leaves_the_input_alone():
    big = _encode(Image.new("RGB", (800, 800), (7, 7, 7)), format="JPEG")
    import base64 as b64
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": b64.b64encode(big).decode()}}
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "x"}, block]}]},
        {"role": "assistant", "content": "plain text"}]
    before = json.dumps(messages)
    out, notes = asyncio.run(normalise_turn_images(messages, 40_000))
    assert json.dumps(messages) == before, "the caller's turns were mutated"
    inner = out[0]["content"][0]["content"][1]["source"]
    w, h = _opened(b64.b64decode(inner["data"])).size
    assert w * h <= 40_000 and len(notes) == 1
    assert out[1] is messages[1]


def test_the_flat_list_keeps_the_same_dicts_where_nothing_changed():
    import base64 as b64
    small = _encode(Image.new("RGB", (10, 10)), format="PNG")
    entry = {"base64": b64.b64encode(small).decode(), "mime_type": "image/png"}
    out, notes = asyncio.run(normalise_flat_images([entry], 1_000_000))
    assert out[0] is entry and notes == []


# ------------------------------------------------ the provider's pixel limit

def _gguf(path, kv):
    """A GGUF header with integer (u32) metadata and one string, no tensors."""
    def s(text):
        raw = text.encode()
        return struct.pack("<Q", len(raw)) + raw

    body = b"".join(s(k) + struct.pack("<I", 4) + struct.pack("<I", v) for k, v in kv.items())
    body = s("general.name") + struct.pack("<I", 8) + s("fake") + body
    path.write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, len(kv) + 1) + body)
    return str(path)


def _llama(tmp_path, tokens=None, header=None):
    mmproj = _gguf(tmp_path / "mmproj.gguf", header if header is not None else
                   {"clip.vision.patch_size": 16, "clip.vision.spatial_merge_size": 2})
    config = {"type": "llamacpp_server", "gguf_path": "D:/m.gguf", "mmproj": mmproj}
    if tokens is not None:
        config["image_max_tokens"] = tokens
    return LlamaServerProvider("llama.cpp", config)


def test_the_llama_provider_derives_pixels_from_tokens_and_the_projector_geometry(tmp_path):
    assert _llama(tmp_path, tokens=4082).image_pixel_limit() == 4082 * 32 * 32


def test_the_llama_provider_with_no_configured_cap_states_no_limit(tmp_path):
    assert _llama(tmp_path, tokens=None).image_pixel_limit() is None


def test_a_projector_that_names_no_merge_counts_one_and_no_patch_size_means_unknown(tmp_path):
    assert _llama(tmp_path, tokens=100, header={"clip.vision.patch_size": 14}).image_pixel_limit() == 100 * 14 * 14
    assert _llama(tmp_path, tokens=100, header={"other.key": 1}).image_pixel_limit() is None


def test_the_base_provider_does_not_know_its_limit_and_a_stand_in_is_unknown():
    assert AIProvider("a", {"type": "x", "model": "m"}).image_pixel_limit() is None
    assert image_pixel_limit_of(SimpleNamespace()) is None
    assert image_pixel_limit_of(SimpleNamespace(image_pixel_limit=lambda: 5.5)) is None
    assert image_pixel_limit_of(SimpleNamespace(image_pixel_limit=lambda: 1234)) == 1234


# ----------------------------------------------------- the hand-offs, wired

class _Seeing(AIProvider):
    def __init__(self, limit):
        super().__init__("seer", {"type": "ollama", "model": "m"})
        self._limit = limit
        self.vision_images = []
        self.tool_messages = []

    def supports_vision(self):
        return True

    def image_pixel_limit(self):
        return self._limit

    async def generate_with_vision(self, prompt, images, **kwargs):
        self.vision_images.append(images)
        return "seen"

    async def generate_with_tools(self, messages, tools, system="", on_chunk=None,
                                  conversation_id=None, **kwargs):
        self.tool_messages.append(messages)
        return {"content": "ok", "tool_calls_raw": [], "thinking": None, "usage": {}}


def _manager(tmp_path, provider):
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": []}), encoding="utf-8")
    manager = LLMManager(config_path=config)
    manager.providers = {"seer": provider}
    manager.default_provider = "seer"
    return manager


def _big_b64():
    import base64 as b64
    return b64.b64encode(_encode(Image.new("RGB", (1000, 1000), (3, 3, 3)), format="JPEG")).decode()


def test_query_hands_the_provider_the_downscaled_picture_and_returns_the_note(tmp_path):
    provider = _Seeing(limit=40_000)
    manager = _manager(tmp_path, provider)
    meta = asyncio.run(manager.query(
        "look", provider_alias="seer", return_metadata=True,
        images=[{"base64": _big_b64(), "mime_type": "image/jpeg"}]))
    import base64 as b64
    sent = _opened(b64.b64decode(provider.vision_images[0][0]["base64"]))
    assert sent.width * sent.height <= 40_000
    assert meta["image_notes"] and "downscaled from 1000x1000" in meta["image_notes"][0]


def test_query_with_an_unknown_limit_sends_the_picture_as_it_came(tmp_path):
    provider = _Seeing(limit=None)
    manager = _manager(tmp_path, provider)
    original = {"base64": _big_b64(), "mime_type": "image/jpeg"}
    meta = asyncio.run(manager.query("look", provider_alias="seer", return_metadata=True, images=[original]))
    assert provider.vision_images[0][0] is original and meta["image_notes"] == []


def test_query_messages_normalises_the_images_in_the_turns(tmp_path):
    provider = _Seeing(limit=40_000)
    manager = _manager(tmp_path, provider)
    messages = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _big_b64()}},
        {"type": "text", "text": "what is this"}]}]
    meta = asyncio.run(manager.query_messages(
        messages, tools=[{"name": "t", "description": "", "input_schema": {}}],
        provider_alias="seer", return_metadata=True))
    import base64 as b64
    sent = provider.tool_messages[0][0]["content"][0]["source"]
    w, h = _opened(b64.b64decode(sent["data"])).size
    assert w * h <= 40_000
    assert meta["image_notes"]


def test_the_agents_two_direct_paths_normalise_too():
    provider = _Seeing(limit=40_000)
    provider.alias = "seer"
    adapter = DpcLlmAdapter(SimpleNamespace(token_count_manager=None, providers={}), provider_alias="seer")
    asyncio.run(adapter._chat_with_native_vision(
        provider, [{"role": "user", "content": "look"}],
        [{"base64": _big_b64(), "mime_type": "image/jpeg"}]))
    import base64 as b64
    w, h = _opened(b64.b64decode(provider.vision_images[0][0]["base64"])).size
    assert w * h <= 40_000
    assert adapter_module.normalise_turn_images is normalise_turn_images


# ------------------------------------------- read_document: cache key, note

def test_the_page_cache_key_carries_the_pixel_limit_only_when_there_is_one(tmp_path):
    ctx = SimpleNamespace(agent_root=tmp_path)
    plain = D._cache_path(ctx, "a" * 32, 3, "m", 150)
    assert D._cache_path(ctx, "a" * 32, 3, "m", 150, None) == plain, "old entries still hold"
    capped = D._cache_path(ctx, "a" * 32, 3, "m", 150, 4_000_000)
    assert capped != plain and capped != D._cache_path(ctx, "a" * 32, 3, "m", 150, 8_000_000)


class _Eye:
    """A stand-in manager whose model limit can change between two reads."""

    def __init__(self, limit):
        self.limit, self.calls = limit, 0

    def image_pixel_limit_for(self, alias):
        return self.limit

    async def query(self, prompt=None, provider_alias=None, images=None, return_metadata=False, **kw):
        self.calls += 1
        return {"response": "text of the page", "model": "m",
                "image_notes": ["downscaled from 9x9 to 3x3 to fit the route's 0.0 MP image limit"]}


def test_a_changed_cap_does_not_serve_a_stale_page_and_the_note_reaches_the_answer(tmp_path):
    entry = {"page": 1, "route": "text", "chars": 0}
    render = lambda number, dpi: (b"png", "image/png", None, None)  # noqa: E731
    eye = _Eye(limit=4_000_000)
    ctx = SimpleNamespace(dpc_service=SimpleNamespace(llm_manager=eye), agent_root=tmp_path)
    asyncio.run(D._read_page_with_vision(ctx, render, dict(entry), "d" * 64, None, 150))
    asyncio.run(D._read_page_with_vision(ctx, render, dict(entry), "d" * 64, None, 150))
    assert eye.calls == 1, "same limit: the second read is the cache"
    eye.limit = 8_000_000
    again = dict(entry)
    asyncio.run(D._read_page_with_vision(ctx, render, again, "d" * 64, None, 150))
    assert eye.calls == 2, "a changed limit reads the page again"
    assert "downscaled from 9x9" in again["note"]
    served = dict(entry)
    asyncio.run(D._read_page_with_vision(ctx, render, served, "d" * 64, None, 150))
    assert served["cached"] is True and "downscaled from 9x9" in served["note"]


# ------------------------- decode memory is guarded by pixels, not by bytes

def _bmp_header(width, height):
    """A 54-byte 24-bit BMP header and no pixels: Image.open reads the size
    from it and nothing is decoded, so a test of 400 MP stays light."""
    return (b"BM" + struct.pack("<IHHI", 54, 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, 0, 0, 0, 0, 0))


def test_a_60_mp_jpeg_weighing_about_a_megabyte_is_downscaled_under_a_limit_and_untouched_without_one():
    data = _encode(Image.new("L", (10_000, 6_000), 128), format="JPEG")
    assert len(data) < 3 * 1024 * 1024, "the premise: 60 MP, a few MB at most"
    out, _mime, note = normalise_image(data, "image/jpeg", pixel_limit=1_000_000)
    width, height = _opened(out).size
    assert width * height <= 1_000_000 and "downscaled from 10000x6000" in note
    assert normalise_image(data, "image/jpeg", pixel_limit=None) == (data, "image/jpeg", None)


def test_an_image_declared_above_the_ceiling_is_refused_before_any_decode(monkeypatch):
    # Pillow's own bomb check is switched off, so this is our header check alone.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)
    data = _bmp_header(20_000, 20_000)  # 400 MP
    with pytest.raises(image_utils.ImageTooManyPixels, match="too many pixels"):
        normalise_image(data, "image/bmp", pixel_limit=None)
    with pytest.raises(ImageTooLarge):  # a sibling: one except covers both
        normalise_image(data, "image/bmp", pixel_limit=1_000_000)


def test_pillows_own_bomb_error_is_a_refusal_too_not_a_passthrough():
    with pytest.raises(image_utils.ImageTooManyPixels):
        normalise_image(_bmp_header(20_000, 20_000), "image/bmp")


def test_the_band_between_pillows_warning_and_the_ceiling_is_processed_quietly(recwarn):
    data = _bmp_header(12_000, 12_000)  # 144 MP: Pillow warns, we accept
    assert 89_478_485 < 12_000 * 12_000 < image_utils.MAX_DECODE_PIXELS
    assert normalise_image(data, "image/bmp", pixel_limit=None) == (data, "image/bmp", None)
    assert not [w for w in recwarn if issubclass(w.category, Image.DecompressionBombWarning)]


def test_the_hand_off_path_does_not_apply_the_door_byte_cap(tmp_path):
    # 5 MB is the [vision] cap at the door, on the incoming original. After
    # that the hand-off to a provider passes max_bytes=None: a picture the
    # door let in is never refused for its weight on its way to the model.
    import base64 as b64
    import os
    big = _encode(Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3)), format="PNG")
    assert len(big) > 5 * 1024 * 1024
    provider = _Seeing(limit=None)
    manager = _manager(tmp_path, provider)
    asyncio.run(manager.query(
        "look", provider_alias="seer", return_metadata=True,
        images=[{"base64": b64.b64encode(big).decode(), "mime_type": "image/png"}]))
    assert len(provider.vision_images) == 1
    with pytest.raises(ImageTooLarge):  # the cap exists, it is just not asked for here
        normalise_image(big, "image/png", max_bytes=5 * 1024 * 1024)
