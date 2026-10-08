# Lets other devices on your local network (for example your phone on the same Wi-Fi) open the TechnoFunda Desk on port 8000.
# Needs administrator rights; the rule only accepts connections from the local subnet, never from the internet.
$log = Join-Path $PSScriptRoot "..\data\firewall.log"
try {
    $name = "TechnoFunda Desk (local network only)"
    if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress LocalSubnet -Profile Any | Out-Null
    }
    $name2 = "TechnoFunda Desk (Tailscale only)"
    if (-not (Get-NetFirewallRule -DisplayName $name2 -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $name2 -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress 100.64.0.0/10 -Profile Any | Out-Null
    }
    "$(Get-Date -Format s) ok: rules '$name' and '$name2' present" | Out-File $log -Append
} catch {
    "$(Get-Date -Format s) failed: $($_.Exception.Message)" | Out-File $log -Append
}
