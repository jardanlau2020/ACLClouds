#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACLClouds 純 API 登入探針（方案①前置 · 只登入 + 驗 session · 絕不續期）

背景（2026-10-05 用戶批「做 ①」後實測）：
  登入閘真兇 = Cap（tiagozip/cap 自架 PoW，sitekey `235a82a3e3`，
  endpoint `cap.aclclouds.com`）；自家 `/auth/captcha` 卡片只屬 renew 端點。
  登入 = application/x-www-form-urlencoded + sanctum XSRF + `captcha_token` 欄位。

本探針做乜：
  1. Cap /challenge → node PoW（cap_core/solve.mjs）→ /redeem 攞一次性 token
  2. 真帳密 POST /auth/login（欄位先用零成本 required 探測，最多一次真嘗試）
  3. GET /api/client + /api/client/account 證 session 有效（只讀）

模式：
  真帳密（GHA）：ACL_EMAIL / ACL_PASSWORD 由 secrets 注入
  通道自檢（本地）：ACL_FAKE=1 → 假 email，只驗 captcha+CSRF+表單通道
      （收到 "No account matching..." 即算通道 OK —— 10-05 同款判準）

退出碼：0 成功／2 登入被拒（帳密或欄位）／3 CSRF 或 Cap 通道問題／4 缺 secret／5 其他
紀律：永不打印密碼；email 遮罩；零續期／零寫操作；真嘗試最多 1-2 次防鎖定。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

import requests

BASE = "https://aclclouds.com"
CAP_SITEKEY = "235a82a3e3"
CAP_API = f"https://cap.aclclouds.com/{CAP_SITEKEY}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 30

REACHED_CREDS = re.compile(r"No account matching|invalid credentials", re.I)
RE_CAPTCHA_BAD = re.compile(r"Captcha incorrect", re.I)
RE_FIELD_REQ = re.compile(
    r"The (\w+) field is required"        # Laravel en
    r"|Le champ (\w+) est obligatoire"    # Laravel fr
)
IDENT_CANDIDATES = ("user", "email", "username")


def mask_email(e: str) -> str:
    try:
        local, dom = e.split("@", 1)
        return (local[:2] + "***" if local else "***") + "@" + dom
    except ValueError:
        return "***"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def solve_cap(s: requests.Session) -> tuple[str, int]:
    """Cap challenge → PoW（node）→ redeem → 一次性 token。"""
    r = s.post(f"{CAP_API}/challenge", timeout=TIMEOUT)
    r.raise_for_status()
    chal = r.json()
    n_ch = len(chal.get("challenges") or [])
    if not n_ch:
        raise RuntimeError(f"challenge 無 challenges: {str(chal)[:160]}")
    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, "c.json")
        out = os.path.join(td, "s.json")
        with open(inp, "w") as f:
            json.dump(chal, f)
        p = subprocess.run(
            ["node", os.path.join("cap_core", "solve.mjs"), inp, out],
            capture_output=True, text=True, timeout=300, cwd=HERE,
        )
        if p.returncode != 0:
            raise RuntimeError(
                f"solve.mjs rc={p.returncode}: {(p.stderr or p.stdout)[-300:]}")
        with open(out) as f:
            sol = json.load(f)
    r2 = s.post(
        f"{CAP_API}/redeem",
        json={"token": sol["token"], "solutions": sol["solutions"]},
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    try:
        j2 = r2.json()
    except ValueError:
        raise RuntimeError(f"redeem 非 JSON HTTP {r2.status_code}: {r2.text[:200]}")
    if not j2.get("success") or not j2.get("token"):
        raise RuntimeError(f"redeem fail HTTP {r2.status_code}: {str(j2)[:200]}")
    return str(j2["token"]), n_ch


def xsrf_value(s: requests.Session) -> str | None:
    val = None
    for c in s.cookies:
        if c.name == "XSRF-TOKEN":
            val = c.value
    if not val:
        return None
    # cookie jar 可能存編碼前後兩種形態；base64 字面冇 %，unquote 幂等安全
    return requests.utils.unquote(val)


def login_post(s: requests.Session, data: dict) -> requests.Response:
    """走瀏覽器時序：開頁 → sanctum csrf → form-urlencoded POST。"""
    s.get(BASE + "/auth/login", timeout=TIMEOUT)
    s.get(BASE + "/sanctum/csrf-cookie", timeout=TIMEOUT)  # 204 + XSRF-TOKEN
    xsrf = xsrf_value(s)
    if not xsrf:
        raise RuntimeError("攞唔到 XSRF-TOKEN cookie（CSRF 通道死）")
    headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRF-TOKEN": xsrf,
        "Referer": BASE + "/auth/login",
        "Origin": BASE,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    return s.post(BASE + "/auth/login", data=data, headers=headers,
                  timeout=TIMEOUT, allow_redirects=True)


def error_text(r: requests.Response) -> str:
    try:
        j = r.json()
    except ValueError:
        return r.text[:300]
    parts = []
    for k in ("message", "detail"):
        if j.get(k):
            parts.append(str(j[k]))
    errs = j.get("errors")
    if isinstance(errs, dict):
        for k, v in errs.items():
            parts.append(f"{k}: {'; '.join(v) if isinstance(v, list) else v}")
    return " | ".join(parts) or r.text[:300]


def verify_session(s: requests.Session) -> int:
    r = s.get(BASE + "/api/client",
               headers={"Accept": "application/json"}, timeout=TIMEOUT)
    if r.status_code != 200:
        return 5
    data = r.json().get("data") or []
    print(f"✅ 登入成功 · GET /api/client → 200 · {len(data)} 個服務")
    for item in data:
        a = item.get("attributes") or {}
        if not a.get("identifier"):
            continue
        print(f"   · {a.get('name')} · id={a.get('identifier')} · "
              f"到期 {a.get('expires_at')} · can_renew={a.get('can_renew')}")
    try:
        r3 = s.get(BASE + "/api/client/account",
                   headers={"Accept": "application/json"}, timeout=TIMEOUT)
        if r3.status_code == 200:
            acc = r3.json()
            if isinstance(acc, dict) and isinstance(acc.get("data"), dict):
                acc = acc["data"]
            em = str(acc.get("email") or "")
            if em:
                print(f"   帳號身分: {mask_email(em)} · "
                      f"email_verified={'yes' if acc.get('email_verified_at') else 'no'}")
    except Exception as e:  # noqa: BLE001 — 帳號身份係額外資訊，失敗唔阻擋結論
        print(f"   （account 讀取失敗，不阻擋：{e}）")
    return 0


def main() -> int:
    fake = os.environ.get("ACL_FAKE") == "1"
    email = ("nonexistent-probe-zzz@example.invalid" if fake
             else os.environ.get("ACL_EMAIL", "").strip())
    password = "probe-invalid-password" if fake else os.environ.get("ACL_PASSWORD", "")
    if not fake and (not email or not password):
        print("❌ 缺 ACL_EMAIL / ACL_PASSWORD（secrets 未注入）")
        return 4

    print(f"模式: {'FAKE 通道自檢' if fake else '真帳密'} · 帳號 {mask_email(email)}")

    nv = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if nv.returncode != 0:
        print("❌ 環境冇 node（PoW 需要）")
        return 5
    print(f"node {nv.stdout.strip()}")

    s = make_session()

    # 1) Cap 通道
    try:
        tok, n_ch = solve_cap(s)
        print(f"✅ Cap PoW 解決（{n_ch} 挑戰）· token len={len(tok)}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ Cap 通道失敗: {e}")
        return 3

    # 2) 標識欄位探測（真帳密模式；只交 password，後端 required 規則會點名欄位，
    #    零帳密消耗 —— validation 失敗唔會入 credential 檢查）
    ident_field = "user"  # bundle 證據：user:e,password:t,remember,captcha_token
    tok2 = None
    if not fake:
        try:
            r = login_post(s, {"password": password, "captcha_token": tok})
            txt = error_text(r)
            m = RE_FIELD_REQ.search(txt)
            if m:
                cand = (m.group(1) or m.group(2) or "").lower()
                if cand in IDENT_CANDIDATES:
                    ident_field = cand
                    print(f"🔍 required 欄位探測 → 標識欄 = `{ident_field}`（零帳密消耗）")
                else:
                    print(f"🔍 required 欄位 = `{cand}`（非標識欄，維持 `{ident_field}`）· {txt[:120]}")
            elif RE_CAPTCHA_BAD.search(txt):
                print(f"❌ Cap token 不被登入端接受: {txt[:200]}")
                return 3
            else:
                print(f"ℹ️ 探測回應（維持 `{ident_field}`）: HTTP {r.status_code} {txt[:160]}")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 欄位探測失敗（維持 `user`）: {e}")

    # 3) 登錄：真模式重解一枚新 token（探測已燒第一枚）；FAKE 模式直接用第一枚
    try:
        if fake:
            login_tok = tok
        else:
            login_tok, n_ch = solve_cap(s)
            print(f"✅ 第二輪 Cap token 就位（{n_ch} 挑戰）")
        r = login_post(s, {ident_field: email, "password": password,
                           "captcha_token": login_tok})
    except Exception as e:  # noqa: BLE001
        print(f"❌ 登入請求失敗: {e}")
        return 3

    txt = error_text(r)
    status = r.status_code

    if RE_CAPTCHA_BAD.search(txt):
        print(f"❌ Cap token 被拒: HTTP {status} {txt[:240]}")
        return 3

    if fake:
        if REACHED_CREDS.search(txt):
            print("✅ 通道確認：已達 credential 檢查（captcha + CSRF + 表單欄位全通）")
            return 0
        print(f"⚠️ 未達 credential 階段: HTTP {status} {txt[:240]}")
        return 5

    # 4) 真模式：session 驗證（只讀）
    rc = verify_session(s)
    if rc == 0:
        return 0
    if REACHED_CREDS.search(txt) or status in (401, 403, 422):
        print(f"❌ 登入被拒 · HTTP {status} · {txt[:300]}")
        print("   → 可能：密碼已改／欄位仍錯。唔再重試（防鎖定），要人手跟進。")
        return 2
    # 登入回應唔似失敗但 /api/client 唔 200 —— 誠實報告
    print(f"⚠️ 登入回應 HTTP {status}（{txt[:200]}）但 session 驗證失敗 "
          f"GET /api/client → 非 200")
    return 5


if __name__ == "__main__":
    sys.exit(main())
