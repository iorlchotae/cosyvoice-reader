# Create desktop shortcut for CosyVoice Reader
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$startBat = Join-Path $root 'scripts\start.bat'

$WshShell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'CosyVoice Reader.lnk'
$shortcut = $WshShell.CreateShortcut($lnk)
$shortcut.TargetPath = $startBat
$shortcut.WorkingDirectory = $root
$shortcut.Description = 'CosyVoice 3.0 local TTS reader'
$shortcut.Save()

Write-Host "Shortcut created: $lnk"
