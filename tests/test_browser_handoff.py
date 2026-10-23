import http.cookiejar
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from yt_dlp.utils import DownloadError

from ultradownloader import BotCheckRequired, LocalYouTube, MediaError, browser, cli, runtime
from ultradownloader.browser import BrowserHandoff, BrowserSession, _proxy_options
from ultradownloader.youtube import _is_bot_check

URL = "https://www.youtube.com/watch?v=BaW_jenozKc"
COOKIE = {
    "domain": ".youtube.com",
    "name": "session",
    "value": "private-value",
    "path": "/",
    "expires": -1,
    "secure": True,
    "httpOnly": True,
}


@pytest.mark.parametrize(
    "message",
    [
        "Sign in to confirm you're not a bot",
        "Sign in to confirm you’re not a bot",
        "Our systems detected unusual traffic",
        "CAPTCHA required",
    ],
)
def test_bot_check_classification(message):
    assert _is_bot_check(message)


@pytest.mark.parametrize(
    "message",
    [
        "HTTP Error 403: Forbidden",
        "HTTP Error 429: Too Many Requests",
        "Private video. Sign in",
        "Video unavailable",
        "Disk full",
    ],
)
def test_ordinary_errors_do_not_launch_browser(message):
    assert not _is_bot_check(message)


def fake_extractor(monkeypatch, *, always_blocked=False):
    import yt_dlp

    clients = []

    def factory(options):
        client = MagicMock()
        client.cookiejar = http.cookiejar.CookieJar()
        client.__enter__.return_value = client
        clients.append((client, options))

        def extract(url, download):
            if len(clients) == 1 or always_blocked:
                raise DownloadError("Sign in to confirm you're not a bot")
            return {"id": "fixture", "title": "Success"}

        client.extract_info.side_effect = extract
        return client

    monkeypatch.setattr(yt_dlp, "YoutubeDL", factory)
    return clients


def test_handoff_retries_once_and_reuses_session(monkeypatch, tmp_path):
    clients = fake_extractor(monkeypatch)
    handoff = MagicMock(
        return_value=BrowserSession([COOKIE, {**COOKIE, "domain": ".evil.test"}], "Browser UA")
    )
    client = LocalYouTube(tmp_path, challenge=handoff)
    opts = {"noplaylist": True, "cookiefile": "old.txt", "cookiesfrombrowser": ("firefox",)}
    assert client._run(URL, opts)["title"] == "Success"
    handoff.assert_called_once_with(URL)
    assert len(clients) == 2
    second, options = clients[1]
    assert "cookiefile" not in options and "cookiesfrombrowser" not in options
    assert options["http_headers"]["User-Agent"] == "Browser UA"
    cookies = list(second.cookiejar)
    assert len(cookies) == 1 and cookies[0].domain == ".youtube.com"
    assert cookies[0].discard and cookies[0].expires is None
    assert client._run(URL, opts)["title"] == "Success"
    handoff.assert_called_once()
    assert opts["cookiefile"] == "old.txt"


def test_unsolved_check_stops_after_one_handoff(monkeypatch, tmp_path):
    clients = fake_extractor(monkeypatch, always_blocked=True)
    handoff = MagicMock(return_value=BrowserSession([COOKIE], "UA"))
    with pytest.raises(BotCheckRequired) as error:
        LocalYouTube(tmp_path, challenge=handoff)._run(URL, {})
    assert error.value.url == URL
    assert len(clients) == 2
    handoff.assert_called_once()


def test_no_handler_exposes_typed_challenge(monkeypatch, tmp_path):
    clients = fake_extractor(monkeypatch)
    with pytest.raises(BotCheckRequired, match="interactive terminal"):
        LocalYouTube(tmp_path)._run(URL, {})
    assert len(clients) == 1


def fake_browser(monkeypatch, tmp_path):
    api = ModuleType("playwright.sync_api")
    api.Error = type("BrowserError", (Exception,), {})
    api.TimeoutError = type("BrowserTimeout", (api.Error,), {})
    manager = MagicMock()
    playwright = manager.__enter__.return_value
    context = playwright.chromium.launch_persistent_context.return_value
    page = MagicMock()
    context.pages = [page]
    context.cookies.return_value = [COOKIE, {**COOKIE, "domain": ".other.test"}]
    page.evaluate.return_value = "Browser UA"
    api.sync_playwright = lambda: manager
    package = ModuleType("playwright")
    package.sync_api = api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(browser, "_system_chrome", lambda: "chrome")
    monkeypatch.setattr("builtins.input", lambda _: "")
    return api, playwright, context, page


def test_visible_browser_exports_only_youtube_and_closes(monkeypatch, tmp_path, capsys):
    _, playwright, context, page = fake_browser(monkeypatch, tmp_path)
    session = BrowserHandoff()(URL)
    assert session.cookies == [COOKIE] and session.user_agent == "Browser UA"
    call = playwright.chromium.launch_persistent_context.call_args
    assert call.kwargs["headless"] is False
    assert "verification-profile" in call.args[0]
    page.goto.assert_called_once_with(URL, wait_until="domcontentloaded", timeout=45000)
    context.close.assert_called_once()
    assert "private-value" not in capsys.readouterr().err
    assert "private-value" not in repr(session)


@pytest.mark.parametrize("cancel", ["cancel", "q", "quit", "n"])
def test_user_can_cancel_and_browser_closes(monkeypatch, tmp_path, cancel):
    _, _, context, _ = fake_browser(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: cancel)
    with pytest.raises(MediaError, match="cancelled"):
        BrowserHandoff()(URL)
    context.close.assert_called_once()


def test_navigation_timeout_still_allows_user_to_clear(monkeypatch, tmp_path):
    api, _, context, page = fake_browser(monkeypatch, tmp_path)
    page.goto.side_effect = api.TimeoutError("loading")
    assert BrowserHandoff()(URL).cookies == [COOKIE]
    context.close.assert_called_once()


def test_closed_browser_reports_clear_error(monkeypatch, tmp_path):
    api, _, context, page = fake_browser(monkeypatch, tmp_path)
    page.evaluate.side_effect = api.Error("Target closed")
    with pytest.raises(MediaError, match="Could not complete browser verification"):
        BrowserHandoff()(URL)
    context.close.assert_called_once()


def test_proxy_uses_same_egress_without_credentials_in_server():
    assert _proxy_options("http://user:password@localhost:8080") == {
        "proxy": {"server": "http://localhost:8080", "username": "user", "password": "password"}
    }


@pytest.mark.parametrize(
    "interactive,disabled,enabled",
    [(True, False, True), (False, False, False), (True, True, False)],
)
def test_cli_only_installs_handler_for_interactive_runs(
    monkeypatch, interactive, disabled, enabled
):
    monkeypatch.setattr(runtime, "ensure", lambda **_: None)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: interactive))
    seen = []

    def probe(self, url):
        seen.append(self.challenge)
        return {"title": "fixture"}

    monkeypatch.setattr(LocalYouTube, "probe", probe)
    assert cli.main(["info", URL] + (["--no-browser-handoff"] if disabled else [])) == 0
    assert (seen[0] is not None) == enabled
