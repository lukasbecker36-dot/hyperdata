$ErrorActionPreference = "Stop"
$log = "C:\Users\lukasbecker\claudeprojects\Hyperliquid\probe_out.txt"
"START $(Get-Date -Format o)" | Out-File $log -Encoding utf8
try {
    $body = '{"type":"recentTrades","coin":"SPX"}'
    $r = Invoke-RestMethod -Uri "https://api.hyperliquid.xyz/info" -Method Post -ContentType "application/json" -Body $body
    "recentTrades count=$($r.Count)" | Out-File $log -Append -Encoding utf8
    if ($r.Count -gt 0) {
        $oldest = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$r[$r.Count-1].time).UtcDateTime
        $newest = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$r[0].time).UtcDateTime
        "oldest=$oldest newest=$newest" | Out-File $log -Append -Encoding utf8
        ($r[0] | ConvertTo-Json -Compress) | Out-File $log -Append -Encoding utf8
    }
} catch {
    "ERROR: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}
"DONE" | Out-File $log -Append -Encoding utf8
