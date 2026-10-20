"""The embedding size is read by the name sentence-transformers 5.4+ uses.

5.4 renamed `get_sentence_embedding_dimension` to `get_embedding_dimension` and
warns on the old name; a release that drops it would leave agent memory without
its vector size. An older model object without the new name still answers.
"""

from dpc_client_core.dpc_agent.memory import EmbeddingProvider


class _NewModel:
    def get_embedding_dimension(self):
        return 1024

    def get_sentence_embedding_dimension(self):
        raise AssertionError("the deprecated name was called")


class _OldModel:
    def get_sentence_embedding_dimension(self):
        return 768


def _provider_with(model):
    provider = EmbeddingProvider()
    provider._model = model
    return provider


def test_a_current_model_is_asked_by_the_new_name():
    assert _provider_with(_NewModel()).dimensions == 1024


def test_a_model_without_the_new_name_is_asked_by_the_old_one():
    assert _provider_with(_OldModel()).dimensions == 768
