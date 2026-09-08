#!/usr/bin/env bash
# oodd (NeoHeberg 2a06:9801:700::1b4) read-only 診斷：點解幾時被重開
# 純只讀, 唔改任何嘢
set -u
echo "=== uname / uptime (diag_start=$(date -u +%FT%TZ)) ==="
uname -a
echo "uptime_since: $(uptime -s)"
who -b 2>/dev/null
echo
echo "=== last -x (boot/poweroff 史) ==="
last -x 2>/dev/null | head -15
echo
echo "=== memory ==="
free -h
grep -E 'MemTotal|MemAvailable|SwapTotal|SwapFree' /proc/meminfo
echo
echo "=== top mem consumers ==="
ps aux --sort=-%mem | head -10
echo
echo "=== failed units ==="
systemctl list-units --failed --no-pager 2>/dev/null | head -10
echo
echo "=== OOM/panic in kernel journal (since boot) ==="
journalctl -k --no-pager 2>/dev/null | grep -i -E 'oom|out of memory|killed process|panic|emergency' | tail -25
echo
echo "=== journal errors (since boot) ==="
journalctl -b --no-pager 2>/dev/null | grep -i -E 'error|fail|oom|killed|shutdown' | tail -25
echo
echo "=== swap ==="
swapon --show 2>/dev/null || cat /proc/swaps
ls -lh /swapfile /swap.img 2>/dev/null || echo "no swapfile found"
echo
echo "=== disk ==="
df -h /
echo
echo "=== virt type ==="
systemd-detect-virt
echo
echo "=== xray / vless service ==="
(systemctl status xray --no-pager -l 2>/dev/null | head -14) || true
ls -l /etc/systemd/system/xray* /lib/systemd/system/xray* /usr/lib/systemd/system/xray* 2>/dev/null || echo "no xray unit files"
ss -ltnp 2>/dev/null | grep -E ':443|:80|:4443' | head -8
echo
echo "=== xray process ==="
ps aux | grep -E '[x]ray' | head -5
echo
echo "=== power/reboot hints in /var/log ==="
grep -i -E 'power|reboot|shutdown|hang|panic' /var/log/syslog /var/log/dmesg /var/log/kern.log 2>/dev/null | tail -20
echo "=== END diag ==="
