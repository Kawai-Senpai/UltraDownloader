import importlib.metadata

from ultradownloader import cli, runtime
from ultradownloader.youtube import LocalYouTube, MediaError

URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


def test_cli_reports_failure_and_continues(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "ensure", lambda **_: None)
    calls = []

    def probe(self, url):
        calls.append(url)
        if len(calls) == 1:
            raise MediaError("Unavailable")
        return {"title": "Second video"}

    monkeypatch.setattr(LocalYouTube, "probe", probe)
    assert cli.main(["info", URL, URL]) == 1
    captured = capsys.readouterr()
    assert "Unavailable" in captured.err and "Second video" in captured.out


def test_bare_youtube_url_routes_to_video(monkeypatch, tmp_path):
    from ultradownloader.youtube import MediaResult

    monkeypatch.setattr(runtime, "ensure", lambda **_: None)
    calls = []

    def video(self, url, **kwargs):
        calls.append((url, kwargs))
        return MediaResult(tmp_path / "media.mkv", "id", "title", tmp_path / "metadata.json")

    monkeypatch.setattr(LocalYouTube, "download_video", video)
    assert cli.main([URL, "--quality", "1080"]) == 0
    assert calls[0][1]["quality"] == "1080"


def test_daily_auto_update_scoped_to_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(runtime, "javascript_runtime", lambda: {"node": {"path": "node"}})
    calls = []
    monkeypatch.setattr(runtime, "install", lambda **kwargs: calls.append(kwargs))
    runtime.ensure(auto_update=True)
    runtime.ensure(auto_update=True)
    assert calls == [{"update": True}]
    assert list(tmp_path.rglob("updates.json"))


def test_missing_dependencies_without_auto_install(monkeypatch):
    import pytest

    def missing(_):
        raise importlib.metadata.PackageNotFoundError()

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(RuntimeError, match="Dependencies missing"):
        runtime.ensure(auto_install=False)


def test_install_uses_current_interpreter_and_runtime_fallback(monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(runtime, "javascript_runtime", dict)
    runtime.install(update=True)
    assert calls[1][:3] == [runtime.sys.executable, "-m", "pip"]
    assert "--upgrade" in calls[1]
    assert calls[2][-1] == "deno>=2.3"
