#!/usr/bin/env python3
if __name__ == "__main__":
    # Bootstrap/setup must work before optional HTTP dependencies are installed.
    from ultradownloader.cli import entrypoint
    raise SystemExit(entrypoint())

import argparse
import asyncio
import aiohttp
import os
import sys
import time
import math
import json
import hashlib
from urllib.parse import urlparse, unquote
import re
# ------------- helpers

def human_to_bytes(s: str) -> int:
    """
    Accepts: 8m, 8mb, 8MB, 8MiB, 8g, 512k, 1024, etc.
    Returns bytes as int. Raises ValueError on bad input.
    """
    s = s.strip()
    m = re.fullmatch(r"(?i)\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b?|)\s*", s)
    if not m:
        raise ValueError(f"Invalid size: {s}")
    val = float(m.group(1))
    unit = m.group(2).lower()

    multipliers = {
        "": 1,
        "b": 1,
        "k": 1024, "kb": 1024, "kib": 1024,
        "m": 1024**2, "mb": 1024**2, "mib": 1024**2,
        "g": 1024**3, "gb": 1024**3, "gib": 1024**3,
        "t": 1024**4, "tb": 1024**4, "tib": 1024**4,
    }
    mul = multipliers.get(unit)
    if mul is None:
        raise ValueError(f"Unknown unit: {unit}")
    return int(val * mul)

def fmt_bytes(n: int) -> str:
    for unit in ["B","KB","MB","GB","TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}"
        n /= 1024

def default_name_from_url(url: str) -> str:
    path = urlparse(url).path
    name = os.path.basename(path) or "download.bin"
    # Decode before stripping separators: %2F and %5C must never create directories.
    name = unquote(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name or "download.bin"

def now() -> float:
    return time.time()

# ------------- metadata for resume

def meta_path(out_path: str) -> str:
    return out_path + ".meta.json"

def load_meta(out_path: str):
    p = meta_path(out_path)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                value = json.load(f)
                return value if isinstance(value, dict) else None
        except Exception:
            return None
    return None

def save_meta(out_path: str, meta: dict):
    temporary = meta_path(out_path) + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(temporary, meta_path(out_path))

# ------------- HTTP capability checks

async def head(session: aiohttp.ClientSession, url: str):
    async with session.head(url, allow_redirects=True) as r:
        r.raise_for_status()
        return r

async def probe_ranges(session: aiohttp.ClientSession, url: str) -> bool:
    # Try a tiny range to verify support
    headers = {"Range": "bytes=0-0"}
    async with session.get(url, headers=headers) as r:
        if r.status == 206:
            return True
        if r.status == 200:
            return False
        r.raise_for_status()
        return False

# ------------- segmented download

class Progress:
    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self._last_print = 0.0
        self._start = now()
        self._lock = asyncio.Lock()

    async def add(self, n: int):
        async with self._lock:
            self.done += n

    def maybe_print(self, prefix=""):
        t = now()
        if t - self._last_print < 0.5:
            return
        self._last_print = t
        speed = self.done / max(1e-6, t - self._start)
        eta = (self.total - self.done) / max(1, speed) if self.total else 0
        eta = max(0, eta)
        line = f"{prefix}{fmt_bytes(self.done)} of {fmt_bytes(self.total)} - {fmt_bytes(speed)}/s - ETA {int(eta)}s"
        print(line, end="\r", flush=True)

async def fetch_range(session, url, start, end, out_path, prog, timeout, max_retries, idx, verify_headers):
    attempt = 0
    backoff = 1.0
    pos = start  # progress within this slice

    while pos <= end:
        headers = {"Range": f"bytes={pos}-{end}"}
        if verify_headers.get("If-Range"):
            headers["If-Range"] = verify_headers["If-Range"]

        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout*2, sock_read=timeout)) as r:
                if r.status != 206:
                    if r.status not in (200, 416):
                        r.raise_for_status()
                    raise RuntimeError(f"Range {idx}: expected HTTP 206, received {r.status}; resource may have changed")
                cr = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", r.headers.get("Content-Range", ""))
                if (not cr or int(cr[1]) != pos or int(cr[2]) != end
                        or int(cr[3]) != prog.total):
                    raise RuntimeError(f"Range {idx}: invalid Content-Range")
                before = pos

                with open(out_path, "r+b") as f:
                    async for chunk in r.content.iter_chunked(1 << 15):
                        if pos > end:
                            break
                        want = min(len(chunk), end - pos + 1)
                        if want <= 0:
                            break
                        f.seek(pos)
                        f.write(chunk[:want])
                        pos += want
                        await prog.add(want)
                        prog.maybe_print()

                if pos == before:
                    raise aiohttp.ClientPayloadError("Range response made no progress")
                attempt = 0  # successful transfer - reset attempt counter
                backoff = 1.0

        except (asyncio.TimeoutError, aiohttp.ClientError):
            print(f"\nChunk {idx} timeout, retrying... (attempt {attempt + 1})")
            attempt += 1
            if attempt > max_retries:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    # final guard - if slice is not fully written, raise so the worker will retry
    if pos <= end:
        got = pos - start
        need = (end - start + 1)
        raise RuntimeError(f"range {idx} incomplete - got {got} of {need} bytes")

async def download_segmented(url, out_path, size, chunk_size, concurrency, timeout, max_retries, validators):
    ranges = []
    pos = 0
    while pos < size:
        end = min(size - 1, pos + chunk_size - 1)
        ranges.append((pos, end))
        pos = end + 1

    # Check for resume meta
    meta = load_meta(out_path)
    done_idx = set()
    etag = validators.get("etag")
    modified = validators.get("last_modified")
    same_resource = bool(meta) and (
        (bool(etag) and not etag.startswith("W/") and meta.get("etag") == etag)
        or (not etag and bool(modified) and meta.get("last_modified") == modified)
    )
    if (same_resource and meta.get("size") == size and meta.get("url") == url
            and meta.get("chunk_size") == chunk_size and os.path.isfile(out_path)
            and os.path.getsize(out_path) == size):
        done_idx = {i for i in meta.get("done", []) if type(i) is int and 0 <= i < len(ranges)}

    # Reset stale metadata before workers start, so old done indices cannot leak back in.
    save_meta(out_path, {"url": url, "size": size, "chunk_size": chunk_size,
                         "etag": etag, "last_modified": modified, "done": sorted(done_idx)})

    # Preallocate file
    if not done_idx:
        with open(out_path, "wb") as f:
            f.truncate(size)

    connector = aiohttp.TCPConnector(limit=concurrency, ssl=None)
    timeout_obj = aiohttp.ClientTimeout(total=timeout*3, connect=timeout, sock_read=timeout)
    headers = {"User-Agent": "UltraDownloader/0.2", "Accept-Encoding": "identity"}
    async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj, headers=headers,
                                     auto_decompress=False) as session:
        prog = Progress(total=size)
        # If resuming, compute already downloaded bytes
        if done_idx:
            already = 0
            for i in done_idx:
                s, e = ranges[i]
                already += e - s + 1
            await prog.add(already)

        # Prepare validators for If-Range
        if_range = (etag if etag and not etag.startswith("W/") else modified) or ""
        verify_headers = {"If-Range": if_range} if if_range else {}

        sem = asyncio.Semaphore(concurrency)
        tasks = []

        async def worker(i, s, e):
            async with sem:
                try:
                    await fetch_range(session, url, s, e, out_path, prog, timeout, max_retries, i, verify_headers)
                    # mark done
                    md = load_meta(out_path) or {}
                    md.update({
                        "url": url,
                        "size": size,
                        "chunk_size": chunk_size,
                        "etag": validators.get("etag"),
                        "last_modified": validators.get("last_modified"),
                    })
                    finished = set(md.get("done", []))
                    finished.add(i)
                    md["done"] = sorted(finished)
                    save_meta(out_path, md)
                except Exception as e:
                    print(f"\nWorker {i} failed: {e}")
                    raise

        for i, (s, e) in enumerate(ranges):
            if i in done_idx:
                continue
            tasks.append(asyncio.create_task(worker(i, s, e)))

        # Wait for all slices - if any failed or was short, this will raise
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    # Clean up meta if complete
    meta = load_meta(out_path)
    if meta and len(meta.get("done", [])) == len(ranges):
        try:
            os.remove(meta_path(out_path))
        except Exception:
            pass

# ------------- single stream fallback

async def download_single(url, out_path, timeout, max_retries):
    attempt = 0
    backoff = 1.0
    while True:
        try:
            connector = aiohttp.TCPConnector(limit=4, ssl=None)
            timeout_obj = aiohttp.ClientTimeout(total=timeout*3, connect=timeout, sock_read=timeout)
            headers = {"User-Agent": "UltraDownloader/0.2", "Accept-Encoding": "identity"}
            async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj, headers=headers,
                                             auto_decompress=False) as session:
                async with session.get(url) as r:
                    r.raise_for_status()
                    size = int(r.headers.get("Content-Length") or 0)
                    prog = Progress(total=size)
                    with open(out_path, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 15):
                            f.write(chunk)
                            await prog.add(len(chunk))
                            prog.maybe_print(prefix="[single] ")
            break
        except Exception:
            attempt += 1
            if attempt > max_retries:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

# ------------- main

async def main(argv=None):
    ap = argparse.ArgumentParser(description="Fast multi connection HTTP downloader with resume")
    ap.add_argument("urls", nargs="+", help="Download URLs (one or more)")
    ap.add_argument("-o", "--output", help="Output file path (for single URL) or directory (for multiple URLs)")
    ap.add_argument("-c", "--connections", type=int, default=16, help="Max concurrent connections per download")
    ap.add_argument("-s", "--chunk-size", default="8MB", help="Chunk size per connection, example 4MB or 8MB")
    ap.add_argument("-t", "--timeout", type=float, default=30.0, help="Connect timeout in seconds")
    ap.add_argument("-r", "--retries", type=int, default=5, help="Max retries per chunk")
    ap.add_argument("--hash", dest="expect_hash", help="Optional sha256 to verify after download (single URL only)")

    args = ap.parse_args(argv)

    urls = args.urls
    chunk_size = human_to_bytes(str(args.chunk_size))
    if chunk_size <= 0 or args.connections <= 0 or args.timeout <= 0 or args.retries < 0:
        ap.error("Chunk size, connections and timeout must be positive; retries cannot be negative")
    if args.expect_hash and len(urls) != 1:
        ap.error("--hash requires a single URL")

    # Handle multiple URLs
    if len(urls) > 1:
        if args.output and os.path.isfile(args.output):
            print("Error: When downloading multiple URLs, --output must be a directory or not specified")
            sys.exit(1)
        
        output_dir = args.output or "."
        os.makedirs(output_dir, exist_ok=True)
        names = [default_name_from_url(url) for url in urls]
        if len(set(os.path.normcase(name) for name in names)) != len(names):
            ap.error("Multiple URLs have the same filename; download them separately with explicit outputs")
        
        # Download all URLs in parallel with error isolation
        async def download_with_error_handling(url, out_path):
            try:
                await download_url(url, out_path, chunk_size, args.connections, args.timeout, args.retries)
                print(f"\n✓ Completed: {os.path.basename(out_path)}")
                return True
            except Exception as e:
                print(f"\n✗ Failed {os.path.basename(out_path)}: {e}")
                return False
        
        tasks = []
        for url in urls:
            out_path = os.path.join(output_dir, default_name_from_url(url))
            tasks.append(download_with_error_handling(url, out_path))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successes = sum(1 for r in results if r is True)
        print(f"\nCompleted {successes}/{len(urls)} downloads in: {output_dir}")
        if successes != len(urls):
            raise RuntimeError("One or more HTTP downloads failed")
        return

    # Single URL handling (original behavior)
    url = urls[0]
    out_path = args.output or default_name_from_url(url)
    if os.path.isdir(out_path) or out_path.endswith(("/", "\\")):
        out_path = os.path.join(out_path, default_name_from_url(url))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    
    await download_url(url, out_path, chunk_size, args.connections, args.timeout, args.retries)
    print("\nDownload complete:", out_path)

    if args.expect_hash:
        print("Verifying sha256...")
        h = hashlib.sha256()
        with open(out_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        got = h.hexdigest()
        if got.lower() == args.expect_hash.lower():
            print("Hash OK")
        else:
            print("Hash mismatch")
            print("Expected:", args.expect_hash)
            print("Got     :", got)
            raise RuntimeError("SHA256 verification failed")

async def download_url(url, out_path, chunk_size, connections, timeout, retries):
    # Probe headers
    connector = aiohttp.TCPConnector(limit=4, ssl=None)
    timeout_obj = aiohttp.ClientTimeout(total=timeout*2, connect=timeout)
    headers = {"User-Agent": "UltraDownloader/0.2", "Accept-Encoding": "identity"}
    async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj, headers=headers,
                                     auto_decompress=False) as session:
        # Use GET 0-0 probe if HEAD is blocked
        size = None
        etag = None
        last_mod = None

        # Try HEAD for size
        try:
            hr = await head(session, url)
            size = hr.headers.get("Content-Length")
            etag = hr.headers.get("ETag")
            last_mod = hr.headers.get("Last-Modified")
            size = int(size) if size is not None else None
        except Exception:
            pass

        supports = False
        try:
            supports = await probe_ranges(session, url)
        except Exception:
            supports = False

        if size is None:
            # Try to get size from a GET request headers
            async with session.get(url, allow_redirects=True) as r:
                r.raise_for_status()
                size = int(r.headers.get("Content-Length") or 0)

    # Decide mode
    if size and supports and size > chunk_size:
        print(f"Segmented mode - size {fmt_bytes(size)} - chunk {fmt_bytes(chunk_size)} - connections {connections}")
        validators = {"etag": etag, "last_modified": last_mod}
        await download_segmented(url, out_path, size, chunk_size, connections, timeout, retries, validators)
    else:
        print("Server does not support ranges or size is small - using single stream")
        await download_single(url, out_path, timeout, retries)
