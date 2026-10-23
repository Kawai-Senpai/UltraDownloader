"""Keep unstable YouTube extraction inside a single yt-dlp adapter."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import runtime


class MediaError(RuntimeError):
    """Media extraction, download or postprocessing failed."""


class SubtitleUnavailable(MediaError):
    """The video has no matching published captions."""


class BotCheckRequired(MediaError):
    """User verification is required; the caller may present a browser handoff."""

    def __init__(self, url: str, detail: str):
        self.url = url
        super().__init__(
            "YouTube requires human verification. Run in an interactive terminal to clear "
            "the check in a browser, or provide --cookies/--cookies-from-browser. " + detail
        )


def _is_bot_check(message: str) -> bool:
    normalized = message.casefold().replace("’", "'")
    return any(
        marker in normalized
        for marker in (
            "not a bot",
            "not a robot",
            "unusual traffic",
            "confirm you're human",
            "confirm you are human",
            "captcha",
        )
    )


def is_youtube_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        return host in {"youtu.be", "youtube.com", "youtube-nocookie.com"} or host.endswith(
            (".youtube.com", ".youtube-nocookie.com")
        )
    except ValueError:
        return False


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        not is_youtube_url(url)
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Use an HTTPS YouTube URL without credentials or a custom port.")


def redact(message: str) -> str:
    message = re.sub(r"(?i)(https?|socks\w*)://[^\s/]*@", r"\1://[redacted]@", message)
    # Signed CDN URLs and tokens should never enter application logs.
    return re.sub(r"https?://[^\s]+[?][^\s]+", "[URL query redacted]", message)


class _Logger:
    def debug(self, message):
        pass

    def warning(self, message):
        logging.getLogger("ultradownloader").warning(redact(message))

    def error(self, message):
        pass  # The caller reports the redacted exception once.


@dataclass(frozen=True)
class NetworkPolicy:
    proxy: str | None = None
    cookie_file: str | None = None
    cookies_from_browser: str | None = None
    retries: int = 5
    timeout: float = 30
    fragments: int = 2
    rate_limit: int | None = None
    sleep: float = 5

    def __post_init__(self):
        if self.cookie_file and self.cookies_from_browser:
            raise ValueError("Choose a cookie file or browser cookies, not both.")
        if (
            self.retries < 0
            or not 1 <= self.fragments <= 16
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
            or not math.isfinite(self.sleep)
            or self.sleep < 0
            or (self.rate_limit is not None and self.rate_limit <= 0)
        ):
            raise ValueError("Invalid retry, timeout, fragment, sleep or rate-limit setting.")
        if self.proxy:
            parsed = urlsplit(self.proxy)
            if (
                parsed.scheme not in {"http", "https", "socks4", "socks5", "socks5h"}
                or not parsed.hostname
            ):
                raise ValueError("Invalid proxy URL.")
        if self.cookie_file:
            path = Path(self.cookie_file).expanduser()
            if not path.is_file() or path.is_symlink():
                raise ValueError("Cookies must be a regular Netscape cookie file.")
            import os

            if os.name == "posix" and path.stat().st_mode & 0o077:
                raise ValueError("Cookie file permissions must be private: chmod 600.")
        if self.cookies_from_browser and self.cookies_from_browser not in {
            "chrome",
            "chromium",
            "edge",
            "firefox",
            "brave",
            "opera",
            "vivaldi",
            "safari",
        }:
            raise ValueError("Unsupported cookie browser.")


@dataclass(frozen=True)
class MediaResult:
    path: Path
    video_id: str
    title: str
    metadata: Path


@dataclass(frozen=True)
class SubtitleResult:
    language: str
    source_kind: str
    files: dict[str, Path]


def video_selector(container: str, quality: str | int = "best") -> str:
    if container not in {"mkv", "mp4", "webm"}:
        raise ValueError("Container must be mkv, mp4 or webm.")
    height = ""
    if str(quality) != "best":
        if not str(quality).isdigit() or int(quality) <= 0:
            raise ValueError("Quality must be best or a positive resolution such as 2160.")
        height = f"[height<={int(quality)}]"
    video_ext = f"[ext={container}]" if container != "mkv" else ""
    audio_ext = {"mkv": "", "mp4": "[ext=m4a]", "webm": "[ext=webm]"}[container]
    return f"bv*{height}{video_ext}+ba{audio_ext}/b{height}{video_ext}"


def _pick_language(language: str, catalog: dict) -> str | None:
    for key in catalog:
        if key.casefold() == language.casefold():
            return key
    # Generic en may fall back to en-US; an explicit en-US never falls back to en-GB.
    if "-" not in language and "_" not in language:
        return next(
            (
                k
                for k in sorted(catalog)
                if re.split("[-_]", k.casefold())[0] == language.casefold()
            ),
            None,
        )
    return None


def convert_captions(source: Path, formats: tuple[str, ...]) -> dict[str, Path]:
    """Preserve timings; remove rolling-caption overlap in derived transcripts only."""
    import pysubs2

    captions = pysubs2.load(str(source), encoding="utf-8")
    segments = []
    previous = []
    previous_end = -1
    for cue in captions:
        text = re.sub(r"\s+", " ", cue.plaintext).strip()
        if not text:
            continue
        words = text.split()
        overlap = 0
        if cue.start < previous_end:
            for count in range(min(len(previous), len(words)), 0, -1):
                if previous[-count:] == words[:count]:
                    overlap = count
                    break
        novel = " ".join(words[overlap:])
        if novel:
            segments.append(
                {"start": cue.start / 1000, "duration": (cue.end - cue.start) / 1000, "text": novel}
            )
        previous, previous_end = words, cue.end
    outputs = {}
    for fmt in formats:
        target = source.with_suffix(f".{fmt}")
        if target.is_symlink():
            raise MediaError(f"Refusing symlink output: {target}")
        if fmt == "txt":
            target.write_text("\n".join(s["text"] for s in segments) + "\n", encoding="utf-8")
        elif fmt == "json":
            target.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
        elif fmt in {"srt", "vtt", "ttml"}:
            if target != source:
                captions.save(str(target), encoding="utf-8", format_=fmt)
        else:
            raise ValueError(f"Unsupported subtitle format: {fmt}")
        outputs[fmt] = target
    return outputs


class LocalYouTube:
    def __init__(
        self,
        root: str | Path = "downloads",
        *,
        network: NetworkPolicy | None = None,
        ffmpeg_location: str | None = None,
        progress: Callable | None = None,
        challenge: Callable | None = None,
    ):
        self.root = Path(root).expanduser().absolute()
        self.network = network or NetworkPolicy()
        self.ffmpeg_location = ffmpeg_location
        self.progress = progress
        self.challenge = challenge
        self._browser_session = None

    def _safe(self, path: Path) -> Path:
        try:
            path.absolute().relative_to(self.root)
            path.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise MediaError("Output path escaped download directory.") from exc
        # Reject symlinks before writing, including partial files and parent directories.
        for part in (path, *path.parents):
            if part.is_symlink():
                raise MediaError(f"Refusing symlink output: {part}")
        return path

    def _job(self, url: str, kind: str) -> Path:
        validate_url(url)
        digest = hashlib.sha256(f"{url}\0{kind}".encode()).hexdigest()[:20]
        path = self._safe(self.root / digest)
        path.mkdir(parents=True, exist_ok=True)
        for existing in path.rglob("*"):
            self._safe(existing)
        return path

    def _options(self, job: Path | None = None) -> dict:
        p = self.network
        opts = {
            "noplaylist": True,
            "continuedl": True,
            "nopart": False,
            "overwrites": False,
            "retries": p.retries,
            "fragment_retries": p.retries,
            "extractor_retries": 3,
            "file_access_retries": 3,
            "skip_unavailable_fragments": False,
            "retry_sleep_functions": {
                kind: lambda n: min(2 ** min(n, 5), 30)
                for kind in ("http", "fragment", "extractor")
            },
            "socket_timeout": p.timeout,
            "concurrent_fragment_downloads": p.fragments,
            "sleep_interval": p.sleep,
            "max_sleep_interval": p.sleep * 2,
            "sleep_interval_requests": 0.75,
            "sleep_interval_subtitles": 1,
            "ratelimit": p.rate_limit,
            "quiet": True,
            "noprogress": True,
            "logger": _Logger(),
            "cachedir": False,
            "ignoreerrors": False,
            "enable_file_urls": False,
            "nocheckcertificate": False,
            "writelink": False,
            "writeurllink": False,
            "writewebloclink": False,
            "writedesktoplink": False,
            "allow_unplayable_formats": False,
            "js_runtimes": runtime.javascript_runtime(),
        }
        if job:
            # The directory includes every quality/format option; retries cannot reuse other qualities.
            opts["outtmpl"] = str(job / "media.%(ext)s")
        if p.proxy:
            opts["proxy"] = p.proxy
        if p.cookie_file:
            opts["cookiefile"] = str(Path(p.cookie_file).expanduser().resolve())
        if p.cookies_from_browser:
            opts["cookiesfrombrowser"] = (p.cookies_from_browser,)
        if self.progress:
            opts["progress_hooks"] = [self.progress]
            opts["postprocessor_hooks"] = [self.progress]
        return opts

    def _run(self, url: str, opts: dict, *, download: bool = False) -> dict:
        import yt_dlp
        from yt_dlp.utils import DownloadError

        validate_url(url)
        parsed = urlsplit(url)
        if opts.get("noplaylist", True) and (
            parsed.path.startswith(("/playlist", "/channel/", "/c/", "/user/", "/@", "/feed/"))
            or (parsed.path == "/watch" and not parse_qs(parsed.query).get("v"))
        ):
            raise MediaError("Playlist/channel URLs require --playlist (or client.playlist()).")
        for attempt in range(2):
            options = dict(opts)
            if self._browser_session is not None:
                # Do not reread stale cookies or write the browser's cookies into a user's file.
                options.pop("cookiefile", None)
                options.pop("cookiesfrombrowser", None)
                options["http_headers"] = {
                    **options.get("http_headers", {}),
                    "User-Agent": self._browser_session.user_agent,
                }
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    if self._browser_session is not None:
                        for cookie in self._browser_session.cookies:
                            domain = cookie["domain"]
                            if not (
                                domain.lstrip(".") in {"youtube.com", "youtu.be"}
                                or domain.endswith(".youtube.com")
                            ):
                                continue
                            expires = cookie.get("expires", -1)
                            ydl.cookiejar.set_cookie(
                                http.cookiejar.Cookie(
                                    version=0,
                                    name=cookie["name"],
                                    value=cookie["value"],
                                    port=None,
                                    port_specified=False,
                                    domain=domain,
                                    domain_specified=domain.startswith("."),
                                    domain_initial_dot=domain.startswith("."),
                                    path=cookie.get("path", "/"),
                                    path_specified=True,
                                    secure=cookie.get("secure", True),
                                    expires=int(expires) if expires > 0 else None,
                                    discard=expires <= 0,
                                    comment=None,
                                    comment_url=None,
                                    rest={"HttpOnly": None} if cookie.get("httpOnly") else {},
                                )
                            )
                    info = ydl.extract_info(url, download=download)
                if not info:
                    raise MediaError("No media information returned.")
                return info
            except DownloadError as exc:
                detail = redact(str(exc))
                if _is_bot_check(str(exc)):
                    if attempt == 0 and self.challenge is not None:
                        self._browser_session = self.challenge(url)
                        if self._browser_session is not None:
                            continue
                    raise BotCheckRequired(url, detail) from None
                raise MediaError(detail) from None
            except OSError as exc:
                raise MediaError(redact(str(exc))) from None

    def probe(self, url: str) -> dict:
        return self._run(url, self._options())

    def list_subtitles(self, url: str) -> dict:
        info = self.probe(url)
        return {
            "uploaded": sorted(info.get("subtitles") or {}),
            "automatic": sorted(info.get("automatic_captions") or {}),
        }

    def playlist(self, url: str, *, items: str | None = None) -> list[str]:
        opts = self._options()
        opts.update(noplaylist=False, extract_flat="in_playlist", playlist_items=items)
        info = self._run(url, opts)
        if "entries" not in info:
            return [url]
        urls = []
        for entry in info["entries"]:
            if not entry:
                raise MediaError("Playlist contains an unavailable entry.")
            video_id = entry.get("id", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                raise MediaError("Invalid video ID in playlist.")
            urls.append(f"https://www.youtube.com/watch?v={video_id}")
        if not urls:
            raise MediaError("Playlist selection contains no available videos.")
        return urls

    def _media(self, url: str, kind: str, opts: dict, extension: str | None) -> MediaResult:
        job = self._job(url, kind)
        options = self._options(job)
        options.update(opts)
        options["ffmpeg_location"] = runtime.ffmpeg_path(self.ffmpeg_location)
        # Capture the final artifact after merging/conversion, never guess among intermediate files.
        paths = []
        options["post_hooks"] = [paths.append]
        info = self._run(url, options, download=True)
        if info.get("has_drm"):
            raise MediaError("DRM-protected media is unsupported.")
        path = Path(paths[-1]) if paths else Path(info.get("filepath") or "")
        if not path.is_absolute():
            path = Path.cwd() / path
        self._safe(path)
        if not path.is_file() or path.stat().st_size == 0:
            raise MediaError("Download did not produce a nonempty final media file.")
        if extension and path.suffix != f".{extension}":
            raise MediaError(f"Expected {extension} output; received {path.suffix}.")
        keys = (
            "id",
            "title",
            "description",
            "channel",
            "uploader",
            "duration",
            "upload_date",
            "webpage_url",
            "chapters",
            "width",
            "height",
            "fps",
            "vcodec",
            "acodec",
            "format_id",
            "abr",
            "language",
        )
        metadata = self._safe(job / "metadata.json")
        metadata.write_text(
            json.dumps({k: info.get(k) for k in keys}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return MediaResult(path, info.get("id", ""), info.get("title", ""), metadata)

    def download_video(
        self,
        url: str,
        *,
        container: str = "mkv",
        quality: str | int = "best",
        format_id: str | None = None,
        thumbnail: bool = False,
    ) -> MediaResult:
        selector = video_selector(container, quality)
        if format_id:
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+", format_id):
                raise ValueError("Use format IDs from `formats`, e.g. 137+140.")
            selector = format_id
        return self._media(
            url,
            f"video:{container}:{quality}:{format_id}:{thumbnail}",
            {
                "format": selector,
                "merge_output_format": container,
                "writethumbnail": thumbnail,
                "postprocessors": [
                    {"key": "FFmpegVideoRemuxer", "preferedformat": container},
                    {"key": "FFmpegMetadata"},
                ],
            },
            container,
        )

    def download_audio(
        self,
        url: str,
        *,
        codec: str = "best",
        bitrate_kbps: int = 192,
        vbr_quality: int | None = None,
    ) -> MediaResult:
        if codec not in {"best", "mp3", "m4a", "opus", "flac", "wav"}:
            raise ValueError("Unsupported audio format.")
        if bitrate_kbps not in {64, 96, 128, 160, 192, 224, 256, 320}:
            raise ValueError("Choose an audio bitrate from 64, 96, 128, 160, 192, 224, 256, 320.")
        if vbr_quality is not None and (codec != "mp3" or not 0 <= vbr_quality <= 9):
            raise ValueError("MP3 VBR quality must be 0 (best) through 9.")
        processors = []
        if codec != "best":
            processors.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": codec,
                    "preferredquality": str(
                        vbr_quality if vbr_quality is not None else bitrate_kbps
                    ),
                }
            )
        processors.append({"key": "FFmpegMetadata"})
        return self._media(
            url,
            f"audio:{codec}:{bitrate_kbps}:{vbr_quality}",
            {
                "format": "ba",
                "postprocessors": processors,
            },
            None if codec == "best" else {"opus": "opus"}.get(codec, codec),
        )

    def download_mp3(
        self, url: str, *, bitrate_kbps: int = 320, vbr_quality: int | None = None
    ) -> MediaResult:
        return self.download_audio(
            url, codec="mp3", bitrate_kbps=bitrate_kbps, vbr_quality=vbr_quality
        )

    def download_subtitles(
        self,
        url: str,
        *,
        language: str = "en",
        formats=("srt", "vtt", "ttml", "txt", "json"),
        prefer_manual: bool = True,
    ) -> SubtitleResult:
        formats = tuple(dict.fromkeys(formats))
        if not formats or set(formats) - {"srt", "vtt", "ttml", "txt", "json"}:
            raise ValueError("Choose subtitle formats from srt, vtt, ttml, txt, json.")
        info = self.probe(url)
        catalogs = [
            ("uploaded", info.get("subtitles") or {}),
            ("automatic", info.get("automatic_captions") or {}),
        ]
        if not prefer_manual:
            catalogs.reverse()
        selected = next(
            (
                (kind, picked)
                for kind, catalog in catalogs
                if (picked := _pick_language(language, catalog))
            ),
            None,
        )
        if not selected:
            raise SubtitleUnavailable(
                f"No captions for {language!r}; available: "
                + ", ".join(sorted(set().union(*(c for _, c in catalogs))))
            )
        kind, selected_language = selected
        job = self._job(url, f"captions:{kind}:{selected_language}")
        opts = self._options(job)
        opts.update(
            skip_download=True,
            writesubtitles=kind == "uploaded",
            writeautomaticsub=kind == "automatic",
            subtitleslangs=[re.escape(selected_language)],
            subtitlesformat="vtt/srt/ttml",
        )
        result = self._run(url, opts, download=True)
        track = (result.get("requested_subtitles") or {}).get(selected_language) or {}
        if not track.get("filepath"):
            raise SubtitleUnavailable("Caption extraction produced no subtitle file.")
        source = self._safe(Path(track["filepath"]).absolute())
        if not source.is_file():
            raise SubtitleUnavailable("Caption file is missing.")
        try:
            outputs = convert_captions(source, formats)
        except (ValueError, OSError) as exc:
            raise MediaError(f"Caption conversion failed: {redact(str(exc))}") from None
        return SubtitleResult(selected_language, kind, outputs)

    def download_transcript(self, url: str, *, language: str = "en") -> SubtitleResult:
        return self.download_subtitles(url, language=language, formats=("txt", "json"))
