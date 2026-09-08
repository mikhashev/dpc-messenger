"""A picture crosses as pixels, never as a place on the sender's disk.

Every receiving provider in the client reads `base64` first and falls back to
`open(img["path"])` when it is absent. That path belongs to the sender: on
another operating system it does not resolve at all, and on a like one it can
resolve to a different file that happens to sit there — the receiver would then
answer about something nobody sent.

DPTP 3.4 already settles it: `base64` is required and `path` is the original
filename, which no receiver is promised it can open. The builder is where that
holds for every caller at once, so a future producer of this message cannot
reintroduce the leak by assembling its own dict.
"""

import pytest

from dpc_protocol.protocol import create_remote_inference_request

A_PICTURE = {
    "path": r"C:\Users\someone\AppData\Local\Temp\shot.png",
    "base64": "aGVsbG8=",
    "mime_type": "image/png",
}


def _images(**kwargs):
    message = create_remote_inference_request("req-1", "what is this?", **kwargs)
    return message["payload"].get("images")


def test_the_senders_own_path_does_not_travel():
    (image,) = _images(images=[A_PICTURE])

    assert "path" not in image
    assert image["base64"] == "aGVsbG8="
    assert image["mime_type"] == "image/png"


def test_the_caller_keeps_the_dict_it_handed_over():
    """The caller reuses its list for the local path; the builder copies."""
    original = dict(A_PICTURE)

    _images(images=[original])

    assert original["path"] == A_PICTURE["path"]


def test_a_picture_with_no_pixels_is_refused_rather_than_sent_as_a_path():
    with pytest.raises(ValueError, match="base64"):
        _images(images=[{"path": "/home/someone/shot.png", "mime_type": "image/png"}])


def test_a_picture_that_does_not_say_what_it_is_is_refused_too():
    """§3.4 makes mime_type required, and a receiver defaults an absent one to
    PNG — so a JPEG would arrive announced as something it is not."""
    with pytest.raises(ValueError, match="mime_type"):
        _images(images=[{"base64": "aGVsbG8="}])


def test_both_missing_are_named_together():
    with pytest.raises(ValueError, match="base64 and no mime_type"):
        _images(images=[{"path": "/home/someone/shot.png"}])


def test_a_request_without_images_says_nothing_about_them():
    assert _images() is None
