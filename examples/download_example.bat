@echo off
REM Example batch file to download a file using UltraDownloader
REM Usage: download_example.bat <URL> <output_file>

set URL=%1
set OUT=%2

if "%URL%"=="" (
    set URL=https://speed.hetzner.de/100MB.bin
)
if "%OUT%"=="" (
    set OUT=100MB.bin
)

python ..\get.py %URL% -o %OUT%
