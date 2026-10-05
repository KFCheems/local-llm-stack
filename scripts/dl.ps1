# _dl2.ps1 - segmented, resumable, self-trimming HTTP downloader.
# Direct connection only (--noproxy '*'): never routes through a system proxy.
param(
    [Parameter(Mandatory=$true)][string]$Url,
    [Parameter(Mandatory=$true)][string]$Out,
    [int64]$Expected = 0,
    [int]$Parts = 8,
    [string]$AuthFile = "",
    [int]$MaxRounds = 12
)
$ErrorActionPreference = "Continue"
$auth = ""
$curl_args = @('--noproxy', '*', '-sIL', '-m', '120')
if ($AuthFile -and (Test-Path $AuthFile)) {
    $tok = (Get-Content $AuthFile -Raw).Trim()
    if ($tok) { $auth = "Authorization: Bearer $tok"; $curl_args += @('-H', $auth) }
}

$head = & curl.exe @curl_args $Url
$total = [int64](($head | Select-String -Pattern '(?i)^content-length:\s*(\d+)' -AllMatches |
    ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -Last 1))
if ($total -le 0) { Write-Host "HEAD_FAILED"; exit 1 }
if ($Expected -gt 0 -and $total -ne $Expected) { Write-Host ("SIZE_MISMATCH head={0} expected={1}" -f $total, $Expected); exit 1 }
Write-Host ("total={0} bytes ({1:N2} GiB), parts={2}" -f $total, ($total / 1GB), $Parts)

$seg = [math]::Ceiling([double]$total / $Parts)

for ($round = 1; $round -le $MaxRounds; $round++) {
    $jobs = @()
    for ($i = 0; $i -lt $Parts; $i++) {
        $start = [int64]($i * $seg); $end = [math]::Min($start + $seg - 1, $total - 1)
        if ($start -gt $end) { break }
        $expect = $end - $start + 1
        $p = "{0}.part{1:d3}" -f $Out, $i
        $have = 0; if (Test-Path $p) { $have = (Get-Item $p).Length }
        if ($have -eq $expect) { continue }
        if ($have -gt $expect) {
            $fs = [System.IO.File]::Open($p, 'Open', 'Write'); $fs.SetLength($expect); $fs.Close()
            continue
        }
        $from = $start + $have
        $job_args = @('-sL', '--speed-time', '30', '--speed-limit', '200000', '-m', '3600',
                      '-r', "$from-$end")
        if ($auth) { $job_args += @('-H', $auth) }
        $jobs += Start-Job -ScriptBlock {
            param($u, $a, $from2, $end2, $p2, $expect2)
            $tmp = "$p2.tmp"
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
            & curl.exe --noproxy '*' @a -o $tmp $u
            if (Test-Path $tmp) {
                $got = (Get-Item $tmp).Length
                if ($got -gt 0) {
                    if (Test-Path $p2) { cmd /c copy /b "$p2" + "$tmp" "$p2" | Out-Null }
                    else { Move-Item -Force $tmp $p2 }
                }
                Remove-Item $tmp -Force -ErrorAction SilentlyContinue
            }
            $have2 = 0; if (Test-Path $p2) { $have2 = (Get-Item $p2).Length }
            if ($have2 -gt $expect2) {
                $fs = [System.IO.File]::Open($p2, 'Open', 'Write'); $fs.SetLength($expect2); $fs.Close()
            }
        } -ArgumentList $Url, $job_args, $from, $end, $p, $expect
    }
    if ($jobs.Count -eq 0) { break }
    Write-Host ("round {0}: {1} segment job(s) launched" -f $round, $jobs.Count)
    while (($jobs | Where-Object { $_.State -eq 'Running' }).Count -gt 0) {
        Start-Sleep -Seconds 15
        $sum = 0
        for ($i = 0; $i -lt $Parts; $i++) {
            $p = "{0}.part{1:d3}" -f $Out, $i
            if (Test-Path $p) { $sum += (Get-Item $p).Length }
        }
        Write-Host ("  {0}  {1:N2} / {2:N2} GiB ({3:P1})" -f (Get-Date -Format HH:mm:ss), ($sum / 1GB), ($total / 1GB), ($sum / $total))
    }
    $jobs | Wait-Job | Out-Null
    $jobs | Remove-Job -Force
    $all = $true
    for ($i = 0; $i -lt $Parts; $i++) {
        $start = [int64]($i * $seg); $end = [math]::Min($start + $seg - 1, $total - 1)
        if ($start -gt $end) { break }
        $expect = $end - $start + 1
        $p = "{0}.part{1:d3}" -f $Out, $i
        $have = 0; if (Test-Path $p) { $have = (Get-Item $p).Length }
        if ($have -ne $expect) { $all = $false }
    }
    if ($all) { Write-Host "ALL_PARTS_COMPLETE"; break }
    Write-Host ("round {0} ended with incomplete parts; continuing" -f $round)
}

$sum = 0; $allok = $true
for ($i = 0; $i -lt $Parts; $i++) {
    $start = [int64]($i * $seg); $end = [math]::Min($start + $seg - 1, $total - 1)
    if ($start -gt $end) { break }
    $expect = $end - $start + 1
    $p = "{0}.part{1:d3}" -f $Out, $i
    $have = 0; if (Test-Path $p) { $have = (Get-Item $p).Length }
    $sum += $have
    if ($have -ne $expect) { Write-Host ("part {0} BAD: {1}/{2}" -f $i, $have, $expect); $allok = $false }
}
if (-not $allok) { Write-Host "INCOMPLETE"; exit 1 }
if ($sum -ne $total) { Write-Host ("SUM_MISMATCH {0}/{1}" -f $sum, $total); exit 1 }

if (Test-Path $Out) { Remove-Item $Out -Force }
$first = $true
Get-ChildItem "$Out.part*" | Sort-Object Name | ForEach-Object {
    if ($first) { Copy-Item $_.FullName $Out -Force; $first = $false }
    else { cmd /c copy /b "$Out" + "$($_.FullName)" "$Out" | Out-Null }
}
$fin = (Get-Item $Out).Length
if ($fin -eq $total) {
    Get-ChildItem "$Out.part*" | Remove-Item -Force
    Write-Host ("FILE_DONE {0} {1}" -f $Out, $fin)
} else {
    Write-Host ("CONCAT_BAD {0}/{1}" -f $fin, $total); exit 1
}
