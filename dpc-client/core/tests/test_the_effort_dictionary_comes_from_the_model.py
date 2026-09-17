"""The words an effort selector may send belong to the model, not to a table.

The chat template is the authority on them, and it ships inside the GGUF under
`tokenizer.chat_template`, so the dictionary is readable without starting a
child. A template that guards the value names its vocabulary in the guard:

    {%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}
    {%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.managers.llama_server_supervisor import effort_dictionary_of

QWEN_GUARD = """
{%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}
{%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}
{{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ '.') }}
{%- endif %}
"""


class TestReadingTheDictionary:
    def test_the_guard_names_the_words_and_the_default(self):
        assert effort_dictionary_of(QWEN_GUARD) == (("xhigh", "medium", "low"), "xhigh")

    def test_a_template_with_no_guard_says_nothing_rather_than_guessing(self):
        assert effort_dictionary_of("{{ messages | last }}") is None

    def test_a_guard_without_a_default_still_yields_its_words(self):
        template = "{%- if reasoning_effort not in ('deep', 'shallow') %}{{ raise }}"

        assert effort_dictionary_of(template) == (("deep", "shallow"), None)

    def test_double_quotes_and_spacing_are_not_part_of_a_word(self):
        template = '{%- if reasoning_effort  not  in ( "high" ,  "low" ) %}'

        assert effort_dictionary_of(template) == (("high", "low"), None)
