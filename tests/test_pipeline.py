"""Real local HTTP, yt-dlp and FFmpeg, without requests to YouTube."""

import json
import subprocess
import threading
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from yt_dlp import YoutubeDL

from ultradownloader.runtime import ffmpeg_path
from ultradownloader.youtube import LocalYouTube, NetworkPolicy

URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


@contextmanager
def serve(directory):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def media(tmp_path):
    ffmpeg = ffmpeg_path()
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=64x64:d=0.3",
            "-c:v",
            "libx264",
            "-an",
            str(tmp_path / "v.mp4"),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=0.3",
            "-c:a",
            "aac",
            str(tmp_path / "a.m4a"),
        ],
        check=True,
    )
    (tmp_path / "en.vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nLocal caption\n", encoding="utf-8"
    )
    with serve(tmp_path) as base:
        yield base


def adapter(monkeypatch, base):
    def extract(self, url, download=True):
        assert url == URL
        return self.process_ie_result(
            {
                "id": "BaW_jenozKc",
                "title": "Fixture title",
                "uploader": "Fixture artist",
                "webpage_url": URL,
                "extractor": "fixture",
                "extractor_key": "Fixture",
                "subtitles": {"en": [{"ext": "vtt", "url": base + "/en.vtt"}]},
                "formats": [
                    {
                        "format_id": "v",
                        "url": base + "/v.mp4",
                        "ext": "mp4",
                        "height": 64,
                        "vcodec": "avc1",
                        "acodec": "none",
                    },
                    {
                        "format_id": "a",
                        "url": base + "/a.m4a",
                        "ext": "m4a",
                        "vcodec": "none",
                        "acodec": "mp4a",
                        "abr": 128,
                    },
                ],
            },
            download=download,
        )

    monkeypatch.setattr(YoutubeDL, "extract_info", extract)


@pytest.mark.parametrize("container", ["mkv", "mp4"])
def test_adaptive_merge_and_resume(media, tmp_path, monkeypatch, container):
    adapter(monkeypatch, media)
    client = LocalYouTube(tmp_path / "downloads", network=NetworkPolicy(sleep=0))
    result = client.download_video(URL, container=container)
    assert result.path.suffix == "." + container
    assert result.path.stat().st_size > 0
    metadata = json.loads(result.metadata.read_text())
    assert metadata["title"] == "Fixture title"
    assert "127.0.0.1" not in result.metadata.read_text()
    before = result.path.stat().st_mtime_ns
    assert client.download_video(URL, container=container).path.stat().st_mtime_ns == before


@pytest.mark.parametrize("bitrate,vbr", [(192, None), (320, None), (320, 0)])
def test_mp3_conversion_and_id3(media, tmp_path, monkeypatch, bitrate, vbr):
    from mutagen.id3 import ID3

    adapter(monkeypatch, media)
    client = LocalYouTube(tmp_path / "downloads", network=NetworkPolicy(sleep=0))
    result = client.download_mp3(URL, bitrate_kbps=bitrate, vbr_quality=vbr)
    assert result.path.suffix == ".mp3"
    assert str(ID3(result.path)["TIT2"]) == "Fixture title"


@pytest.mark.parametrize(
    "codec,extension",
    [("best", "m4a"), ("m4a", "m4a"), ("opus", "opus"), ("flac", "flac"), ("wav", "wav")],
)
def test_original_audio_keeps_codec(media, tmp_path, monkeypatch, codec, extension):
    adapter(monkeypatch, media)
    result = LocalYouTube(tmp_path / "downloads", network=NetworkPolicy(sleep=0)).download_audio(
        URL, codec=codec
    )
    assert result.path.suffix == "." + extension


def test_real_subtitle_pipeline(media, tmp_path, monkeypatch):
    adapter(monkeypatch, media)
    client = LocalYouTube(tmp_path / "downloads", network=NetworkPolicy(sleep=0))
    result = client.download_subtitles(URL)
    assert result.files["txt"].read_text() == "Local caption\n"
    assert len(result.files) == 5


def test_legacy_http_cli_and_hash(tmp_path):
    import hashlib

    from ultradownloader.cli import main

    payload = b"HTTP fixture contents"
    (tmp_path / "file.bin").write_bytes(payload)
    output = tmp_path / "output"
    with serve(tmp_path) as base:
        assert (
            main(
                [
                    base + "/file.bin",
                    "-o",
                    str(output) + "/",
                    "--hash",
                    hashlib.sha256(payload).hexdigest(),
                ]
            )
            == 0
        )
        assert (output / "file.bin").read_bytes() == payload
        with pytest.raises(RuntimeError, match="SHA256 verification failed"):
            main(["http", base + "/file.bin", "-o", str(output) + "/", "--hash", "0" * 64])
