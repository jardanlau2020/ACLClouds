#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探測 ACLClouds 純 API 登入可行性（唔用瀏覽器）。

背景：2026-09-20 17:1x UTC 起登入頁嘅卡片挑戰圖全空白（/auth/captcha/image 回 500），
瀏覽器登入死路。但如果 GHA runner 嘅 IP 喺 context=login 之下 captcha 直接 auto-pass
（17:26 run 嘅健康探測就係咁：challenge 無卡片選項），咁純 API 登入就唔使圖都得。

本探針只讀 / 登入，唔做續期。憑證由 secrets 注入（同主 workflow 一樣）。
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

s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept": "application/json",
                  "X-Requested-With": "XMLHttpRequest",
                  "Referer": BASE + "/auth/login", "Origin": BASE})


def xsrf():
    return urllib.parse.unquote(s.cookies.get("XSRF-TOKEN", ""))


def show(tag, r, n=300):
    body = r.text.replace("\n", " ")
    print(f"  [{tag}] HTTP {r.status_code} {body[:n]}")


print("== 1) csrf-cookie")
show("sanctum", s.get(BASE + "/sanctum/csrf-cookie"), 80)

print("== 2) challenge (context=login)")
r = s.get(BASE + "/auth/captcha/challenge?context=login")
show("challenge", r)
ch = r.json() if r.status_code == 200 else {}
base = {k: ch[k] for k in ("id", "ts", "sig") if k in ch}
base["context"] = "login"

print("== 3) POST /auth/captcha → 睇係 auto-pass 定要卡片")
r = s.post(BASE + "/auth/captcha",
           json=dict(base, elapsed=random.randint(1800, 9000)),
           headers={"X-XSRF-TOKEN": xsrf()})
show("captcha", r)
token = None
d = {}
try:
    d = r.json()
except Exception:
    pass
if d.get("passed") and d.get("token"):
    token = d["token"]
    print("  ✅ captcha auto-pass, 有 token")
else:
    print("  ⚠️ 需要互動卡片；options:", len(d.get("options") or []), "target:", d.get("target"))
    opts = d.get("options") or []
    if opts:
        ir = s.get(BASE + f"/auth/captcha/image?t={urllib.parse.quote(opts[0])}")
        print(f"  🖼 image HTTP {ir.status_code} len={len(ir.content)}")
        if ir.status_code == 200:
            open("/tmp/probe_card0.png", "wb").write(ir.content)
    # 跟刷新 id 原則
    for k in ("id", "ts", "sig"):
        if k in d:
            base[k] = d[k]

print("== 4) 用空 body POST /auth/login（睇 captcha 收唔收 + 揭示欄位）")
r = s.post(BASE + "/auth/login", json={},
           headers={"X-XSRF-TOKEN": xsrf(),
                    **({"X-Captcha-Token": token} if token else {})})
show("login空", r, 500)

if token:
    print("== 5) 假憑證 + captcha_token（睇驗證錯揭示欄位名）")
    for payload in ({"captcha_token": token, "email": "x@example.com", "password": "x"},
                    {"captcha_token": token, "user": "x@example.com", "password": "x"},
                    {"captcha_token": token, "username": "x@example.com", "password": "x"}):
        r = s.post(BASE + "/auth/login", json=payload, headers={"X-XSRF-TOKEN": xsrf()})
        show("login" + list(payload.keys())[1], r, 300)

    if EMAIL and PASSWORD:
        print("== 6) 真憑證 + captcha_token → 試真登入（會產生 session，唔做續期）")
        r = s.post(BASE + "/auth/login",
                   json={"captcha_token": token, "email": EMAIL, "password": PASSWORD,
                         "remember": True},
                   headers={"X-XSRF-TOKEN": xsrf()})
        show("真登入", r, 300)
        print("  🍪 cookie 名:", list(s.cookies.keys()))
        if r.status_code in (200, 204):
            chk = s.get(BASE + "/api/client")
            print(f"  ✅ /api/client HTTP {chk.status_code} (200 = 登入成功)")
            if chk.status_code == 200:
                sess = s.cookies.get("__Host-aclclouds_session", "")
                print("  🎉 純 API 登入成功！session 前 8 位:", sess[:8], "長度:", len(sess))
else:
    print("== 5/6) 冇 captcha_token，跳過真登入測試")

sys.exit(0)
