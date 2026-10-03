#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACLClouds 自動續期 —— Client API Key 版（ACLClouds02）
=====================================================
零 Cookie、零 Captcha、零瀏覽器、零代理。

原理：面板（Pterodactyl 系）Client API 支援用 API Key（`ptlc_` 開頭，面板
Account → API Credentials 產生，只代表自己帳號）直接操作自己的服務：

    GET  /api/client                            → 列出帳號下所有服務
                                                  （含 expires_at / can_renew / plan.renewal_days）
    POST /api/client/servers/{id}/upgrade/renew → 續期
        ├ 窗口未開 → 400 {"error":"renewal_not_available","days_remaining":N}
        │   （HTTP 400 帶業務原因 = 認證已通過；未認證/爛 key 會係 401）
        └ 成功     → 2xx

同 Cookie 版（renew_fixed.py，ACLClouds01）係兩條獨立路線，互不影響。

環境變數：
    ACL_API_KEYS        必填。多帳號格式：`顯示名|||ptlc_xxx`，一行一個
                        （亦接受 `顯示名:ptlc_xxx`；單條淨 key 都得）
    ACL_API_KEY         單帳號簡寫（可選；有 ACL_API_KEYS 時以佢為準）
    ACL_SERVER_ALIASES  顯示名對映（與 Cookie 版共用）：'面板名或id=顯示名' 逗號分隔
    ACL_API_BASE_URL    可選，預設 https://aclclouds.com
    TG_BOT_TOKEN / TG_CHAT_ID   可選，Telegram 通知
    DEBUG=1             可選，印原始回應
"""

import os
import re
import sys
import json
from datetime import datetime, timezone, timedelta

import requests

# ==================== 配置 ====================
BASE_URL = os.environ.get("ACL_API_BASE_URL", "").strip().rstrip("/") \
    or os.environ.get("ACL_BASE_URL", "https://aclclouds.com").rstrip("/")

API_KEYS_RAW = os.environ.get("ACL_API_KEYS", "").strip()
SINGLE_KEY = os.environ.get("ACL_API_KEY", "").strip()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

DEBUG = os.environ.get("DEBUG", "0") == "1"

# 到期前幾日先開始撲閘（面板免費計劃「到期前 2 日開放續期」；加 1 日緩衝）
RENEW_WINDOW_DAYS = float(os.environ.get("RENEW_WINDOW_DAYS", "2"))
WINDOW_BUFFER_DAYS = float(os.environ.get("WINDOW_BUFFER_DAYS", "1"))

UA = "aclclouds-renew-apikey/1.0"


# ==================== 小工具 ====================
def log(msg):
    print(msg, flush=True)


def debug(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}", flush=True)


def now_utc():
    return datetime.now(timezone.utc)


def fmt_dt_cn(dt):
    """UTC+8 顯示，MM-DD HH:MM"""
    if not dt:
        return ""
    return (dt.astimezone(timezone(timedelta(hours=8)))).strftime("%m-%d %H:%M")


def fmt_remaining_cn(seconds):
    """人話剩餘時間：3 天 4 小時 / 4 小時 12 分"""
    if seconds is None:
        return ""
    if seconds < 0:
        return "已到期"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    if days:
        return f"{days} 天 {hours} 小時"
    if hours:
        return f"{hours} 小時 {mins} 分"
    return f"{mins} 分"


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_aliases(raw):
    """ACL_SERVER_ALIASES: '面板名或id=顯示名, 另一台=名2' → {key_lower: 顯示名}"""
    out = {}
    for chunk in re.split(r"[,;\n]+", raw or ""):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k, v = k.strip().lower(), v.strip()
        if k and v:
            out[k] = v
    return out


SERVER_ALIASES = _parse_aliases(os.environ.get("ACL_SERVER_ALIASES", ""))


def _alias(row):
    for k in (str(row.get("id") or "").lower(), str(row.get("name") or "").lower()):
        if k and k in SERVER_ALIASES:
            return SERVER_ALIASES[k]
    return row.get("name") or "?"


def send_tg(text):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        log("ℹ️ 未配置 TG_BOT_TOKEN/TG_CHAT_ID, 跳過通知")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=25,
        )
        if r.status_code == 200:
            log("📨 TG 通知已發送")
        else:
            log(f"⚠️ TG 通知失敗: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"⚠️ TG 通知異常: {e}")


# ==================== 帳號清單 ====================
def collect_accounts():
    """→ [(label, api_key), ...]"""
    accounts = []
    for line in (API_KEYS_RAW or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|||" in line:
            label, key = line.split("|||", 1)
        elif ":" in line and line.lower().startswith(("acl", "acc", "main", "jeffer")):
            label, key = line.split(":", 1)
        else:
            label, key = f"account-{len(accounts) + 1}", line
        label, key = label.strip(), key.strip()
        if key:
            accounts.append((label or f"account-{len(accounts) + 1}", key))
    if not accounts and SINGLE_KEY:
        accounts.append(("main", SINGLE_KEY))
    return accounts


# ==================== HTTP ====================
def make_session(api_key):
    s = requests.Session()
    s.headers.update({
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    return s


def api_get(session, path):
    return session.get(BASE_URL + path, timeout=30)


def api_post(session, path, payload=None):
    return session.post(BASE_URL + path, json=payload or {}, timeout=30)


def list_servers(session):
    """面板嘅 /api/client 就係伺服器清單（/api/client/servers 係 404）"""
    r = api_get(session, "/api/client")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json().get("data") or []
    out = []
    for item in data:
        attrs = item.get("attributes") or {}
        if not attrs.get("identifier"):
            continue
        out.append({
            "id": attrs.get("identifier"),
            "uuid": attrs.get("uuid"),
            "name": attrs.get("name"),
            "expires_at": attrs.get("expires_at"),
            "can_renew": bool(attrs.get("can_renew")),
            "auto_renew": bool(attrs.get("auto_renew")),
            "is_free": attrs.get("is_free"),
            "is_suspended": bool(attrs.get("is_suspended")),
            "renewal_days": ((attrs.get("plan") or {}).get("renewal_days")),
            "plan": ((attrs.get("plan") or {}).get("name")),
        })
    return out


def renew_server(session, sid):
    return api_post(session, f"/api/client/servers/{sid}/upgrade/renew")


# ==================== 單一帳號 ====================
def process_account(label, api_key):
    res = {"label": label, "ok": False, "msg": "", "servers": []}
    session = make_session(api_key)
    try:
        servers = list_servers(session)
    except Exception as e:
        res["msg"] = f"讀取伺服器清單失敗（{e}）"
        log(f"🚨 [{label}] {res['msg']}")
        return res

    res["ok"] = True
    log(f"📦 [{label}] 共 {len(servers)} 台伺服器")

    for srv in servers:
        row = {
            "id": srv["id"],
            "name": srv["name"],
            "expire": parse_iso(srv["expires_at"]),
            "window_open": None,
            "action": "skip",
            "note": "",
        }
        exp = row["expire"]
        if exp:
            row["window_open"] = exp - timedelta(days=RENEW_WINDOW_DAYS)
            row["remaining"] = (exp - now_utc()).total_seconds()
        else:
            row["remaining"] = None

        remain_days = (row["remaining"] / 86400.0) if row["remaining"] is not None else None
        window_days = float(srv["renewal_days"] or RENEW_WINDOW_DAYS)
        should_try = srv["can_renew"] or (
            remain_days is not None and remain_days <= window_days + WINDOW_BUFFER_DAYS
        )

        log(f"  ▪️ {_alias(row)} ({srv['id']}) 到期 {fmt_dt_cn(exp)} | "
            f"can_renew={srv['can_renew']} 剩 {fmt_remaining_cn(row['remaining'])}")

        if srv["is_suspended"]:
            row["action"] = "failed"
            row["note"] = "伺服器已被停權，請登入面板處理"
            res["servers"].append(row)
            continue

        if not should_try:
            row["note"] = "距離到期仲遠，未到續期窗口"
            res["servers"].append(row)
            continue

        try:
            r = renew_server(session, srv["id"])
        except Exception as e:
            row["action"] = "failed"
            row["note"] = f"請求異常（{e}）"
            res["servers"].append(row)
            continue

        if r.status_code in (200, 204):
            log(f"  ✅ 續期成功: {r.text[:200]}")
            row["action"] = "renewed"
            # 重新讀一次拎最新到期日
            try:
                fresh = {s["id"]: s for s in list_servers(session)}.get(srv["id"])
                if fresh and fresh.get("expires_at"):
                    new_exp = parse_iso(fresh["expires_at"])
                    if new_exp:
                        row["expire"] = new_exp
                        row["remaining"] = (new_exp - now_utc()).total_seconds()
                        row["window_open"] = new_exp - timedelta(days=RENEW_WINDOW_DAYS)
            except Exception as e:
                debug(f"重讀到期日失敗: {e}")
            res["servers"].append(row)
            continue

        # 失敗分類
        body = r.text[:300]
        if r.status_code == 400 and "renewal_not_available" in body:
            days = ""
            try:
                days = r.json().get("days_remaining")
            except Exception:
                pass
            row["note"] = f"續期窗口未開（面板報 days_remaining={days}）" if days is not None \
                else "續期窗口未開"
            log(f"  ⏳ {row['note']}")
        elif r.status_code in (401, 403):
            row["action"] = "failed"
            row["note"] = f"API Key 被拒（HTTP {r.status_code}）"
        else:
            row["action"] = "failed"
            row["note"] = f"HTTP {r.status_code} {body}"
        res["servers"].append(row)

    return res


# ==================== TG 排版（同 Cookie 版方案 B 一致：每台兩行）====================
def _render_block(row, account=None):
    name = _alias(row)
    if account:
        name = f"{account}/{name}"
    action, exp, rem, note = row.get("action"), row.get("expire"), row.get("remaining"), row.get("note") or ""

    if action == "renewed":
        l1 = f"✅ {name} · 成功續期" + (f"至 {fmt_dt_cn(exp)}" if exp else "")
        l2 = "ℹ️ " + (f"剩餘 {fmt_remaining_cn(rem)} · " if rem else "") + "服務已自動展期"
        return [l1, l2]

    if action == "failed":
        rem_str = f"（剩 {fmt_remaining_cn(rem)}）" if rem else ""
        return [f"🚨 {name} · 續期未完成{rem_str}",
                f"⚠️ {note or '提交異常'} · 請登入面板手動處理"]

    rem_str = f"（剩 {fmt_remaining_cn(rem)}）" if rem else ""
    l1 = f"🟢 {name} · 狀態良好{rem_str}"
    parts = []
    if exp:
        parts.append(f"{fmt_dt_cn(exp)} 到期")
    if row.get("window_open"):
        parts.append(f"續期窗口將於 {fmt_dt_cn(row['window_open'])} 開啟")
    elif note:
        parts.append(note)
    return [l1, "ℹ️ " + (" · ".join(parts) if parts else "未到續期窗口")]


def build_summary(all_results):
    blocks = []
    multi = len(all_results) > 1
    for r in all_results:
        if not r.get("ok") and not r.get("servers"):
            blocks.append([f"🚨 ACLClouds · 帳號異常（{r['label']}）",
                           f"⚠️ {r.get('msg') or 'API Key 無效或面板異常'} · 請檢查憑證"])
            continue
        for row in (r.get("servers") or []):
            blocks.append(_render_block(row, account=r["label"] if multi else None))
    if not blocks:
        return "🟢 ACLClouds02 · 檢查完成（未發現伺服器實例）"
    return "\n\n".join("\n".join(b) for b in blocks)


# ==================== 主流程 ====================
def main():
    log(f"🚀 ACLClouds API Key 續期啟動 @ {now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log(f"🌐 站點: {BASE_URL}")

    accounts = collect_accounts()
    if not accounts:
        msg = ("❌ 未配置 ACL_API_KEYS（或 ACL_API_KEY）\n\n"
               "格式：ACL_API_KEYS = `顯示名|||ptlc_xxx`（一行一個，可多帳號）")
        log(msg)
        send_tg(msg)
        sys.exit(1)

    log(f"📋 共 {len(accounts)} 個帳號（API Key 模式）")
    all_results = [process_account(label, key) for label, key in accounts]

    summary = build_summary(all_results)
    print("\n" + summary + "\n")
    send_tg(summary)

    if any(not r.get("ok") for r in all_results) or \
       any(row.get("action") == "failed" for r in all_results for row in (r.get("servers") or [])):
        log("❌ 存在失敗, exit 1 (Actions 將標紅)")
        sys.exit(1)
    log("✅ 完成")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n用戶中斷")
    except Exception as e:
        log(f"💥 未捕獲異常: {e}")
        send_tg(f"🎮 ACLClouds02 續期巡檢\n💥 腳本崩潰：{e}")
        sys.exit(1)