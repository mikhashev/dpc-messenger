"""A provider whose `supports_vision()` can answer yes must carry a picture.

Callers trust the flag: `LLMManager.query` checks it and then calls
`generate_with_vision`; the agent's image routing and the gateway ask the same
question. `AIProvider.generate_with_vision` raises NotImplementedError, so a
yes without an override sends the user's image into that exception.
GigaChat answered yes for its Max model with no override (board card
GIGACHAT-MAX-SAYS-IT-SEES-IMAGES-AND-HAS-NO-PATH-TO-SEND-ONE).

The check is static, so nothing is constructed: no network, no keys, no
~/.dpc. «Can answer yes» is any `supports_vision` other than the base's or
one whose every return is the literal False.
"""

import ast
import importlib
import inspect
import pkgutil
import textwrap

import pytest

import dpc_client_core.providers as providers_pkg
from dpc_client_core.providers.base import AIProvider


def _provider_classes():
    for info in pkgutil.iter_modules(providers_pkg.__path__):
        importlib.import_module(f"{providers_pkg.__name__}.{info.name}")
    found, todo = {}, [AIProvider]
    while todo:
        for sub in todo.pop().__subclasses__():
            if sub.__module__.startswith(providers_pkg.__name__ + ".") and sub not in found:
                found[sub] = None
                todo.append(sub)
    return sorted(found, key=lambda c: c.__name__)


def _function_tree(func) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


def can_say_yes(cls) -> bool:
    func = cls.supports_vision
    if func is AIProvider.supports_vision:
        return False
    returns = [n for n in ast.walk(_function_tree(func)) if isinstance(n, ast.Return)]
    return any(not (isinstance(r.value, ast.Constant) and r.value.value is False) for r in returns)


def has_a_vision_path(cls) -> bool:
    func = cls.generate_with_vision
    if func is AIProvider.generate_with_vision:
        return False
    raises = [n for n in ast.walk(_function_tree(func))
              if isinstance(n, ast.Raise) and "NotImplementedError" in ast.dump(n)]
    return not raises


def test_every_provider_that_can_say_yes_to_images_has_a_path_that_sends_one():
    classes = _provider_classes()
    sees = [c for c in classes if can_say_yes(c)]
    # Guard against a harness that finds nothing and passes: these are known to see.
    assert {"AnthropicProvider", "GeminiProvider", "OllamaProvider"} <= {c.__name__ for c in sees}
    assert len(classes) >= 12
    offenders = [c.__name__ for c in sees if not has_a_vision_path(c)]
    assert offenders == [], (
        f"{offenders}: supports_vision() can answer yes but generate_with_vision is the base "
        "class's (or raises NotImplementedError); make the flag False until a path exists"
    )


def test_gigachat_does_not_claim_to_see():
    from dpc_client_core.providers.gigachat_provider import GigaChatProvider

    assert not can_say_yes(GigaChatProvider)


def test_the_checker_catches_the_shape_it_exists_for():
    class Claims(AIProvider):
        def supports_vision(self):
            return "Max" in self.model

    class ClaimsAndRaises(Claims):
        async def generate_with_vision(self, prompt, images, **kwargs):
            raise NotImplementedError("later")

    class Sees(Claims):
        async def generate_with_vision(self, prompt, images, **kwargs):
            return "ok"

    class Denies(AIProvider):
        def supports_vision(self):
            return False

    assert can_say_yes(Claims) and not has_a_vision_path(Claims)
    assert can_say_yes(ClaimsAndRaises) and not has_a_vision_path(ClaimsAndRaises)
    assert can_say_yes(Sees) and has_a_vision_path(Sees)
    assert not can_say_yes(Denies) and not can_say_yes(AIProvider)
