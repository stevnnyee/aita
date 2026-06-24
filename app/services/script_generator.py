from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.config import Settings, get_settings
from app.services.reddit_scraper import RedditPost
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from openai import OpenAI

logger = get_logger(__name__)

TARGET_WORDS = 270
MIN_WORDS = 250
MAX_WORDS = 290
MODEL = "gpt-4o"

SYSTEM_PROMPT = """\
You write short-form vertical video voiceover scripts for r/AmItheAsshole (AITA) content.

Structure (in order):
1. hook — ~5 seconds, ~20 words. Open with a provocative question that makes viewers stop scrolling.
2. story — Condense the Reddit post into a clear, dramatic narrative. Keep names simple (e.g. "the poster", "their sister"). Do not read the subreddit title verbatim.
3. verdict — State the AITA verdict (NTA, YTA, ESH, NAH, or INFO) and a brief hot take explaining why.
4. cta — One short call-to-action encouraging viewers to follow for more AITA verdicts.

Rules:
- full_text must be the complete spoken script, all sections flowing naturally as one continuous narration suitable for text-to-speech.
- full_text must be approximately 270 words (acceptable range: 250–290). Short-form pacing: ~4 words per second.
- No stage directions, sound effects, or markdown.
- Do not mention Reddit, upvotes, or that this is a script.
- Return valid JSON only with these keys: hook, story, verdict, cta, full_text.
"""


@dataclass(frozen=True)
class Script:
    full_text: str
    hook: str
    story: str
    verdict: str
    cta: str
    word_count: int


def word_count(text: str) -> int:
    return len(text.split())


def _openai_client(settings: Settings) -> "OpenAI":
    from openai import OpenAI

    return OpenAI(api_key=settings.openai_api_key)


def _user_prompt(post: RedditPost) -> str:
    return (
        f"Title: {post.title}\n\n"
        f"Post body:\n{post.body}\n\n"
        f"Write the voiceover script. Target {TARGET_WORDS} words in full_text."
    )


def _parse_response(content: str) -> Script:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"OpenAI returned invalid JSON: {exc}") from exc

    required = ("hook", "story", "verdict", "cta", "full_text")
    missing = [key for key in required if key not in data or not str(data[key]).strip()]
    if missing:
        raise ValueError(f"OpenAI response missing fields: {', '.join(missing)}")

    full_text = str(data["full_text"]).strip()
    return Script(
        hook=str(data["hook"]).strip(),
        story=str(data["story"]).strip(),
        verdict=str(data["verdict"]).strip(),
        cta=str(data["cta"]).strip(),
        full_text=full_text,
        word_count=word_count(full_text),
    )


def _call_openai(
    client: OpenAI,
    post: RedditPost,
    *,
    correction: Optional[str] = None,
) -> Script:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(post)},
    ]
    if correction:
        messages.append({"role": "user", "content": correction})

    response = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0.8,
        messages=messages,
    )

    content = response.choices[0].message.content
    if not content:
        raise ValueError("OpenAI returned an empty response")

    return _parse_response(content)


def _in_word_range(count: int) -> bool:
    return MIN_WORDS <= count <= MAX_WORDS


def generate(
    post: RedditPost,
    *,
    settings: Optional[Settings] = None,
) -> Script:
    """Generate a ~270-word voiceover script for a Reddit post."""
    settings = settings or get_settings()
    if not settings.openai_api_key:
        raise ValueError(
            "Missing openai_api_key. Set OPENAI_API_KEY via environment or .env."
        )

    client = _openai_client(settings)
    script = _call_openai(client, post)

    if not _in_word_range(script.word_count):
        logger.warning(
            "Script word count %d out of range [%d, %d]; retrying once",
            script.word_count,
            MIN_WORDS,
            MAX_WORDS,
        )
        correction = (
            f"The full_text was {script.word_count} words. "
            f"Rewrite the entire script so full_text is between {MIN_WORDS} "
            f"and {MAX_WORDS} words (target {TARGET_WORDS}). "
            "Return the same JSON structure."
        )
        try:
            script = _call_openai(client, post, correction=correction)
        except Exception:
            # The first script is valid (just off-length); prefer it over
            # failing the whole pipeline if the correction call errors out.
            logger.exception(
                "Retry call failed; keeping original %d-word script", script.word_count
            )

    if not _in_word_range(script.word_count):
        logger.warning(
            "Script word count %d still outside [%d, %d] after retry; proceeding",
            script.word_count,
            MIN_WORDS,
            MAX_WORDS,
        )

    logger.info(
        "Generated script for post %s: %d words",
        post.id,
        script.word_count,
    )
    return script


SAMPLE_POST = RedditPost(
    id="sample",
    title="AITA for refusing to give my sister the wedding venue deposit?",
    body=(
        "My sister got engaged last year and asked if she could use the venue I booked "
        "for my wedding because she loved it. I had put down a $5,000 non-refundable "
        "deposit. She assumed I would just transfer it to her since my fiancé and I "
        "decided to elope instead. When I said she would need to reimburse me the full "
        "$5,000 before I transferred anything, she called me selfish and said family "
        "shouldn't charge family. My parents are siding with her and say I should gift "
        "her the deposit since I'm not using the venue anyway. I told them the venue "
        "won't refund me, so if she wants it she pays me back. Now I'm uninvited to "
        "her engagement party."
    ),
    score=4200,
    url="https://www.reddit.com/r/AmItheAsshole/comments/sample",
    author="sample_user",
    created_utc=0.0,
)


def _print_script(script: Script) -> None:
    print(f"Word count: {script.word_count}\n")
    print("--- HOOK ---")
    print(script.hook)
    print("\n--- STORY ---")
    print(script.story)
    print("\n--- VERDICT ---")
    print(script.verdict)
    print("\n--- CTA ---")
    print(script.cta)
    print("\n--- FULL TEXT (for TTS) ---")
    print(script.full_text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a voiceover script from a Reddit post using GPT-4o.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Use a built-in sample AITA post instead of fetching from Reddit",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("Set OPENAI_API_KEY in .env before running.")

    if args.sample:
        post = SAMPLE_POST
    else:
        from app.database import get_db_session, init_db
        from app.services.reddit_scraper import fetch_eligible_post

        init_db()
        with get_db_session() as db:
            post = fetch_eligible_post(db, settings=settings)
        if post is None:
            raise SystemExit(
                "No eligible Reddit post found. Use --sample or check Reddit config."
            )

    script = generate(post, settings=settings)
    print(f"Post: {post.title}\n")
    _print_script(script)


if __name__ == "__main__":
    main()
