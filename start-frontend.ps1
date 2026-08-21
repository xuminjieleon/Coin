# CoinLens 前端启动脚本
# 注：企业 DNS 劫持 npm registry（解析到 127.0.0.1），通过 NODE_OPTIONS 注入
# dns-override.cjs 修复 npm 网络请求；若网络正常可去掉该环境变量。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:NODE_OPTIONS = "--require $root\frontend\tools\dns-override.cjs"
Set-Location (Join-Path $root "frontend")
npm.cmd run dev
