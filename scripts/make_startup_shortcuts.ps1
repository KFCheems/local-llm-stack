# make_startup_shortcuts.ps1 - silent logon autostart for the shim, the
# gatekeepers and the manager (pythonw, no console window).  Each component
# exits quietly when its port is already taken, so this is re-runnable.
param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$StackDir
)
$ErrorActionPreference = 'Stop'
$pyw = Join-Path (Split-Path $PythonExe) "pythonw.exe"
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found next to $PythonExe" }
$startup = [Environment]::GetFolderPath('Startup')
$ws = New-Object -ComObject WScript.Shell

$apps = @(
  @{ name = "llm-stack-shim.lnk";     arg = "$StackDir\scripts\responses_shim.py" },
  @{ name = "llm-stack-manager.lnk";  arg = "$StackDir\scripts\manager.py" }
)
foreach ($s in (Get-Content "$StackDir\config\stack.json" | ConvertFrom-Json).slots) {
  $apps += @{ name = "llm-stack-gatekeeper-$($s.name).lnk";
              arg = "$StackDir\scripts\gatekeeper.py --slot $($s.name)" }
}
foreach ($a in $apps) {
  $lnk = $ws.CreateShortcut((Join-Path $startup $a.name))
  $lnk.TargetPath = $pyw
  $lnk.Arguments = $a.arg
  $lnk.WorkingDirectory = $StackDir
  $lnk.WindowStyle = 7
  $lnk.Description = "local-llm-stack autostart"
  $lnk.Save()
  "created $($a.name)"
}
