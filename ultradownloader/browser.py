"""Visible, user-driven YouTube verification. Never attempts to solve challenges."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import runtime


@dataclass(frozen=True)
class BrowserSession:
    cookies: list[dict] = field(repr=False)
    user_agent: str


def _system_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if root := os.environ.get(variable):
            candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
    return next((str(path) for path in candidates if path.is_file()), None)


def _proxy_options(proxy: str | None) -> dict:
    if not proxy:
        return {}
    parsed = urlsplit(proxy)
    scheme = "socks5" if parsed.scheme == "socks5h" else parsed.scheme
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    server = f"{scheme}://{hostname}" + (f":{parsed.port}" if parsed.port else "")
    settings = {"server": server}
    if parsed.username:
        settings.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
    return {"proxy": settings}


class BrowserHandoff:
    def __init__(self, *, auto_install: bool = True, proxy: str | None = None):
        self.auto_install = auto_install
        self.proxy = proxy

    def __call__(self, url: str) -> BrowserSession:
        from .youtube import MediaError, redact, validate_url

        validate_url(url)
        print(
            "\nYouTube requires a bot check. Opening a browser for you to complete it.\n"
            "Clear the check or sign in there, then return here. "
            "Only this dedicated YouTube session will be used for the retry.",
            file=sys.stderr,
        )
        try:
            import playwright.sync_api as api
        except ImportError:
            if not self.auto_install:
                raise MediaError(
                    "Browser handoff needs Playwright. Install with "
                    "`python -m pip install 'playwright>=1.51,<2'`, or enable auto-install."
                ) from None
            runtime.install_browser()
            import playwright.sync_api as api

        # Never attach to the user's normal browser or read its unrelated cookies.
        profile = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache"))
        profile = profile / "UltraDownloader" / "verification-profile"
        if any(part.is_symlink() for part in (profile, *profile.parents)):
            raise MediaError("Verification profile cannot be a symlink.")
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            profile.chmod(0o700)
        try:
            with api.sync_playwright() as playwright:
                options = {"headless": False, **_proxy_options(self.proxy)}
                if executable := _system_chrome():
                    options["executable_path"] = executable
                elif not Path(playwright.chromium.executable_path).is_file():
                    if not self.auto_install:
                        raise MediaError(
                            "No browser found. Install Chrome or run "
                            "`python -m playwright install chromium`."
                        )
                    subprocess.run(
                        [sys.executable, "-m", "playwright", "install", "chromium"], check=True
                    )
                context = playwright.chromium.launch_persistent_context(str(profile), **options)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    except api.TimeoutError:
                        print(
                            "Page is still loading; complete the check in the browser.",
                            file=sys.stderr,
                        )
                    page.bring_to_front()
                    print(f"Verification page: {url}", file=sys.stderr)
                    try:
                        answer = input("After clearing it, press Enter to retry (or type cancel): ")
                    except EOFError:
                        raise MediaError("Verification cancelled: terminal input closed.") from None
                    if answer.strip().casefold() in {"cancel", "q", "quit", "n"}:
                        raise MediaError("Verification cancelled; partial downloads retained.")
                    # Filter domains twice: these are the only credentials shared with yt-dlp.
                    cookies = [
                        c
                        for c in context.cookies(
                            [
                                "https://www.youtube.com/",
                                "https://youtube.com/",
                                "https://youtu.be/",
                            ]
                        )
                        if c["domain"].lstrip(".") in {"youtube.com", "youtu.be"}
                        or c["domain"].endswith(".youtube.com")
                    ]
                    if not cookies:
                        raise MediaError(
                            "Browser returned no YouTube cookies; verification was not saved."
                        )
                    return BrowserSession(cookies, page.evaluate("navigator.userAgent"))
                finally:
                    context.close()
        except api.Error as exc:
            raise MediaError(
                "Could not complete browser verification. Close any other UltraDownloader "
                f"verification window and try again. {redact(str(exc))}"
            ) from None
