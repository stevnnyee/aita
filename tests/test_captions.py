from __future__ import annotations

import pytest

from app.config import Settings
from app.services import captions as cap
from app.services.voiceover import Alignment, VoiceoverResult
from app.services.script_generator import Script


def words(*texts):
    """TimedPhrase words at 1s each, back to back."""
    return [cap.TimedPhrase(t, float(i), float(i + 1)) for i, t in enumerate(texts)]


def alignment_for(text):
    """Build a char-level Alignment with 0.1s per character."""
    chars = list(text)
    starts = [i * 0.1 for i in range(len(chars))]
    ends = [(i + 1) * 0.1 for i in range(len(chars))]
    return Alignment(chars, starts, ends)


def make_script(text):
    return Script(full_text=text, hook="", story="", verdict="", cta="", word_count=0)


# --- _format_ass_time --------------------------------------------------------

@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0.0, "0:00:00.00"),
        (1.5, "0:00:01.50"),
        (-3.0, "0:00:00.00"),
        (1.999, "0:00:02.00"),       # centi rollover
        (59.999, "0:01:00.00"),      # sec+min rollover
        (3661.25, "1:01:01.25"),
    ],
)
def test_format_ass_time(seconds, expected):
    assert cap._format_ass_time(seconds) == expected


# --- _escape_ass_text --------------------------------------------------------

def test_escape_ass_text():
    assert cap._escape_ass_text("a{b}c\\d") == r"a\{b\}c\\d"


# --- _words_from_alignment ---------------------------------------------------

def test_words_from_alignment_splits_on_whitespace():
    res = cap._words_from_alignment(alignment_for("go now"))
    assert [w.text for w in res] == ["go", "now"]
    # "go" spans chars 0-1 (0.0 .. 0.2), "now" spans chars 3-5 (0.3 .. 0.6)
    assert res[0].start == pytest.approx(0.0)
    assert res[0].end == pytest.approx(0.2)
    assert res[1].start == pytest.approx(0.3)
    assert res[1].end == pytest.approx(0.6)


def test_words_from_alignment_handles_leading_trailing_space():
    res = cap._words_from_alignment(alignment_for("  hi  "))
    assert [w.text for w in res] == ["hi"]


# --- _even_word_timing -------------------------------------------------------

def test_even_word_timing_distributes_evenly():
    res = cap._even_word_timing("a b c d", 8.0)
    assert [w.text for w in res] == ["a", "b", "c", "d"]
    assert res[0].start == 0.0 and res[0].end == 2.0
    assert res[-1].end == pytest.approx(8.0)


def test_even_word_timing_empty_or_zero_duration():
    assert cap._even_word_timing("", 5.0) == []
    assert cap._even_word_timing("a b", 0.0) == []


# --- _chunk_words ------------------------------------------------------------

@pytest.mark.parametrize(
    "n,expected_lines",
    [(1, 1), (2, 1), (3, 1), (4, 1), (5, 2), (6, 2), (7, 2), (8, 3), (9, 3)],
)
def test_chunk_words_line_counts(n, expected_lines):
    chunks = cap._chunk_words(words(*[f"w{i}" for i in range(n)]))
    assert len(chunks) == expected_lines
    # no line exceeds the max, no interior line is below the min
    for c in chunks:
        assert 1 <= len(c.text.split()) <= cap.MAX_WORDS_PER_LINE


def test_chunk_words_spans_start_to_end():
    chunks = cap._chunk_words(words("a", "b", "c"))
    assert chunks[0].text == "a b c"
    assert chunks[0].start == 0.0
    assert chunks[0].end == 3.0


def test_chunk_words_five_splits_two_then_three():
    chunks = cap._chunk_words(words("a", "b", "c", "d", "e"))
    assert [c.text for c in chunks] == ["a b", "c d e"]


def test_chunk_words_empty():
    assert cap._chunk_words([]) == []


# --- _timed_phrases (alignment vs fallback) ---------------------------------

def test_timed_phrases_uses_alignment():
    phrases = cap._timed_phrases("ignored text", alignment_for("go now"), 10.0)
    assert [p.text for p in phrases] == ["go now"]


def test_timed_phrases_falls_back_when_no_alignment(caplog):
    phrases = cap._timed_phrases("a b c d e", None, 10.0)
    assert [p.text for p in phrases] == ["a b", "c d e"]


def test_timed_phrases_falls_back_on_whitespace_only_alignment():
    phrases = cap._timed_phrases("x y z", alignment_for("   "), 6.0)
    assert [p.text for p in phrases] == ["x y z"]


# --- _build_ass_content ------------------------------------------------------

def test_build_ass_content_uppercases_and_has_header():
    content = cap._build_ass_content(words("hello", "world"))
    assert "[Script Info]" in content
    assert f"PlayResX: {cap.PLAY_RES_X}" in content
    dialogue = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogue) == 2
    assert "HELLO" in dialogue[0] and "WORLD" in dialogue[1]


# --- generate ----------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    return Settings(output_dir=tmp_path / "output")


def test_generate_writes_ass_using_voiceover_alignment(settings):
    vo = VoiceoverResult(
        audio_path=settings.output_dir / "r1" / "voiceover.mp3",
        alignment_path=settings.output_dir / "r1" / "alignment.json",
        duration_seconds=2.0,
        alignment=alignment_for("go now please"),
    )
    result = cap.generate(make_script("ignored"), vo, "r1", settings=settings)

    assert result.ass_path.is_file()
    assert result.line_count == 1  # 3 words -> 1 line
    body = result.ass_path.read_text()
    assert "GO NOW PLEASE" in body


def test_generate_loads_alignment_from_disk_when_missing_on_result(settings, monkeypatch):
    from app.services import voiceover as vo_mod

    out = settings.output_dir / "r2"
    out.mkdir(parents=True)
    align_path = out / "alignment.json"
    vo_mod._save_alignment(align_path, alignment_for("hello there"))

    vo = VoiceoverResult(
        audio_path=out / "voiceover.mp3",
        alignment_path=align_path,
        duration_seconds=2.0,
        alignment=None,  # forces load from disk
    )
    result = cap.generate(make_script("ignored"), vo, "r2", settings=settings)
    assert "HELLO THERE" in result.ass_path.read_text()


def test_generate_even_fallback_uses_script_text(settings):
    vo = VoiceoverResult(
        audio_path=settings.output_dir / "r3" / "voiceover.mp3",
        alignment_path=settings.output_dir / "r3" / "missing.json",
        duration_seconds=4.0,
        alignment=None,
    )
    result = cap.generate(make_script("one two three"), vo, "r3", settings=settings)
    assert result.line_count == 1
    assert "ONE TWO THREE" in result.ass_path.read_text()
