#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析 TUIC 代理 URI (tuic://...), 生成 sing-box 本地桥接配置 (JSON 输出到 stdout)。

用法:
    TUIC_PROXY="tuic://..." python3 tuic_bridge.py > /tmp/tuic.json
    sing-box run -c /tmp/tuic.json &

原理: sing-box 在 127.0.0.1:10800 开 mixed inbound (HTTP+SOCKS5),
把本地流量走 TUIC/QUIC 隧道送到远端节点。API requests 与浏览器
只要把代理指向 http://127.0.0.1:10800 即可。
"""
import json
import os
import sys
from urllib.parse import urlparse, parse_qs, unquote


def main() -> int:
    uri = os.environ.get("TUIC_PROXY", "").strip()
    if not uri:
        print("⚠️ TUIC_PROXY 为空, 无配置可生成", file=sys.stderr)
        return 1

    u = urlparse(uri)
    if u.scheme not in ("tuic", "tuicv5", "tuicv6"):
        print(f"⚠️ 不支持的 scheme: {u.scheme}", file=sys.stderr)
        return 1

    # auth 部分: uuid%3Apassword (URL-encoded "uuid:password")
    auth = unquote(u.username or "")
    parts = auth.split(":")
    uuid = parts[0].strip()
    # TUIC v1: password 恒等於 uuid; v3: 獨立. 有第二段用第二段, 冇就照舊 (uuid 頂替)
    password = (parts[1].strip() if len(parts) > 1 else uuid)
    host = u.hostname
    port = u.port
    q = parse_qs(u.query)

    server_name = (q.get("sni") or [host or ""])[0]
    congestion = (q.get("congestion_control") or [""])[0]
    alpn = (q.get("alpn") or ["h3"])[0]
    insecure = str((q.get("allow_insecure") or ["0"])[0]).lower() in ("1", "true", "yes")

    # 本地桥接监听端口: 默认 10800 (workflow 里 curl -x http://127.0.0.1:10800 探测)
    listen_port = int(os.environ.get("TUIC_BRIDGE_PORT", "10800"))

    config = {
        "log": {"level": "info"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": listen_port,
            }
        ],
        "outbounds": [
            {
                "type": "tuic",
                "tag": "tuic-out",
                "server": host,
                "server_port": port,
                "uuid": uuid,
                "password": password,
                "congestion_control": congestion or "bbr",
                "udp_relay_mode": "native",
                "tls": {
                    "enabled": True,
                    "server_name": server_name,
                    "insecure": insecure,
                    "alpn": [alpn],
                },
            }
        ],
    }
    json.dump(config, sys.stdout, indent=2)
    print(file=sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
