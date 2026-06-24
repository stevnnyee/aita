from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from app.config import Settings
from app.services import youtube_uploader as yt
from app.services.reddit_scraper import RedditPost
from app.services.script_generator import Script


def make_post(title="AITA for testing the uploader?", url="https://reddit.com/r/x/1"):
    return RedditPost(
        id="abc", title=title, body="b", score=1000, url=url,
        author="u", created_utc=0.0,
    )


def make_script(verdict="NTA. Clear cut."):
    return Script(full_text="f", hook="h", story="s", verdict=verdict, cta="c", word_count=1)


# --- _truncate_title ---------------------------------------------------------

def test_truncate_title_short_unchanged():
    assert yt._truncate_title("Short title") == "Short title"


def test_truncate_title_collapses_whitespace():
    assert yt._truncate_title("a   b\n c") == "a b c"


def test_truncate_title_truncates_with_ellipsis():
    title = "x" * 150
    out = yt._truncate_title(title)
    assert len(out) <= yt.MAX_TITLE_LENGTH
    assert out.endswith("...")


def test_truncate_title_exact_boundary():
    title = "y" * yt.MAX_TITLE_LENGTH
    assert yt._truncate_title(title) == title  # no ellipsis at exactly max


# --- _build_title / _build_description --------------------------------------

def test_build_title_uses_post_title():
    assert yt._build_title(make_post(title="Hi  there")) == "Hi there"


def test_build_description_includes_verdict_url_hashtags():
    desc = yt._build_description(make_post(url="https://reddit.com/p/9"), make_script("YTA!"))
    assert desc.startswith("YTA!")
    assert "https://reddit.com/p/9" in desc
    assert "#AITA" in desc


def test_build_description_falls_back_on_empty_verdict():
    desc = yt._build_description(make_post(), make_script(verdict="   "))
    assert desc.startswith("Watch for the full AITA verdict.")


# --- _validate_youtube_files -------------------------------------------------

def test_validate_youtube_files_missing(tmp_path):
    s = Settings(youtube_client_secrets_file=tmp_path / "nope.json")
    with pytest.raises(FileNotFoundError, match="client secrets"):
        yt._validate_youtube_files(s)


def test_validate_youtube_files_present(tmp_path):
    secrets = tmp_path / "client_secrets.json"
    secrets.write_text("{}")
    yt._validate_youtube_files(Settings(youtube_client_secrets_file=secrets))  # no raise


# --- _load_credentials -------------------------------------------------------

def test_load_credentials_raises_without_token(tmp_path):
    s = Settings(youtube_token_file=tmp_path / "missing_token.json")
    with pytest.raises(RuntimeError, match="missing or invalid"):
        yt._load_credentials(s)


# --- upload ------------------------------------------------------------------

class FakeStatus:
    def __init__(self, p):
        self._p = p

    def progress(self):
        return self._p


class FakeRequest:
    def __init__(self, steps):
        self._it = iter(steps)

    def next_chunk(self):
        return next(self._it)


class FakeVideos:
    def __init__(self, request, capture):
        self._request = request
        self._capture = capture

    def insert(self, part, body, media_body):
        self._capture.update(part=part, body=body, media=media_body)
        return self._request


class FakeYouTube:
    def __init__(self, request, capture):
        self._videos = FakeVideos(request, capture)

    def videos(self):
        return self._videos


@pytest.fixture
def stub_media(monkeypatch):
    """Stub googleapiclient.http.MediaFileUpload so upload() needs no real SDK."""
    module = types.ModuleType("googleapiclient.http")

    class FakeMediaFileUpload:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    module.MediaFileUpload = FakeMediaFileUpload
    monkeypatch.setitem(sys.modules, "googleapiclient.http", module)
    return FakeMediaFileUpload


def test_upload_missing_video(tmp_path):
    with pytest.raises(FileNotFoundError, match="Video file not found"):
        yt.upload(tmp_path / "nope.mp4", make_post(), make_script())


def test_upload_happy_path(tmp_path, monkeypatch, stub_media):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"video")
    capture = {}
    request = FakeRequest([(FakeStatus(0.5), None), (None, {"id": "VID123"})])
    monkeypatch.setattr(
        yt, "get_youtube_service", lambda settings: FakeYouTube(request, capture)
    )

    settings = Settings(youtube_privacy_status="unlisted")
    result = yt.upload(video, make_post(title="My AITA"), make_script("ESH"), settings=settings)

    assert result.youtube_video_id == "VID123"
    assert result.youtube_url == "https://www.youtube.com/watch?v=VID123"
    assert result.title == "My AITA"

    assert capture["part"] == "snippet,status"
    snippet = capture["body"]["snippet"]
    assert snippet["title"] == "My AITA"
    assert snippet["categoryId"] == yt.YOUTUBE_CATEGORY_ENTERTAINMENT
    assert snippet["tags"] == list(yt.DEFAULT_TAGS)
    status = capture["body"]["status"]
    assert status["privacyStatus"] == "unlisted"
    assert status["selfDeclaredMadeForKids"] is False
    # resumable upload was configured
    assert capture["media"].kwargs["resumable"] is True
