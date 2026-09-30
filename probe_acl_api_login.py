#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探針 v3：ACLClouds 純 API 登入（校正 /auth/login 嘅正確欄位）。

v2 結果：卡片挑戰解得開、captcha_token 到手（ctx=login），但
`POST /auth/login {captcha_token,email,password,remember}` 回 422 "Captcha incorrect."。

由前端 bundle `6893.js` 反編譯出真正嘅請求（註冊/登入共用）：

    axios.get("/sanctum/csrf-cookie")
      .then(() => axios.post("/auth/login", {
          user:            username,
          password:        password,
          remember:        remember,
          captcha_token:   captchaToken,   // /auth/captcha 過關後嗰個 token
          captcha_answer:  captchaAnswer,  // 揀咗嗰張卡嘅 option token
      }))

⇒ v2 缺 `captcha_answer`、又用咗 `email` 而唔係 `user`，所以被當「Captcha incorrect」。
本探針逐個變體試，全部重新解卡（token 每次新鮮）。

只做登入 + 讀 /api/client，唔做任何續期動作。
"""
import hashlib
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


def show(tag, r, n=300):
    print(f"  [{tag}] HTTP {r.status_code} {(r.text or '').replace(chr(10),' ')[:n]}")


def solve_captcha(context=CONTEXT):
    """解卡 → (token, chosen_answer) 或 (None, None)。協議同 renew_fixed.py v2 一致。"""
    s.get(BASE + "/sanctum/csrf-cookie", timeout=25)
    r = s.get(BASE + f"/auth/captcha/challenge?context={context}", timeout=25)
    if r.status_code != 200:
        print(f"   challenge HTTP {r.status_code}")
        return None, None
    c = r.json()
    base = {"context": c.get("context") or context, "id": c.get("id"),
            "ts": c.get("ts"), "sig": c.get("sig")}
    for rnd in range(1, MAX_ROUNDS + 1):
        r = s.post(BASE + "/auth/captcha", json=dict(base, elapsed=random.randint(1800, 9000)),
                   headers={"X-XSRF-TOKEN": xsrf()}, timeout=30)
        if r.status_code != 200:
            print(f"   回合{rnd} HTTP {r.status_code}")
            return None, None
        a = r.json()
        if a.get("passed") and a.get("token"):
            print(f"   回合{rnd}: ✅ 直接過")
            return a["token"], None
        opts = a.get("options") or []
        if not (a.get("interactive") and opts):
            print(f"   回合{rnd}: 非互動未過 {json.dumps(a, ensure_ascii=False)[:160]}")
            return None, None
        if a.get("id") and a.get("sig"):
            base = {"context": a.get("context") or base.get("context"), "id": a["id"],
                    "ts": a.get("ts") or base.get("ts"), "sig": a["sig"]}
        target = (a.get("target") or "").strip()
        asig = a.get("answer_sig") or ""
        best = None
        for i, op in enumerate(opts):
            ir = s.get(BASE + "/auth/captcha/image?t=" + urllib.parse.quote(op), timeout=25)
            txt = ocr(ir.content) if ir.status_code == 200 else ""
            print(f"      卡{i}: OCR='{txt}'")
            lo, tl = txt.lower(), target.lower()
            if tl and (tl in lo or (lo and lo in tl)):
                best = op
                break
        if best is None:
            print(f"   回合{rnd}: OCR 冇中 '{target}' → 唔盲交")
            return None, None
        r2 = s.post(BASE + "/auth/captcha", json=dict(base, answer=best, answer_sig=asig, target=target),
                    headers={"X-XSRF-TOKEN": xsrf()}, timeout=30)
        b = {}
        try:
            b = r2.json()
        except Exception:  # noqa: BLE001
            pass
        if b.get("passed") and b.get("token"):
            print(f"   回合{rnd}: ✅ 卡片過關 (目標 '{target}')")
            return b["token"], best
        if b.get("id") and b.get("sig"):
            base = {"context": b.get("context") or base.get("context"), "id": b["id"],
                    "ts": b.get("ts") or base.get("ts"), "sig": b["sig"]}
        if not b.get("interactive"):
            print(f"   回合{rnd}: 卡片後未過 {json.dumps(b, ensure_ascii=False)[:160]}")
            return None, None
    return None, None


# 2026-09-30 由 4736.js（login route chunk）實錘：
#   React.createElement(y.A, {context: "login", onVerify: (e, t) => { i(e); u(t) }})
#   → captchaToken = e = token；captchaAnswer = t = **"human"**（widget 呼 onVerify(token,"human")）
#   ⇒ /auth/login 嘅 captcha_answer 係固定字串 "human"，唔係揀咗嗰張卡！
VARIANTS = [
    ("V1 captcha_answer='human'", lambda tok, ans: {"user": EMAIL, "password": PASSWORD, "remember": True,
                                                    "captcha_token": tok, "captcha_answer": "human"}),
    ("V2 'human' 但用 email 鍵", lambda tok, ans: {"email": EMAIL, "password": PASSWORD, "remember": True,
                                                  "captcha_token": tok, "captcha_answer": "human"}),
    ("V3 'human' 無 remember", lambda tok, ans: {"user": EMAIL, "password": PASSWORD,
                                                 "captcha_token": tok, "captcha_answer": "human"}),
]

for name, build in VARIANTS:
    print(f"════ 變體 {name} ════")
    tok, ans = solve_captcha()
    if not tok:
        print("   ❌ 解卡失敗, 跳過")
        continue
    print(f"   token len={len(tok)} chosen_answer={'有' if ans else '無'}")
    r = s.post(BASE + "/auth/login", json=build(tok, ans),
               headers={"X-XSRF-TOKEN": xsrf()}, timeout=30)
    show("login", r, 400)
    if r.status_code in (200, 204):
        chk = s.get(BASE + "/api/client", timeout=25)
        show("/api/client", chk, 160)
        if chk.status_code == 200:
            sess = s.cookies.get("__Host-aclclouds_session", "") or s.cookies.get("aclclouds_session", "")
            print(f"   🎉 純 API 登入成功！session len={len(sess)} "
                  f"sha256[0:8]={hashlib.sha256(sess.encode()).hexdigest()[:8]}")
            print(f"   結論: 用變體「{name}」")
            sys.exit(0)
        print("   ⚠️ login 2xx 但 /api/client 未認, 睇上面回應")
    if r.status_code == 422 and "aptcha" not in (r.text or ""):
        print("   ⚠️ 422 但唔係 captcha 錯 → 可能係憑證/欄位問題")

print("== 結論: ❌ 三個變體都未成功, 需再查前端 login 流程")
sys.exit(0)
