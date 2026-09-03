$ErrorActionPreference = "Continue"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
$log = "D:\Work\Coin\backend\data\autostart.log"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Value "`n===== $ts trigger ====="
$py = "D:\Work\Coin\backend\.venv\Scripts\python.exe"
$workdir = "D:\Work\Coin\backend"
$outLog = "D:\Work\Coin\backend\data\service-out.log"
$errLog = "D:\Work\Coin\backend\data\service-err.log"
try {
  # 若 8000 已被占用则不重复起
  $busy = netstat -ano | Select-String ":8000\s" | Select-String "LISTENING"
  if ($busy) {
    Add-Content -Path $log -Value "8000 already listening, skip"
    exit 0
  }
  $p = Start-Process -FilePath $py -ArgumentList "main.py" -WorkingDirectory $workdir `
        -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
  Add-Content -Path $log -Value ("started python PID=" + $p.Id)
} catch {
  Add-Content -Path $log -Value ("start error: " + $_.Exception.Message)
}