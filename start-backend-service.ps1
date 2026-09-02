$ErrorActionPreference = "Continue"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path "D:\Work\Coin\backend\data\autostart.log" -Value "`n===== $ts boot trigger ====="
try {
  & "D:\Work\Coin\start-backend.ps1" *>> "D:\Work\Coin\backend\data\autostart.log"
} catch {
  Add-Content -Path "D:\Work\Coin\backend\data\autostart.log" -Value ("start error: " + $_.Exception.Message)
}