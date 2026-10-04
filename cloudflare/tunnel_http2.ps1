# tunnel_http2.ps1 - switch the cloudflared Windows service to --protocol http2.
# On many mainland-China networks QUIC/UDP to Cloudflare edges is throttled,
# causing multi-second first-request stalls; TCP/http2 is the stable path.
# Run elevated (the script self-elevates).
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Start-Process powershell -Verb RunAs -Wait `
    -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath
  exit
}
$reg = 'HKLM:\SYSTEM\CurrentControlSet\Services\Cloudflared'
$img = (Get-ItemProperty -Path $reg).ImagePath
if ($img -match '--protocol') {
  "already has --protocol: $img"
} else {
  $new = $img -replace '\btunnel\s+', 'tunnel --protocol http2 '
  Set-ItemProperty -Path $reg -Name ImagePath -Value $new
  Restart-Service Cloudflared -Force
  Start-Sleep 10
  "now: $((Get-ItemProperty -Path $reg).ImagePath)"
}
"service: $((Get-Service Cloudflared).Status)"
