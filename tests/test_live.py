"""Explicit live canary; the normal suite never hits YouTube."""

import os

import pytest

from ultradownloader import LocalYouTube


@pytest.mark.skipif(
    not os.environ.get("ULTRADOWNLOADER_TEST_URL"),
    reason="No authorized live canary URL configured",
)
def test_authorized_youtube_download(tmp_path):
    from ultradownloader.runtime import ensure

    ensure(auto_install=False)
    client = LocalYouTube(tmp_path)
    result = client.download_video(os.environ["ULTRADOWNLOADER_TEST_URL"], quality=360)
    assert result.path.is_file() and result.path.stat().st_size > 0
