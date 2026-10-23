"""YouTube CLI and backwards-compatible HTTP routing."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import asdict

from . import runtime
from .youtube import LocalYouTube, MediaError, NetworkPolicy, is_youtube_url, redact

COMMANDS = {
    "video",
    "audio",
    "mp3",
    "subtitles",
    "transcript",
    "all",
    "info",
    "formats",
    "setup",
    "update",
    "doctor",
    "http",
}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="UltraDownloader: YouTube media, captions and HTTP files"
    )
    subs = ap.add_subparsers(dest="command", required=True)
    for name in ("setup", "update", "doctor"):
        p = subs.add_parser(name, help=f"{name.capitalize()} managed dependencies and runtimes")
        if name == "update":
            p.add_argument("--nightly", action="store_true", help="Opt into upstream prereleases")
    for name in sorted(COMMANDS - {"setup", "update", "doctor", "http"}):
        p = subs.add_parser(name)
        p.add_argument("urls", nargs="+", help="HTTPS YouTube videos, Shorts or playlists")
        p.add_argument("-o", "--output", default="downloads", help="Output directory")
        p.add_argument("--playlist", action="store_true", help="Expand playlists/channel entries")
        p.add_argument("--playlist-items", help="yt-dlp playlist selection, e.g. 1:10 or 1,3,5")
        p.add_argument("--cookies", help="Private Netscape cookie file")
        p.add_argument("--cookies-from-browser", help="Explicitly load cookies from this browser")
        p.add_argument(
            "--no-browser-handoff",
            action="store_true",
            help="Report bot checks without opening a browser or waiting for input",
        )
        p.add_argument("--proxy")
        p.add_argument("--fragments", type=int, default=2)
        p.add_argument("--retries", type=int, default=5)
        p.add_argument("--timeout", type=float, default=30)
        p.add_argument("--rate-limit", type=int, help="Media bytes per second")
        p.add_argument("--sleep", type=float, default=5, help="Minimum seconds before downloads")
        p.add_argument("--ffmpeg-location", help="FFmpeg binary or directory")
        p.add_argument("--no-auto-install", action="store_true")
        p.add_argument(
            "--auto-update", action="store_true", help="Update dependencies at most daily"
        )
        p.add_argument("--quiet", action="store_true", help="Hide progress; keep results/errors")
        if name in {"video", "all"}:
            p.add_argument("--container", choices=("mkv", "mp4", "webm"), default="mkv")
            p.add_argument(
                "--quality", default="best", help="best or resolution ceiling, e.g. 2160"
            )
            p.add_argument("--format", dest="format_id", help="Exact format IDs, e.g. 137+140")
            p.add_argument("--thumbnail", action="store_true", help="Save cover image sidecar")
        if name in {"audio", "mp3", "all"}:
            p.add_argument(
                "--audio-format",
                choices=("best", "mp3", "m4a", "opus", "flac", "wav"),
                default="best" if name == "audio" else "mp3",
            )
            p.add_argument(
                "--bitrate",
                type=int,
                default=192 if name == "audio" else 320,
                help="Target audio kbps (audio: 192; MP3: 320)",
            )
            p.add_argument("--vbr-quality", type=int, help="MP3 VBR: 0 best through 9 smallest")
        if name in {"subtitles", "transcript", "all"}:
            p.add_argument("--language", default="en", help="Caption language, e.g. en or hi")
            p.add_argument("--prefer-auto", action="store_true")
            p.add_argument("--subtitle-formats", default="srt,vtt,ttml,txt,json")
    return ap


def _progress():
    last = 0.0

    def emit(event):
        nonlocal last
        now = time.monotonic()
        status = event.get("status")
        if status == "downloading":
            if now - last < 0.5:
                return
            last = now
            done = event.get("downloaded_bytes", 0)
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            amount = f"{done / total:.1%}" if total else f"{done / 1048576:.1f} MiB"
            speed = (event.get("speed") or 0) / 1048576
            print(
                f"Downloading {amount} | {speed:.2f} MiB/s | ETA {event.get('eta')}s",
                file=sys.stderr,
            )
        elif status in {"started", "processing", "finished"}:
            print(f"{event.get('postprocessor', 'Download')}: {status}", file=sys.stderr)

    return emit


def _print_result(value):
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Legacy HTTP usage keeps its original parser and flags.
    if argv and (argv[0] == "http" or (argv[0] not in COMMANDS and not argv[0].startswith("-"))):
        if argv[0] == "http":
            argv.pop(0)
        elif is_youtube_url(argv[0]):
            argv.insert(0, "video")
        else:
            from get import main as http_main

            asyncio.run(http_main(argv))
            return 0
        if argv and argv[0] not in COMMANDS:
            from get import main as http_main

            asyncio.run(http_main(argv))
            return 0
    ap = parser()
    args = ap.parse_args(argv)
    try:
        if args.command in {"setup", "update"}:
            runtime.install(
                update=args.command == "update", nightly=getattr(args, "nightly", False)
            )
            _print_result(runtime.doctor())
            return 0
        if args.command == "doctor":
            report = runtime.doctor()
            _print_result(report)
            return 0 if report["ready"] else 1
        if args.playlist_items and not args.playlist:
            ap.error("--playlist-items requires --playlist")
        # Validate user settings before initiating dependency downloads.
        network = NetworkPolicy(
            proxy=args.proxy,
            cookie_file=args.cookies,
            cookies_from_browser=args.cookies_from_browser,
            fragments=args.fragments,
            retries=args.retries,
            timeout=args.timeout,
            rate_limit=args.rate_limit,
            sleep=args.sleep,
        )
        from .youtube import validate_url

        for url in args.urls:
            validate_url(url)
        runtime.ensure(auto_install=not args.no_auto_install, auto_update=args.auto_update)
        from .browser import BrowserHandoff

        challenge = None
        if not args.no_browser_handoff and sys.stdin.isatty():
            challenge = BrowserHandoff(auto_install=not args.no_auto_install, proxy=args.proxy)
        client = LocalYouTube(
            args.output,
            network=network,
            ffmpeg_location=args.ffmpeg_location,
            progress=None if args.quiet else _progress(),
            challenge=challenge,
        )
        failures = 0
        for url in args.urls:
            try:
                urls = client.playlist(url, items=args.playlist_items) if args.playlist else [url]
            except (MediaError, ValueError) as exc:
                print(f"Error: {redact(str(exc))}", file=sys.stderr)
                failures += 1
                continue
            for video in urls:
                try:
                    command = args.command
                    if command in {"info", "formats"}:
                        info = client.probe(video)
                        if command == "formats":
                            keys = (
                                "format_id",
                                "ext",
                                "width",
                                "height",
                                "fps",
                                "vcodec",
                                "acodec",
                                "abr",
                                "tbr",
                                "filesize",
                                "filesize_approx",
                                "dynamic_range",
                            )
                            _print_result(
                                [
                                    {k: f.get(k) for k in keys}
                                    for f in info.get("formats", [])
                                    if not f.get("has_drm")
                                ]
                            )
                        else:
                            keys = (
                                "id",
                                "title",
                                "description",
                                "channel",
                                "duration",
                                "chapters",
                                "upload_date",
                                "webpage_url",
                                "live_status",
                            )
                            _print_result(
                                {
                                    **{k: info.get(k) for k in keys},
                                    "subtitles": sorted(info.get("subtitles") or {}),
                                    "automatic_captions": sorted(
                                        info.get("automatic_captions") or {}
                                    ),
                                }
                            )
                    if command in {"video", "all"}:
                        _print_result(
                            client.download_video(
                                video,
                                container=args.container,
                                quality=args.quality,
                                format_id=args.format_id,
                                thumbnail=args.thumbnail,
                            )
                        )
                    if command in {"audio", "mp3", "all"}:
                        _print_result(
                            client.download_audio(
                                video,
                                codec="mp3" if command == "mp3" else args.audio_format,
                                bitrate_kbps=args.bitrate,
                                vbr_quality=args.vbr_quality,
                            )
                        )
                    if command in {"subtitles", "transcript", "all"}:
                        _print_result(
                            client.download_subtitles(
                                video,
                                language=args.language,
                                formats=("txt", "json")
                                if command == "transcript"
                                else tuple(args.subtitle_formats.split(",")),
                                prefer_manual=not args.prefer_auto,
                            )
                        )
                except (MediaError, ValueError, OSError, RuntimeError) as exc:
                    print(f"Error: {redact(str(exc))}", file=sys.stderr)
                    failures += 1
        return 1 if failures else 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Error: {redact(str(exc))}", file=sys.stderr)
        return 1


def entrypoint():
    try:
        return main()
    except KeyboardInterrupt:
        print("Stopped; partial downloads retained for resume.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Error: {redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(entrypoint())
