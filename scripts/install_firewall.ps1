# install_firewall.ps1 - allow inbound TCP to the Responses shim port.
# Run once from an elevated prompt (or let it self-elevate).
param([int]$Port = 8088)
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Start-Process powershell -Verb RunAs -Wait `
    -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath,'-Port',$Port
  exit
}
netsh advfirewall firewall delete rule name="local-llm-stack shim $Port" >$null 2>&1
netsh advfirewall firewall add rule name="local-llm-stack shim $Port" `
  description="OpenAI Responses API shim for local model stack" `
  dir=in action=allow protocol=TCP localport=$Port profile=private,domain | Out-Null
"firewall rule added for TCP $Port (private/domain profiles only)"
