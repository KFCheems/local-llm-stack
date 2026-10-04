# provision_hostname.ps1 - wire one hostname to a local port on a Cloudflare-
# managed tunnel, with a Cloudflare Access app in front of it (email OTP for
# humans + one shared service token for API clients).
#
# Needs CLOUDFLARE_TOKEN (API token with Access:Apps Edit, Access:ServiceTokens
# Edit, Cloudflare Tunnel Edit, DNS Edit for the zone).
#
# Usage:
#   powershell -File provision_hostname.ps1 -Hostname "api.example.com" `
#     -Port 8088 -AppPrefix "Local API" -OwnerEmails "you@example.com" `
#     [-ServiceTokenId <existing-token-uuid>] [-CreateToken]
#
# After the first run the script prints/keeps the service token client id +
# secret (written next to the script); reuse the same token for every hostname.
param(
    [Parameter(Mandatory=$true)][string]$Hostname,
    [Parameter(Mandatory=$true)][int]$Port,
    [Parameter(Mandatory=$true)][string]$AppPrefix,
    [Parameter(Mandatory=$true)][string[]]$OwnerEmails,
    [string]$ServiceTokenId = "",
    [switch]$CreateToken
)
$ErrorActionPreference = 'Stop'
$tok = $env:CLOUDFLARE_TOKEN
if (-not $tok) { throw "CLOUDFLARE_TOKEN env var not set" }
$h = @{ Authorization = "Bearer $tok"; "Content-Type" = "application/json" }
$base = "https://api.cloudflare.com/client/v4"

function Invoke-Cf($method, $path, $body) {
  $json = if ($body) { $body | ConvertTo-Json -Depth 8 } else { $null }
  try {
    if ($json) { Invoke-RestMethod -Uri "$base$path" -Headers $h -Method $method -Body $json }
    else { Invoke-RestMethod -Uri "$base$path" -Headers $h -Method $method }
  } catch {
    $resp = $_.Exception.Response
    if ($resp) { $sr = New-Object IO.StreamReader($resp.GetResponseStream())
      throw "API ERR $($resp.StatusCode.value__): $($sr.ReadToEnd())" }
    else { throw $_.Exception.Message }
  }
}

# zone lookup from the hostname's registrable domain (last two labels)
$parts = $Hostname.Split(".")
$domain = $parts[-2] + "." + $parts[-1]
$zone = (Invoke-Cf "GET" "/zones?name=$domain").result | Select-Object -First 1
if (-not $zone) { throw "zone $domain not found on this token" }
$zid = $zone.id; $aid = $zone.account.id
"zone: $domain ($zid)  account: $aid"

# tunnel: first healthy tunnel on the account, or by name
$tunnels = (Invoke-Cf "GET" "/accounts/$aid/cfd_tunnel?is_deleted=false").result
$tun = $tunnels | Where-Object { $_.status -eq "healthy" } | Select-Object -First 1
if (-not $tun) { throw "no healthy tunnel found - create one in the dashboard or via API first" }
$tunId = $tun.id
"tunnel: $($tun.name) ($tunId)"

# service token (create once, reuse)
if (-not $ServiceTokenId) {
  $stl = (Invoke-Cf "GET" "/accounts/$aid/access/service_tokens?per_page=50").result
  $st = $stl | Sort-Object { [datetime]$_.created_at } -Descending | Select-Object -First 1
  if ($CreateToken -and -not $st) {
    $new = Invoke-Cf "POST" "/accounts/$aid/access/service_tokens" @{
      name = "local-llm-stack $(Get-Date -Format yyyy-MM-dd)"; duration = "8760h" }
    $st = $new.result
    "service token created: $($st.client_id)  (secret saved to _access_api_token.txt)"
    "CF_ACCESS_CLIENT_ID=$($st.client_id)"
    "CF_ACCESS_CLIENT_SECRET=$($st.client_secret)"
  } elseif (-not $st) { throw "no service token exists - pass -CreateToken" }
  $ServiceTokenId = $st.id
}
"service token: $ServiceTokenId"

# access app (self-hosted, email OTP allow-list + service token non-identity)
$appName = "$AppPrefix ($($tun.name))"
$apps = (Invoke-Cf "GET" "/accounts/$aid/access/apps?per_page=50").result
$app = $apps | Where-Object { $_.domain -eq $Hostname } | Select-Object -First 1
if ($app) { "access app exists: $($app.id)" } else {
  $app = Invoke-Cf "POST" "/accounts/$aid/access/apps" @{
    name = $appName; type = "self_hosted"; domain = $Hostname
    session_duration = "24h"; auto_redirect = $true; app_launcher_visible = $false }
  "access app created: $($app.result.id)"
}
$aid2 = $aid; $appId = $app.result.id; if (-not $appId) { $appId = $app.id }
$pols = (Invoke-Cf "GET" "/accounts/$aid2/access/apps/$appId/policies").result
$ownerInclude = @($OwnerEmails | ForEach-Object { @{ email = @{ email = $_ } } })
if (-not ($pols | Where-Object { $_.decision -eq "allow" })) {
  Invoke-Cf "POST" "/accounts/$aid2/access/apps/$appId/policies" @{
    name = "Allow owner emails"; decision = "allow"; include = $ownerInclude } | Out-Null
  "  + owner emails policy"
}
if (-not ($pols | Where-Object { $_.decision -eq "non_identity" })) {
  Invoke-Cf "POST" "/accounts/$aid2/access/apps/$appId/policies" @{
    name = "Service Token"; decision = "non_identity"
    include = @( @{ service_token = @{ token_id = $ServiceTokenId } } ) } | Out-Null
  "  + service token policy"
}

# tunnel ingress: add/replace this hostname's entry, keep the rest
$cfg = (Invoke-Cf "GET" "/accounts/$aid2/cfd_tunnel/$tunId/configurations").result.config
$ingress = @($cfg.ingress | Where-Object { $_.hostname -and $_.hostname -ne $Hostname })
$ingress += @{ hostname = $Hostname; service = "http://localhost:$Port" }
$ingress += @{ service = "http_status:404" }
Invoke-Cf "PUT" "/accounts/$aid2/cfd_tunnel/$tunId/configurations" @{
  config = @{ ingress = $ingress; "warp-routing" = @{ enabled = $false } } } | Out-Null
"ingress updated"

# DNS CNAME to the tunnel
$records = (Invoke-Cf "GET" "/zones/$zid/dns_records?per_page=100").result
if ($records | Where-Object { $_.name -eq $Hostname }) { "DNS record exists" }
else {
  Invoke-Cf "POST" "/zones/$zid/dns_records" @{
    type = "CNAME"; name = $Hostname.Split(".")[0]
    content = "$tunId.cfargotunnel.com"; proxied = $true; ttl = 1 } | Out-Null
  "DNS CNAME created"
}
"DONE: https://$Hostname now routes to localhost:$Port behind Access"
