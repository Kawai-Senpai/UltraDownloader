
<p align="center">
	<img src="https://img.shields.io/pypi/pyversions/aiohttp.svg" alt="Python version">
	<img src="https://img.shields.io/badge/maintainer-KawaiSenpai-blueviolet">
</p>

# UltraDownloader

<p align="center"><b>Ultra-fast, resumable, multi-connection HTTP downloader for Python 3.11+</b></p>

UltraDownloader is a command-line tool for downloading large files quickly using multiple connections, with robust resume support and SHA256 verification. Great for datasets, models, and any big files.

---

## 🚀 Features

- ⚡ Multi-connection segmented downloads (configurable chunk size & concurrency)
- 🔄 Resume support (auto-saves progress, resumes interrupted downloads)
- 🛡️ SHA256 hash verification (optional)
- 🧩 Single-stream fallback for servers that don't support ranges
- 📝 Simple, readable code (single file, no external dependencies except `aiohttp`)

---

## 📦 Installation

1. **Clone or download this folder**
2. **Install dependencies** (Python 3.11+ recommended)

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -U pip
pip install -r requirements.txt
```

---

## 🛠️ Usage

### Download a single file

```powershell
python get.py https://speed.hetzner.de/100MB.bin -o 100MB.bin
```

### Download multiple files (to a folder)

```powershell
python get.py https://speed.hetzner.de/100MB.bin https://speed.hetzner.de/10MB.bin -o downloads/
```

### With SHA256 verification

```powershell
python get.py https://speed.hetzner.de/100MB.bin -o 100MB.bin --hash <sha256sum>
```

### Example batch/PowerShell scripts

- See [`examples/download_example.bat`](examples/download_example.bat) (Windows batch)
- See [`examples/download_example.ps1`](examples/download_example.ps1) (PowerShell)
- See [`examples/urls.txt`](examples/urls.txt) for a sample URL list

---

## 📝 Command-line options

| Option            | Description                                      |
|-------------------|--------------------------------------------------|
| `<urls>`          | One or more URLs to download                     |
| `-o, --output`    | Output file (single) or directory (multiple)     |
| `-c, --connections` | Max concurrent connections (default: 16)      |
| `-s, --chunk-size`  | Chunk size per connection (e.g. 8MB, 4MB)     |
| `-t, --timeout`     | Timeout in seconds (default: 30)               |
| `-r, --retries`     | Max retries per chunk (default: 5)             |
| `--hash`            | Optional SHA256 to verify after download       |

---

## 📚 Examples

### Download a file (default settings)

```powershell
python get.py https://speed.hetzner.de/100MB.bin -o 100MB.bin
```

### Download multiple files from a list

```powershell
for /f %u in (examples/urls.txt) do python get.py %u -o downloads\
```

Or in PowerShell:

```powershell
Get-Content examples/urls.txt | ForEach-Object { python get.py $_ -o downloads/ }
```

### Use the example batch/PowerShell scripts

```powershell
cd examples
download_example.bat
# or
./download_example.ps1
```

---

## 🧑‍💻 Developer notes

- Code is in `get.py` (single file, ~400 lines, well-commented)
- Only dependency: `aiohttp` (see `requirements.txt`)
- Works on Windows, Linux, Mac (tested on Windows 10/11)

---

## 🤝 Contributing

PRs and issues welcome! Please open an issue or PR and mention `@KawaiSenpai`.

---

## 📝 License

No explicit license yet. If you want to use this for commercial or open-source projects, open an issue to discuss.

---

<p align="center"><b>Maintained by KawaiSenpai</b></p>
