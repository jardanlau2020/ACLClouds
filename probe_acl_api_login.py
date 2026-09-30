#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探針 v2：ACLClouds「純 API 登入」可行性（帶自家卡片驗證的純 API 解卡）。

背景
----
2026-09-30 連續三單紅燈：cache cookie 過期 → 401 → 腳本退瀏覽器 fallback，
但瀏覽器路徑睇唔到頁面上嘅 Cap 卡片挑戰（`_challenge_pending()` 只認
"i am not a robot" / "Click on X"），表單冇 captcha_token 就交 → 頁面彈
「Erreur / Captcha incorrect.」。

但主腳本其實已經有**純 API 解卡**能力（`solve_captcha_api`，09-16 逆向，
用於 context=renewal_gate）。本探針驗證同一套邏輯搬去 context=login 能唔能夠
換到 captcha_token 並完成 `POST /auth/login`。

只做登入 + 讀 /api/client，**唔做任何續期動作**。
"""
import json
import os
import random
import sys
import urllib.parse

import requests

BASE = "https://aclclouds.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
EMAIL = os.environ.get("ACL_EMAIL", "").strip()
PASSWORD = os.environ.get("ACL_PASSWORD", "").strip()
CONTEXT = os.environ.get("ACL_CAPTCHA_CONTEXT", "login")
MAX_ROUNDS = 6

s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept": "application/json",
                  "X-Requested-With": "XMLHttpRequest",
                  "Referer": BASE + "/auth/login", "Origin": BASE})


def xsrf():
    return urllib.parse.unquote(s.cookies.get("XSRF-TOKEN", ""))


# ── OCR 引擎（同 renew_fixed.py 一樣：ddddocr）──────────────────────────────
_ocr_engine = None
try:
    import ddddocr
    _ocr_engine = ddddocr.DdddOcr(show_ad=False)
    print("🔤 OCR 引擎: ddddocr 已載入")
except Exception as e:  # noqa: BLE001
    print(f"⚠️ OCR 引擎載入失敗: {e}")


def ocr(png_bytes):
    if _ocr_engine is None:
        return ""
    try:
        return (_ocr_engine.classification(png_bytes) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def show(tag, r, n=260):
    body = (r.text or "").replace("\n", " ")
    print(f"  [{tag}] HTTP {r.status_code} {body[:n]}")


print("== 1) csrf-cookie")
show("sanctum", s.get(BASE + "/sanctum/csrf-cookie", timeout=25), 60)
print("   cookies:", list(s.cookies.keys()))

print(f"== 2) challenge (context={CONTEXT})")
r = s.get(BASE + f"/auth/captcha/challenge?context={CONTEXT}", timeout=25)
show("challenge", r)
if r.status_code != 200:
    sys.exit(1)
c = r.json()
base = {"context": c.get("context") or CONTEXT,
        "id": c.get("id"), "ts": c.get("ts"), "sig": c.get("sig")}
print(f"   id={str(base['id'])[:16]}... ts={base['ts']} sig={str(base['sig'])[:16]}...")

token = None
for rnd in range(1, MAX_ROUNDS + 1):
    print(f"== 3.{rnd}) POST /auth/captcha (round {rnd})")
    r = s.post(BASE + "/auth/captcha",
               json=dict(base, elapsed=random.randint(1800, 9000)),
               headers={"X-XSRF-TOKEN": xsrf()}, timeout=30)
    show("captcha", r)
    if r.status_code != 200:
        break
    a = r.json()
    if a.get("passed") and a.get("token"):
        token = a["token"]
        print(f"   ✅ 第 {rnd} 輪直接通過, captcha_token len={len(token)}")
        break
    opts = a.get("options") or []
    if not (a.get("interactive") and opts):
        print(f"   ⚠️ 非互動且未通過: {json.dumps(a, ensure_ascii=False)[:200]}")
        break
    # 跟刷新後嘅 id/ts/sig（09-16 鐵律：用舊 id 交答案會被靜默拒）
    if a.get("id") and a.get("sig"):
        base = {"context": a.get("context") or base.get("context"),
                "id": a["id"], "ts": a.get("ts") or base.get("ts"), "sig": a["sig"]}
    target = (a.get("target") or "").strip()
    asig = a.get("answer_sig") or ""
    print(f"   🎯 目標 '{target}' / {len(opts)} 張卡 (answer_sig len={len(asig)})")
    best, texts = None, []
    for i, op in enumerate(opts):
        ir = s.get(BASE + "/auth/captcha/image?t=" + urllib.parse.quote(op), timeout=25)
        txt = ocr(ir.content) if ir.status_code == 200 else ""
        texts.append(txt)
        print(f"      卡{i}: HTTP {ir.status_code} {len(ir.content)}B OCR='{txt}'")
        lo = txt.lower()
        tl = target.lower()
        if tl and (tl in lo or (lo and lo in tl)):
            best = op
            break
    if best is None:
        print(f"   ❌ OCR 冇一張中目標 (讀到: {texts}) → 唔盲交")
        break
    r2 = s.post(BASE + "/auth/captcha",
                json=dict(base, answer=best, answer_sig=asig, target=target),
                headers={"X-XSRF-TOKEN": xsrf()}, timeout=30)
    show("answer", r2)
    b = {}
    try:
        b = r2.json()
    except Exception:  # noqa: BLE001
        pass
    if b.get("passed") and b.get("token"):
        token = b["token"]
        print(f"   ✅ 第 {rnd} 輪卡片過關, captcha_token len={len(token)}")
        break
    if b.get("id") and b.get("sig"):
        base = {"context": b.get("context") or base.get("context"),
                "id": b["id"], "ts": b.get("ts") or base.get("ts"), "sig": b["sig"]}
    if not b.get("interactive"):
        print(f"   ⚠️ 卡片後仍未過: {json.dumps(b, ensure_ascii=False)[:200]}")
        break

if not token:
    print("== 結論: ❌ 攞唔到 captcha_token, 純 API 登入此路不通")
    sys.exit(0)

print("== 4) POST /auth/login (真憑證 + captcha_token)")
for payload in ({"captcha_token": token, "email": EMAIL, "password": PASSWORD, "remember": True},
                {"captcha_token": token, "email": EMAIL, "password": PASSWORD}):
    r = s.post(BASE + "/auth/login", json=payload, headers={"X-XSRF-TOKEN": xsrf()}, timeout=30)
    show("login", r, 200)
    if r.status_code in (200, 204):
        break
print("   🍪 cookies:", list(s.cookies.keys()))

chk = s.get(BASE + "/api/client", timeout=25)
show("/api/client", chk, 200)
if chk.status_code == 200:
    sess = s.cookies.get("__Host-aclclouds_session", "") or s.cookies.get("aclclouds_session", "")
    print(f"   🎉 純 API 登入成功！session len={len(sess)} sha256[0:8]="
          f"{__import__('hashlib').sha256(sess.encode()).hexdigest()[:8]}")
else:
    print("== 結論: ❌ captcha 過咗但登入未成功（睇上面 login 回應）")
sys.exit(0)
