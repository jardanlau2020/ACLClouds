#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACLClouds 403 取證探針（一次性診斷用，可安全刪除）
===================================================
背景：2026-10-05 run #11 顯示 ACLClouds02 續期 POST 食 HTTP 403，但
同一個 key 讀 /api/client 正常 → 認證層係通的，403 只發生在 renew 端點。
要分辨三種可能，單靠狀態碼唔夠，必須睇 body/headers 原文：

  1. 帳號/權限層：body 講明 permission / subscription / not allowed
  2. CF Turnstile 反自動化層：CF challenge（cf-mitigated: challenge、
     "Just a moment"、cf-turnstile、captcha 關鍵字）
  3. 方法/UA 層：同一端點換 UA 或換方法就通 → 面板只擋 POST 裸請求

探針做幾組對照，並**唔發 Telegram**（避免同真實紅燈混淆），
結果全部落 stdout，由人／助手讀 log 判讀。
"""

import os
import sys
import json
import re

import requests

BASE_URL = os.environ.get("ACL_API_BASE_URL", "").strip().rstrip("/") \
    or "https://aclclouds.com"
API_KEYS_RAW = os.environ.get("ACL_API_KEYS", "").strip()

UA_DEFAULT = "aclclouds-renew-apikey/1.0"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# 續期 POST 可能真係續到，所以每個 server id 只打一次「真 POST」，
# 對照組用 GET/OPTIONS（不觸發續期）——避免重複消耗免費續期次數。
CF_HINTS = ("just a moment", "cf-turnstile", "cf_chl", "challenge-platform",
            "captcha", "cf-mitigated", "attention required", "checking your browser")


def log(m):
    print(m, flush=True)


def mask(k):
    """永不輸出完整 key：只留頭 4 尾 4。"""
    if not k:
        return "(空)"
    return f"{k[:4]}…{k[-4:]}(len={len(k)})" if len(k) > 12 else f"(len={len(k)})"


def collect_accounts():
    out = []
    for line in API_KEYS_RAW.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|||" in line:
            label, key = line.split("|||", 1)
        elif ":" in line and line.lower().startswith(("acl", "acc", "main", "jeffer")):
            label, key = line.split(":", 1)
        else:
            label, key = f"account-{len(out) + 1}", line
        label, key = label.strip(), key.strip()
        if key:
            out.append((label or f"account-{len(out) + 1}", key))
    return out


def make_session(api_key, ua=UA_DEFAULT):
    s = requests.Session()
    s.headers.update({
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": ua,
    })
    return s


def sniff(resp, tag):
    """從 status / headers / body 抽線索，判定擋邊層。"""
    h = {k.lower(): v for k, v in resp.headers.items()}
    server = h.get("server", "")
    via = h.get("via", "")
    cf_ray = "cf-ray" in h
    mitigated = h.get("cf-mitigated", "")
    ctype = h.get("content-type", "")
    body = (resp.text or "")[:1200]

    verdict = []
    if mitigated or "challenge-platform" in (h.get("cf-chl-bypass") or "") or \
       (cf_ray and "just a moment" in body.lower()):
        verdict.append("CF Turnstile/challenge 反自動化")
    if cf_ray:
        verdict.append("經 CF 邊緣（cf-ray 存在）")
    if "captcha" in body.lower() or "captcha" in (h.get("x-captcha") or ""):
        verdict.append("body 提及 captcha")
    if resp.status_code in (401,):
        verdict.append("認證層（key 死/格式錯）")
    if resp.status_code == 403:
        if re.search(r"permission|forbidden|not allowed|subscription|unauthor", body, re.I):
            verdict.append("權限/業務層（body 有 permission/subscription 類字眼）")
        if not verdict:
            verdict.append("403 來源未明（要睇 body 原文）")
    if ctype and "text/html" in ctype:
        verdict.append(f"回 HTML 非 JSON（ctype={ctype}）")

    log(f"    [{tag}] HTTP {resp.status_code} | server={server!r} via={via!r}")
    interesting = {k: v for k, v in h.items()
                   if k in ("cf-ray", "cf-mitigated", "content-type", "retry-after",
                            "x-request-id", "set-cookie", "x-captcha")}
    if interesting:
        for k, v in interesting.items():
            if k == "set-cookie":
                # set-cookie 可能含會話資訊，打印時遮罩 value
                names = re.findall(r"(^|,\s*)([A-Za-z0-9_\-]+)=", v)
                log(f"      {k}: <{len(v)} bytes, keys={[n for _, n in names]}>")
            else:
                log(f"      {k}: {v[:160]}")
    log(f"      判定: {' + '.join(verdict) or '（無特徵）'}")
    log(f"      body[:600]={body[:600]!r}")
    return verdict


def main():
    log(f"🩺 ACLClouds 403 取證探針 @ {BASE_URL}")
    accounts = collect_accounts()
    if not accounts:
        log("❌ ACL_API_KEYS 為空")
        return 2
    log(f"📋 {len(accounts)} 個帳號\n")

    for label, key in accounts:
        log(f"══ [{label}] key={mask(key)} ══")
        sess = make_session(key)

        # ── A. 基線：讀清單（已知可行）──────────────────────────
        try:
            r = sess.get(BASE_URL + "/api/client", timeout=30)
            log(f"  A) GET /api/client → HTTP {r.status_code}")
            if r.status_code != 200:
                sniff(r, "A")
                log("  ⚠️ 連清單都讀唔到 → 認證層有問題，續期 POST 先唔試\n")
                continue
            servers = []
            for item in (r.json().get("data") or []):
                a = item.get("attributes") or {}
                if a.get("identifier"):
                    servers.append(a)
            log(f"     → 認證層 OK，讀到 {len(servers)} 台")
        except Exception as e:
            log(f"  A) 例外: {e}")
            continue

        for a in servers:
            sid = a.get("identifier")
            name = a.get("name")
            can_renew = bool(a.get("can_renew"))
            log(f"\n  ── 伺服器 {sid} ({name}) can_renew={can_renew} ──")

            if not can_renew:
                log("     can_renew=False（窗口未開）→ 唔打 renew POST（避免噪音）")
                # 仍用 GET 睇端點層面
                try:
                    rg = make_session(key).get(f"{BASE_URL}/api/client/servers/{sid}/upgrade/renew",
                                              timeout=30)
                    log(f"     B) GET renew 端點（對照，不觸發續期）→ HTTP {rg.status_code}")
                    sniff(rg, "B-GET")
                except Exception as e:
                    log(f"     B) 例外: {e}")
                continue

            # ── C. 換 UA 試 GET（同端點，不觸發續期）──────────────
            try:
                rb = make_session(key, UA_BROWSER).get(
                    f"{BASE_URL}/api/client/servers/{sid}/upgrade/renew", timeout=30)
                log(f"     C) GET renew 端點 + 瀏覽器 UA（對照）→ HTTP {rb.status_code}")
                sniff(rb, "C-GET-BROWSERUA")
            except Exception as e:
                log(f"     C) 例外: {e}")

            # ── D. 真續期 POST（僅此一次）─────────────────────────
            try:
                rp = make_session(key).post(
                    f"{BASE_URL}/api/client/servers/{sid}/upgrade/renew",
                    json={}, timeout=30)
                log(f"     D) POST renew（ua=腳本）→ HTTP {rp.status_code}")
                sniff(rp, "D-POST")

                if rp.status_code in (200, 204):
                    log("     ✅ 續期成功！403 唔穩定（可能 CF 隨機/首次觸發）")
                    continue

                # ── E. 403 就用瀏覽器 UA 再 POST 一次（判 UA 層）──
                if rp.status_code == 403:
                    try:
                        rq = make_session(key, UA_BROWSER).post(
                            f"{BASE_URL}/api/client/servers/{sid}/upgrade/renew",
                            json={}, timeout=30)
                        log(f"     E) POST renew + 瀏覽器 UA（判 UA 層）→ HTTP {rq.status_code}")
                        sniff(rq, "E-POST-BROWSERUA")
                    except Exception as e:
                        log(f"     E) 例外: {e}")
            except Exception as e:
                log(f"     D) 例外: {e}")
        log("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
