import json
from pathlib import Path

import pytest
from yt_dlp import YoutubeDL

from ultradownloader.youtube import (
    LocalYouTube,
    MediaError,
    NetworkPolicy,
    SubtitleUnavailable,
    _pick_language,
    convert_captions,
    redact,
    validate_url,
    video_selector,
)

URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/x",
        "http://youtube.com/watch?v=x",
        "https://youtube.com.evil.test/watch?v=x",
        "https://user:secret@youtube.com/",
        "https://youtube.com:8080/watch?v=x",
    ],
)
def test_url_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize(
    "url", [URL, "https://youtu.be/BaW_jenozKc", "https://music.youtube.com/watch?v=BaW_jenozKc"]
)
def test_url_accepted(url):
    validate_url(url)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fragments": 0},
        {"retries": -1},
        {"sleep": -1},
        {"timeout": float("nan")},
        {"proxy": "file:///x"},
        {"cookie_file": "missing"},
        {"rate_limit": 0},
    ],
)
def test_network_validation(kwargs):
    with pytest.raises(ValueError):
        NetworkPolicy(**kwargs)


def test_actual_format_selection():
    formats = [
        {"format_id": "v360", "ext": "mp4", "height": 360, "vcodec": "avc1", "acodec": "none"},
        {"format_id": "v1080", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "none"},
        {"format_id": "v2160", "ext": "webm", "height": 2160, "vcodec": "vp9", "acodec": "none"},
        {"format_id": "a", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128},
    ]
    for container, quality, expected in (
        ("mkv", "best", "v2160+a"),
        ("mkv", 1080, "v1080+a"),
        ("mp4", "best", "v1080+a"),
    ):
        with YoutubeDL({"quiet": True, "format": video_selector(container, quality)}) as ydl:
            result = ydl.process_ie_result(
                {
                    "id": "test",
                    "title": "test",
                    "formats": [
                        {**fmt, "url": f"https://example.com/{fmt['format_id']}"} for fmt in formats
                    ],
                },
                download=False,
            )
        assert result["format_id"] == expected


def test_resolution_ceiling_never_silently_exceeded():
    assert "[height<=720]" in video_selector("mkv", 720)
    with pytest.raises(ValueError):
        video_selector("mkv", "720p;bad")


def test_language_matching():
    assert _pick_language("en", {"en-US": []}) == "en-US"
    assert _pick_language("en-US", {"en-GB": []}) is None


def test_transcripts_and_subtitle_conversion(tmp_path):
    path = tmp_path / "captions.en.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello world\n\n"
        "00:00:01.500 --> 00:00:03.000\nHello world again\n\n"
        "00:00:10.000 --> 00:00:11.000\nHello world\n",
        encoding="utf-8",
    )
    results = convert_captions(path, ("srt", "ttml", "txt", "json", "vtt"))
    assert results["txt"].read_text() == "Hello world\nagain\nHello world\n"
    assert json.loads(results["json"].read_text())[1] == {
        "start": 1.5,
        "duration": 1.5,
        "text": "again",
    }
    assert "00:00:00,000" in results["srt"].read_text()
    assert "<tt" in results["ttml"].read_text()


def test_manual_preference_and_no_media_download(tmp_path, monkeypatch):
    client = LocalYouTube(tmp_path)
    monkeypatch.setattr(
        client, "probe", lambda _: {"subtitles": {"en": [{}]}, "automatic_captions": {"en": [{}]}}
    )

    def run(url, options, *, download):
        assert options["skip_download"] and download
        assert options["writesubtitles"] and not options["writeautomaticsub"]
        job = Path(options["outtmpl"]).parent
        source = job / "media.en.vtt"
        source.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHi\n", encoding="utf-8")
        return {"requested_subtitles": {"en": {"filepath": str(source)}}}

    monkeypatch.setattr(client, "_run", run)
    assert client.download_transcript(URL).files["txt"].read_text() == "Hi\n"
    with pytest.raises(SubtitleUnavailable):
        client.download_transcript(URL, language="hi")


def test_quality_separates_resume_directories(tmp_path):
    client = LocalYouTube(tmp_path)
    assert client._job(URL, "video:mkv:best") != client._job(URL, "video:mkv:720")


def test_secret_redaction():
    message = redact("failed socks5://user:password@host:123 and https://cdn.test/v?token=secret")
    assert "password" not in message and "secret" not in message


def test_playlist_opt_in_is_enforced_before_extraction(tmp_path, monkeypatch):
    import yt_dlp

    def unexpected(*args, **kwargs):
        pytest.fail("Playlist extraction must not start without explicit opt-in")

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", unexpected)
    with pytest.raises(MediaError, match="require --playlist"):
        LocalYouTube(tmp_path).probe("https://www.youtube.com/playlist?list=test")


def test_playlist_selection_and_empty_result(tmp_path, monkeypatch):
    client = LocalYouTube(tmp_path)

    def entries(url, opts):
        assert not opts["noplaylist"] and opts["playlist_items"] == "1:3"
        return {"entries": [{"id": "BaW_jenozKc"}]}

    monkeypatch.setattr(client, "_run", entries)
    assert client.playlist(URL, items="1:3") == [URL]
    monkeypatch.setattr(client, "_run", lambda *args: {"entries": []})
    with pytest.raises(MediaError, match="no available videos"):
        client.playlist(URL)


def test_symlink_output_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks needs OS privileges")
    with pytest.raises(MediaError):
        LocalYouTube(link)._job(URL, "test")
