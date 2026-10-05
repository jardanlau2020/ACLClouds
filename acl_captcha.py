#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACLClouds 自家 anti-bot 驗證（純 API，renewal_gate）—— 由 renew_fixed.py 原樣搬出
==============================================================================
背景：2026-10-05 取證（probe_403.py, run 37305141975）確認續期端點回
    403 {"error":"captcha_required","code":"captcha_required",
         "message":"Confirmez que vous n'êtes pas un robot pour renouveler
                    ce service gratuit."}
即「續期免費服務要 proof-of-human」。GET /api/client 正常 → 認證層冇問題；
key 冇死、UA 換邊款都一樣 403 → 唔係 Cloudflare 擋，係站方應用層閘。

協議由前端 6893.js 反編譯還原：
    1. GET  /auth/captcha/challenge?context=<ctx>  → {id, ts, sig, context}
    2. POST /auth/captcha {context,id,ts,sig,elapsed}
         ├ {passed:true, token}                     → 攞到 captcha_token
         └ {passed:false, interactive:true,
            options:[...], target, answer_sig}      → 卡片挑戰
    3. 卡片：GET /auth/captcha/image?t=<option> → OCR → POST {..., answer, answer_sig, target}

⚠️ 兩條血淚規矩（09-16 本地對照實驗實錘，唔好改）：
  a) interactive 回應會帶「刷新後」嘅 {id,ts,sig}，之後提交必須用新值。
     用最初 GET 嘅舊 id/sig 交答案，後端一律靜默拒絕、只會再出新題
     （實測 C2: 4/4 正確答案全拒；A: 刷新後第 1 輪即過）。
  b) 呢個係應用層卡片題，唔係 Cloudflare Turnstile 互動題 ——
     唔係繞過 CF 挑戰，係同站方自家前端走同一條 protocol。

本檔案係既有程式碼嘅搬移，唔係新寫 solver。
"""

import os
import random
import re
import urllib.parse

import requests

# OCR 引擎（ddddocr 可選；缺咗就退化成「隨機揀卡博一輪」）
OCR_AVAILABLE = False
_ocr_engine = None
try:
    import ddddocr  # noqa
    OCR_AVAILABLE = True
    try:
        _ocr_engine = ddddocr.DdddOcr(show_ad=False)
    except Exception:
        _ocr_engine = None
except Exception:
    OCR_AVAILABLE = False


def _ocr_card_text(png_bytes):
    """OCR 單張卡片截圖，回傳識別文字（失敗回空串）"""
    if _ocr_engine is None:
        return ""
    try:
        return (_ocr_engine.classification(png_bytes) or "").strip()
    except Exception:
        return ""


def _log(msg):
    print(msg, flush=True)


def _get(session, base_url, path):
    return session.get(base_url + path, timeout=30)


def _post(session, base_url, path, payload=None):
    return session.post(base_url + path, json=payload if payload is not None else {}, timeout=30)


def solve_captcha_api(session, base_url, context="renewal_gate", max_rounds=6):
    """純 API 解 ACLClouds 自家 anti-bot 驗證。成功回 captcha_token，失敗回 None。

    機房 IP 亦可用 —— 驗證係應用層 protocol，唔係互動 CAPTCHA 繞過。
    """
    try:
        ch = _get(session, base_url, f"/auth/captcha/challenge?context={context}")
    except Exception as e:
        _log(f"   [captcha] challenge 異常: {e}")
        return None
    if ch.status_code != 200:
        _log(f"   [captcha] challenge GET HTTP {ch.status_code}: {ch.text[:120]}")
        return None
    try:
        c = ch.json()
    except Exception:
        _log(f"   [captcha] challenge 響應唔係 JSON: {ch.text[:120]}")
        return None

    base = {"context": c.get("context") or context,
            "id": c.get("id"), "ts": c.get("ts"), "sig": c.get("sig")}
    if not base.get("id") or not base.get("sig"):
        _log(f"   [captcha] challenge 缺 id/sig: {str(c)[:160]}")
        return None

    for rnd in range(1, max_rounds + 1):
        try:
            r = _post(session, base_url, "/auth/captcha",
                       dict(base, elapsed=random.randint(1800, 9000)))
            a = r.json()
        except Exception as e:
            _log(f"   [captcha] 第 {rnd} 輪提交異常: {e}")
            return None

        if a.get("passed") and a.get("token"):
            _log(f"   [captcha] ✅ 第 {rnd} 輪驗證通過, captcha_token 已到手")
            return a["token"]

        opts = a.get("options")
        if not (a.get("interactive") and opts):
            _log(f"   [captcha] 後端唔畀過: {str(a)[:180]}")
            return None

        # ⚠️ 規矩 (a)：用回應帶嘅新 id/sig，唔可以留舊嘅
        if a.get("id") and a.get("sig"):
            base = {"context": a.get("context") or base.get("context"),
                    "id": a["id"], "ts": a.get("ts") or base.get("ts"),
                    "sig": a["sig"]}

        target = (a.get("target") or "").strip()
        asig = a.get("answer_sig") or ""
        _log(f"   [captcha] 第 {rnd} 輪卡片挑戰: 目標 '{target}' / {len(opts)} 張卡")

        best = None
        tl = target.lower()
        for i, op in enumerate(opts):
            try:
                img_r = _get(session, base_url,
                             f"/auth/captcha/image?t={urllib.parse.quote(op)}")
                txt = _ocr_card_text(img_r.content) if img_r.status_code == 200 else ""
                _log(f"   [captcha]   卡{i}: OCR='{txt}'")
                lo = txt.lower()
                if tl and (tl in lo or (lo and lo in tl)):
                    best = op
                    break
            except Exception as e:
                _log(f"   [captcha]   卡{i} OCR 異常: {e}")
        if best is None:
            if tl:
                _log("   [captcha] OCR 冇中目標, 隨機揀卡博一輪（目標字串已知，"
                     "可見下輪是否再出同一目標）")
            best = random.choice(opts)

        try:
            r2 = _post(session, base_url, "/auth/captcha",
                       dict(base, answer=best, answer_sig=asig, target=target))
            b = r2.json()
        except Exception as e:
            _log(f"   [captcha] 卡片提交異常: {e}")
            return None

        if b.get("passed") and b.get("token"):
            _log(f"   [captcha] ✅ 第 {rnd} 輪卡片過關, captcha_token 已到手")
            return b["token"]

        # 規矩 (a) 一樣適用：答錯後用回應嘅新 id/sig 續題
        if b.get("id") and b.get("sig"):
            base = {"context": b.get("context") or base.get("context"),
                    "id": b["id"], "ts": b.get("ts") or base.get("ts"),
                    "sig": b["sig"]}
        if not b.get("interactive"):
            _log(f"   [captcha] 卡片後仍唔過: {str(b)[:180]}")
            return None

    _log("   [captcha] 多輪未解, 放棄純 API 驗證")
    return None


def renew_with_captcha(session, base_url, sid, max_rounds=6):
    """續期 POST，遇 403 captcha_required 就解驗證再帶 token 重發。

    回 (response, captcha_was_required)。captcha_was_required=True 代表
    站方要求 proof-of-human（無論最後通唔通），TG 措辭要反映呢點。
    """
    path = f"/api/client/servers/{sid}/upgrade/renew"
    r = _post(session, base_url, path)

    captcha_required = False
    if r.status_code == 403:
        body = r.text or ""
        try:
            j = r.json()
            code = j.get("code") if isinstance(j, dict) else None
            if code == "captcha_required" or "captcha" in body.lower():
                captcha_required = True
        except Exception:
            if "captcha" in body.lower():
                captcha_required = True

        if captcha_required:
            _log("🧩 續期接口要 anti-bot 驗證, 走純 API renewal_gate 流程（唔使瀏覽器）...")
            tok = solve_captcha_api(session, base_url, "renewal_gate", max_rounds)
            if tok:
                r2 = _post(session, base_url, path, {"captcha_token": tok})
                _log(f"   [renew] 帶 token 重發 -> HTTP {r2.status_code} | {r2.text[:120]}")
                if r2.status_code != 403 or "captcha" not in (r2.text or "").lower():
                    return r2, captcha_required
                _log("   [renew] 帶 token 仍被攔")
            else:
                _log("   [renew] 純 API 驗證未過")

    return r, captcha_required
