"""Explicit dependency management; no downloaded shell scripts or system changes."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PACKAGES = [
    "aiohttp>=3.10,<4",
    "yt-dlp[default]>=2026.10.21",
    "pysubs2>=1.9,<2",
    "imageio-ffmpeg>=0.6,<1",
]


def _installer() -> list[str]:
    command = [sys.executable, "-m", "pip", "install"]
    if subprocess.run(
        [sys.executable, "-m", "pip", "--version"], capture_output=True, check=False
    ).returncode:
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("Install pip or uv, then run `python -m ultradownloader setup`.")
        command = [uv, "pip", "install", "--python", sys.executable]
    return command


def install_browser() -> None:
    """Only needed when the user is handed a browser challenge."""
    subprocess.run([*_installer(), "playwright>=1.51,<2"], check=True)


def install(*, update: bool = False, nightly: bool = False) -> None:
    """Install into the interpreter running this command, including uv environments."""
    command = _installer()
    if update:
        command.append("--upgrade")
    if nightly:
        command.append("--pre")
    subprocess.run([*command, *PACKAGES], check=True)
    managed_deno = False
    try:
        importlib.metadata.version("deno")
        managed_deno = True
    except importlib.metadata.PackageNotFoundError:
        pass
    if not javascript_runtime() or (update and managed_deno):
        subprocess.run([*command, "deno>=2.3"], check=True)


def javascript_runtime() -> dict:
    for name, minimum in (("deno", (2, 3)), ("node", (22, 0))):
        candidate = shutil.which(name)
        if not candidate:
            candidate_path = Path(sys.executable).parent / (
                name + (".exe" if os.name == "nt" else "")
            )
            candidate = str(candidate_path) if candidate_path.is_file() else None
        if candidate:
            import re

            result = subprocess.run(
                [candidate, "--version"], capture_output=True, text=True, timeout=10, check=False
            )
            version = re.search(r"(\d+)\.(\d+)", result.stdout)
            if result.returncode == 0 and version and tuple(map(int, version.groups())) >= minimum:
                return {name: {"path": candidate}}
    return {}


def ffmpeg_path(location: str | None = None) -> str:
    if location:
        path = Path(location).expanduser()
        if path.is_dir():
            path /= "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        if not path.is_file():
            raise RuntimeError(f"FFmpeg does not exist: {path}")
        return str(path.resolve())
    if found := shutil.which("ffmpeg"):
        return found
    from imageio_ffmpeg import get_ffmpeg_exe

    return get_ffmpeg_exe()


def doctor(location: str | None = None) -> dict:
    versions = {}
    for package in ("yt-dlp", "yt-dlp-ejs", "pysubs2", "imageio-ffmpeg"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    try:
        binary = ffmpeg_path(location)
        result = subprocess.run(
            [binary, "-version"], capture_output=True, text=True, timeout=10, check=False
        )
        versions["ffmpeg"] = result.stdout.splitlines()[0] if result.returncode == 0 else None
        versions["ffmpeg_path"] = binary
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError):
        versions["ffmpeg"] = None
    versions["javascript"] = javascript_runtime()
    versions["python"] = sys.version.split()[0]
    versions["ready"] = all(
        versions.get(p) for p in ("yt-dlp", "yt-dlp-ejs", "pysubs2", "ffmpeg", "javascript")
    )
    return versions


def ensure(*, auto_install: bool = True, auto_update: bool = False) -> None:
    """Auto updates are opt-in, at most once daily and before importing yt-dlp."""
    environment = hashlib.sha256(sys.executable.encode()).hexdigest()[:16]
    state_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "UltraDownloader"
    state_root /= environment
    stamp = state_root / "updates.json"
    due = True
    try:
        due = time.time() - json.loads(stamp.read_text())["checked"] > 86400
    except (OSError, ValueError, KeyError, TypeError):
        pass
    missing = False
    for package in ("yt-dlp", "yt-dlp-ejs", "pysubs2", "imageio-ffmpeg"):
        try:
            importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            missing = True
    missing = missing or not javascript_runtime()
    try:
        version = importlib.metadata.version("yt-dlp")
        missing = missing or tuple(int(p) for p in version.split(".")[:3]) < (2026, 10, 21)
        # EJS must match the extractor, rather than an independently chosen latest release.
        for requirement in importlib.metadata.requires("yt-dlp") or []:
            if requirement.startswith("yt-dlp-ejs=="):
                expected = requirement.split("==", 1)[1].split(";", 1)[0].strip()
                missing = missing or importlib.metadata.version("yt-dlp-ejs") != expected
    except (importlib.metadata.PackageNotFoundError, ValueError):
        missing = True
    if missing and not auto_install:
        raise RuntimeError("Dependencies missing. Run `python -m ultradownloader setup`.")
    if missing or (auto_update and due):
        install(update=auto_update)
        if auto_update:
            state_root.mkdir(parents=True, exist_ok=True)
            stamp.write_text(json.dumps({"checked": time.time()}), encoding="utf-8")
