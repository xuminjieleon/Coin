# CoinLens backend as a SYSTEM boot task. Run elevated once.
# Runs start-backend.ps1 (notifier + executor start with it) at SYSTEM boot,
# independent of logon session. Logs to backend\data\autostart.log.
$ErrorActionPreference = "Stop"
$root = "D:\Work\Coin"
$taskName = "CoinLens Backend"
$script = Join-Path $root "start-backend.ps1"
$log = Join-Path $root "backend\data\autostart.log"
$wrapper = Join-Path $root "start-backend-service.ps1"

$wrapperBody = @'
$ErrorActionPreference = "Continue"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path "LOGPATH" -Value "`n===== $ts boot trigger ====="
try {
  & "SCRIPTPATH" *>> "LOGPATH"
} catch {
  Add-Content -Path "LOGPATH" -Value ("start error: " + $_.Exception.Message)
}
'@
$wrapperBody = $wrapperBody.Replace("LOGPATH", $log).Replace("SCRIPTPATH", $script)
[System.IO.File]::WriteAllText($wrapper, $wrapperBody, [System.Text.UTF8Encoding]::new($false))

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "REGISTERED $taskName (SYSTEM AtStartup)"
