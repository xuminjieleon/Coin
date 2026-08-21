# CoinLens 后端启动脚本
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $root "backend")
& ".\.venv\Scripts\python.exe" "main.py"
