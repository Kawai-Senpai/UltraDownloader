import asyncio
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import pytest

from get import Progress, default_name_from_url, download_segmented, fetch_range, save_meta

DATA = bytes(range(256)) * 128


@contextmanager
def server(*, status=206, invalid_range=False):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            start, end = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
            self.send_response(status)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header(
                "Content-Range", "bad" if invalid_range else f"bytes {start}-{end}/{len(DATA)}"
            )
            self.end_headers()
            self.wfile.write(DATA[start : end + 1])

        def log_message(self, *args):
            pass

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}/file.bin"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


@pytest.mark.parametrize(
    "old_chunk,old_etag,exists",
    [(4096, '"old"', True), (1024, '"current"', True), (4096, '"current"', False)],
)
def test_stale_resume_never_skips_bytes(tmp_path, old_chunk, old_etag, exists):
    output = str(tmp_path / "output.bin")
    if exists:
        (tmp_path / "output.bin").write_bytes(b"x" * len(DATA))
    with server() as url:
        save_meta(
            output,
            {
                "url": url,
                "size": len(DATA),
                "chunk_size": old_chunk,
                "etag": old_etag,
                "done": [0, 1],
            },
        )
        asyncio.run(
            download_segmented(
                url, output, len(DATA), 4096, 2, 5, 0, {"etag": '"current"', "last_modified": None}
            )
        )
    assert (tmp_path / "output.bin").read_bytes() == DATA


@pytest.mark.parametrize("status,invalid", [(200, False), (416, False), (206, True)])
def test_range_corruption_rejected_before_write(tmp_path, status, invalid):
    output = tmp_path / "output.bin"
    output.write_bytes(b"x" * len(DATA))

    async def attempt(url):
        async with aiohttp.ClientSession() as session:
            await fetch_range(session, url, 0, 1023, str(output), Progress(len(DATA)), 5, 0, 0, {})

    with server(status=status, invalid_range=invalid) as url, pytest.raises(RuntimeError):
        asyncio.run(attempt(url))
    assert output.read_bytes() == b"x" * len(DATA)


def test_encoded_filename_cannot_escape_directory():
    assert default_name_from_url("https://example.com/%2e%2e%2fsecret.bin") == "secret.bin"
    assert default_name_from_url("https://example.com/%2e%2e%5csecret.bin") == "secret.bin"
