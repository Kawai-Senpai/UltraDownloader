# UltraDownloader

Download YouTube video, audio, subtitles and transcripts, or large HTTP files.
Python 3.12+; Windows, macOS and Linux.

## Quick start

```powershell
uv sync
uv run ultradownloader doctor
uv run ultradownloader video "https://www.youtube.com/watch?v=VIDEO_ID"
```

Without uv:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python get.py setup
python get.py video "https://www.youtube.com/watch?v=VIDEO_ID"
```

`setup` installs Python dependencies and a local FFmpeg binary. A supported Deno
or Node runtime is discovered automatically; Deno is installed into the active
Python environment if neither is available. No administrator access or downloaded
shell installer is needed. First-use YouTube commands install missing dependencies
by default; `--no-auto-install` disables that behavior. Use `doctor` to see exact
versions and executable paths. Library callers install dependencies before use.

## Video and quality

```powershell
# Highest available quality, including adaptive video + audio. No re-encoding.
python get.py video "$URL" --quality best --container mkv

# Best available at or below 1080p; never silently exceed the ceiling.
python get.py video "$URL" --quality 1080 --container mp4

# Inspect resolution, FPS, HDR, codecs, bitrates and exact format IDs.
python get.py formats "$URL"
python get.py video "$URL" --format 137+140 --container mkv

# Save thumbnail alongside the video and metadata.
python get.py video "$URL" --thumbnail -o media
```

Set `$URL` to a complete YouTube URL. Watch, Shorts, embed, music.youtube.com and
youtu.be URLs use yt-dlp's extractor. A bare YouTube URL also defaults to video.
MKV allows the widest codec selection and defaults to best available video plus
best audio. MP4/WebM constrain source formats to their respective containers;
this can reduce available quality. Explicit format IDs override quality selection.
FFmpeg merges/remuxes video without upscaling or recompressing it. If no format
satisfies your ceiling or container, the command fails rather than changing it.

## Audio and MP3

```powershell
# Best source audio, preserving its codec without lossy conversion.
python get.py audio "$URL"

# MP3 defaults to 320 kbps. Choose a target bitrate or VBR quality.
python get.py mp3 "$URL" --bitrate 192
python get.py mp3 "$URL" --vbr-quality 0

python get.py audio "$URL" --audio-format m4a --bitrate 256
python get.py audio "$URL" --audio-format opus --bitrate 160
python get.py audio "$URL" --audio-format flac
python get.py audio "$URL" --audio-format wav
```

MP3 files receive title/artist metadata through FFmpeg. MP3 VBR ranges from 0
(best) to 9; VBR overrides bitrate. Available bitrate targets are 64, 96, 128,
160, 192, 224, 256 and 320 kbps. Higher output bitrates, FLAC and WAV cannot
restore information absent from YouTube's compressed source.

## Subtitles, transcripts and text

```powershell
# Lists available caption languages, description, chapters and other metadata.
python get.py info "$URL"

# Uploaded captions preferred; automatic captions are the fallback.
python get.py subtitles "$URL" --language en
python get.py subtitles "$URL" --language hi --prefer-auto
python get.py subtitles "$URL" --subtitle-formats srt,vtt,ttml

# Text plus JSON segments containing start/duration/text.
python get.py transcript "$URL" --language en

# Video, audio and all five caption/text formats.
python get.py all "$URL" --quality 2160 --language en --thumbnail
```

Subtitle exports support SRT, WebVTT, TTML, TXT and JSON. Original timing is
preserved in subtitle files; derived transcripts remove overlapping rolling
caption text. Basic conversion may lose advanced styling. Transcript extraction
uses published human/automatic captions, not speech recognition: a video with
no matching captions returns an explicit error. Text generation from uncaptained
audio is not included. `info` also exposes video descriptions and chapter text.

## Playlists and batches

```powershell
python get.py video "$PLAYLIST_URL" --playlist --playlist-items 1:10
python get.py mp3 "$PLAYLIST_URL" --playlist --playlist-items 1,3,5
python get.py transcript "$URL1" "$URL2" --language en
```

Playlist expansion is opt-in. For a watch URL containing a playlist, the default
is only that video. Playlist/channel entries are processed sequentially, with
conservative request sleeps and two fragment connections per download. Failures
produce a nonzero exit status, and subsequent videos/URLs continue. `all` reports
an error if any requested output, including captions, is unavailable; media
already downloaded remains on disk.

## Resume, storage and network settings

Downloads go into deterministic directories under `downloads/` (or `-o`). Job
identities include quality, container and audio settings, preventing a later
high-quality request from accidentally reusing a lower-quality file. Filenames
use fixed components instead of remote titles. The original title, description,
chapters and selected media metadata are saved in `metadata.json` for media jobs.
Results print the final file paths as JSON. Transient signed media URLs and
cookies are excluded from saved metadata. Keep `.part` files to resume interrupted
media downloads. Run a given output job in one process at a time.

```powershell
python get.py video "$URL" --fragments 4 --retries 10 --timeout 45
python get.py video "$URL" --rate-limit 2000000 --sleep 8
python get.py video "$URL" --proxy http://127.0.0.1:8080
python get.py video "$URL" --cookies-from-browser firefox
python get.py video "$URL" --cookies private-cookies.txt
python get.py video "$URL" --ffmpeg-location C:\Tools\ffmpeg\bin
```

Cookies are loaded only when explicitly requested. Cookie files use Netscape
format and must have private permissions on POSIX (`chmod 600`). Proxy credentials
and URL queries are redacted from application errors/logs. TLS verification stays
enabled, local file extraction and shortcut generation stay disabled, and DRM
formats are excluded. Download only content you are authorized to copy.

## Updates and troubleshooting

```powershell
python get.py update
python get.py update --nightly
python get.py video "$URL" --auto-update
python get.py doctor
```

`--auto-update` updates at most once per day per Python environment, before loading
the extractor. Explicit `update` always checks immediately. Updates install from
PyPI into the active environment, keeping yt-dlp and its required EJS release
together. Managed Deno and the FFmpeg Python distribution update too; an existing
system Node/Deno/FFmpeg is left to its package manager. Re-run in a fresh process
after manual updates. `doctor` prints the actual bundled FFmpeg version; use
`--ffmpeg-location` for a newer full FFmpeg/ffprobe installation when needed.

For reproducible development, `uv sync --frozen --extra dev` uses `uv.lock`.
Runtime updates deliberately change that environment; `uv sync --frozen` restores
the lock. To qualify and lock new dependencies, run `uv lock --upgrade`, sync and
run the tests. Nightly updates are explicitly opt-in.

YouTube may require sign-in, limit a network, or temporarily break an extractor.
A sign-in/bot error is reported as a failure, not a successful HTML download.
When a bot check is detected in an interactive terminal, UltraDownloader opens
a **visible browser** at the video. Clear the check or sign in there, return to
the terminal and press Enter. The failed operation retries once with that
browser's YouTube cookies and user agent. Type `cancel` to stop the handoff.
The same proxy is used for the browser and downloader when configured.

This uses a dedicated persistent profile under
`%LOCALAPPDATA%/UltraDownloader/verification-profile` on Windows, or
`~/.cache/UltraDownloader/verification-profile` elsewhere. It never attaches to
your everyday browser. Session cookies are transferred to yt-dlp in memory,
without exporting a cookie file or changing an explicitly supplied cookie file.
The dedicated browser profile retains your sign-in for later verification;
close the verification browser and remove that profile directory to forget it.

Playwright installs on demand; an existing Chrome/Chromium is used when found,
otherwise managed Chromium installs too. To prepare the optional browser support
ahead of time, run `uv sync --extra browser`, then
`uv run python -m playwright install chromium` if Chrome is unavailable.
`--no-auto-install` also applies to browser dependencies. Use
`--no-browser-handoff` to disable interactive recovery. Noninteractive/CI runs
report the error immediately and never open a browser or wait for terminal input.
Clearing a browser check does not guarantee the session will be accepted by
YouTube's downloader endpoints; an unsuccessful retry is reported as a failure.

For an account-accessible video, supply your own cookies explicitly. For player
errors, run `doctor`, `update`, and optionally test `update --nightly`. Token
providers can use yt-dlp's upstream plugin system; this project does not synthesize
tokens, rotate proxies, or circumvent DRM. No downloader can guarantee access to
private, deleted, region-restricted or otherwise unavailable media.

The adapter follows [yt-dlp's Python API](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp)
and the official [JavaScript runtime setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

## HTTP downloads (existing commands preserved)

```powershell
python get.py https://example.org/file.bin -o file.bin
python get.py http https://example.org/file.bin -o downloads/
python get.py https://example.org/file.bin -c 8 -s 4MB -r 5 --hash SHA256
```

Segmented HTTP downloads validate Content-Range before writing. Resume requires
matching resource validators, size, chunk size and an existing output file.
Stale state is discarded. Metadata writes are atomic. Hash mismatches and failed
batches exit with failure. Duplicate filenames in a batch require separate output
paths. For multiple URLs, `-o` is a directory. Legacy HTTP options are shown by
`python get.py http --help`.

## Python API

```python
from ultradownloader import LocalYouTube, NetworkPolicy

client = LocalYouTube("media", network=NetworkPolicy(fragments=2))
video = client.download_video(url, quality=2160, container="mkv")
audio = client.download_mp3(url, bitrate_kbps=320)
text = client.download_transcript(url, language="en")
print(video.path, audio.path, text.files["txt"])
```

Library callers can opt into the same handoff with
`LocalYouTube("media", challenge=BrowserHandoff())` after importing
`BrowserHandoff` from `ultradownloader.browser`. Without a handler, a detected
bot check raises `BotCheckRequired`, including its source URL, for a custom UI
to expose to the user. Browser handoff must run on a thread with an interactive
terminal; GUI applications should provide their own challenge callback instead.

## Development and checks

```powershell
uv sync --frozen --extra dev
uv run pytest -q
uv run ruff check ultradownloader tests
```

Tests use local HTTP fixtures and real FFmpeg/yt-dlp pipelines for adaptive
merging, remuxing, audio conversion/tags, caption extraction and conversion.
Other tests cover quality selection, redaction, path validation, resume integrity,
CLI failures and update scheduling. Ordinary tests never contact YouTube. A live
canary runs only when `ULTRADOWNLOADER_TEST_URL` is explicitly set to an authorized
video. The CI matrix checks Python 3.12-3.14 on Windows and Linux.

No project license has been declared. Third-party components retain their licenses.
