# Example PowerShell script to download a file using UltraDownloader
# Usage: .\download_example.ps1 -Url <URL> -Out <output_file>

param(
    [string]$Url = "https://speed.hetzner.de/100MB.bin",
    [string]$Out = "100MB.bin"
)

python ..\get.py $Url -o $Out
