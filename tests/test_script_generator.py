from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.services import script_generator as sg


def make_script(n_words: int) -> sg.Script:
    text = " ".join(["word"] * n_words)
    return sg.Script(
        full_text=text,
        hook="hook",
        story="story",
        verdict="NTA",
        cta="cta",
        word_count=sg.word_count(text),
    )


def stub_call_openai(monkeypatch, items):
    """Make _call_openai yield `items` in order; an Exception item is raised."""
    monkeypatch.setattr(sg, "_openai_client", lambda settings: object())
    seq = iter(items)
    state = {"calls": 0}

    def fake(client, post, *, correction=None):
        state["calls"] += 1
        item = next(seq)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(sg, "_call_openai", fake)
    return state


@pytest.fixture
def settings():
    return Settings(openai_api_key="sk-test")


# --- word_count --------------------------------------------------------------

def test_word_count_splits_on_whitespace():
    assert sg.word_count("one two   three\nfour") == 4
    assert sg.word_count("") == 0


# --- _in_word_range ----------------------------------------------------------

@pytest.mark.parametrize(
    "count,expected",
    [(sg.MIN_WORDS, True), (sg.MAX_WORDS, True), (sg.TARGET_WORDS, True),
     (sg.MIN_WORDS - 1, False), (sg.MAX_WORDS + 1, False)],
)
def test_in_word_range(count, expected):
    assert sg._in_word_range(count) is expected


# --- _parse_response ---------------------------------------------------------

def test_parse_response_valid():
    payload = json.dumps(
        {"hook": "h", "story": "s", "verdict": "NTA", "cta": "c",
         "full_text": "a b c d e"}
    )
    script = sg._parse_response(payload)
    assert script.verdict == "NTA"
    assert script.word_count == 5


def test_parse_response_strips_whitespace_and_counts():
    payload = json.dumps(
        {"hook": " h ", "story": "s", "verdict": "YTA", "cta": "c",
         "full_text": "  one two three  "}
    )
    script = sg._parse_response(payload)
    assert script.hook == "h"
    assert script.full_text == "one two three"
    assert script.word_count == 3


def test_parse_response_invalid_json():
    with pytest.raises(ValueError, match="invalid JSON"):
        sg._parse_response("not json{")


def test_parse_response_missing_field():
    payload = json.dumps({"hook": "h", "story": "s", "verdict": "NTA", "cta": "c"})
    with pytest.raises(ValueError, match="full_text"):
        sg._parse_response(payload)


def test_parse_response_empty_field():
    payload = json.dumps(
        {"hook": "h", "story": "   ", "verdict": "NTA", "cta": "c", "full_text": "x"}
    )
    with pytest.raises(ValueError, match="story"):
        sg._parse_response(payload)


# --- _user_prompt ------------------------------------------------------------

def test_user_prompt_includes_title_and_body():
    prompt = sg._user_prompt(sg.SAMPLE_POST)
    assert sg.SAMPLE_POST.title in prompt
    assert sg.SAMPLE_POST.body in prompt


# --- generate: retry orchestration ------------------------------------------

def test_generate_requires_api_key():
    with pytest.raises(ValueError, match="openai_api_key"):
        sg.generate(sg.SAMPLE_POST, settings=Settings(openai_api_key=""))


def test_generate_no_retry_when_in_range(settings, monkeypatch):
    state = stub_call_openai(monkeypatch, [make_script(sg.TARGET_WORDS)])
    script = sg.generate(sg.SAMPLE_POST, settings=settings)
    assert state["calls"] == 1
    assert script.word_count == sg.TARGET_WORDS


def test_generate_retries_and_uses_in_range_result(settings, monkeypatch):
    state = stub_call_openai(
        monkeypatch, [make_script(100), make_script(sg.TARGET_WORDS)]
    )
    script = sg.generate(sg.SAMPLE_POST, settings=settings)
    assert state["calls"] == 2
    assert script.word_count == sg.TARGET_WORDS


def test_generate_proceeds_when_retry_still_out_of_range(settings, monkeypatch, caplog):
    state = stub_call_openai(monkeypatch, [make_script(100), make_script(500)])
    script = sg.generate(sg.SAMPLE_POST, settings=settings)
    assert state["calls"] == 2
    assert script.word_count == 500  # proceeds with the retry result


def test_generate_falls_back_to_first_script_when_retry_raises(settings, monkeypatch):
    first = make_script(100)
    state = stub_call_openai(monkeypatch, [first, RuntimeError("api down")])
    script = sg.generate(sg.SAMPLE_POST, settings=settings)
    assert state["calls"] == 2
    assert script is first  # retry failed; original kept rather than crashing
