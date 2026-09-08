#!/usr/bin/env bash
# oodd 短 verification：重開入無、xray 有無活返
set -u
echo "=== verify $(date -u +%FT%TZ) ==="
uptime
echo "uptime_since: $(uptime -s)"
free -h | head -3
echo "xray unit: $(systemctl is-active xray 2>/dev/null || echo 'not-found/failed')"
ss -ltnp 2>/dev/null | grep -E ':443' | head -5
ps aux | grep -E '[x]ray' | head -3
echo "=== END verify ==="
