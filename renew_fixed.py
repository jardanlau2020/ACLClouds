#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACLClouds 自动续期脚本 - 适配 2026-09 aclclouds.com 改版
========================================================
改版后关键变化 (均已适配):
1. 面板从 dash.aclclouds.com 迁到 aclclouds.com/dashboard,
   dash.aclclouds.com 所有请求 302 -> aclclouds.com。
   => BASE_URL 必须指向 https://aclclouds.com
   (否则 requests 跨域重定向时会丢弃手动注入的 Cookie 头 -> 401)
2. 服务器响应新增字段: expires_at / can_renew / free_renewals_remaining /
   free_renewals_max / plan.renewal_days / is_free / service_type
3. 续期接口仍为 POST /api/client/servers/{id}/upgrade/renew
   (未登录时返回 401 而非 404, 说明路由存在)
4. 免费 Minecraft 提前 2 小时可续; 免费服务按周期(每4天/每6h等);
   付费服务提前 4 天。

用法:
    export ACL_COOKIES="XSRF-TOKEN=...; __Host-aclclouds_session=..."
    export TG_BOT_TOKEN=...  TG_CHAT_ID=...   # 可选
    export DEBUG=1                             # 可选, 打印原始响应
    python renew_fixed.py
"""

import os
import sys
import json
import time
import re
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests

# OCR 引擎 (点选题卡片是图片, 需识别卡面文字; 参照 bo-aclclouds 方案)
try:
    import ddddocr
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

_ocr_engine = None
if OCR_AVAILABLE:
    try:
        _ocr_engine = ddddocr.DdddOcr(show_ad=False)
    except Exception:
        _ocr_engine = None


def _ocr_card_text(png_bytes):
    """OCR 单张卡片截图, 返回识别文字 (失败返回空串)"""
    if _ocr_engine is None:
        return ""
    try:
        return (_ocr_engine.classification(png_bytes) or "").strip()
    except Exception:
        return ""


# ==================== 配置 ====================
# 改版后 API 直接由 aclclouds.com 提供; 不要再指到 dash.aclclouds.com
# (dash 会 302 到主域, 重定向时 Cookie 会被 requests 丢弃导致 401)
BASE_URL = os.environ.get("ACL_BASE_URL", "https://aclclouds.com").rstrip("/")
RENEW_THRESHOLD_HOURS = float(os.environ.get("RENEW_THRESHOLD_HOURS", "48"))

# Cookie: 完整的浏览器 Cookie 字符串, 必须来自 https://aclclouds.com
# 至少包含 XSRF-TOKEN 和 __Host-aclclouds_session
COOKIE = os.environ.get("ACL_COOKIES", "").strip()

# Cookie 缓存文件 (GitHub Actions cache 持久化, 每单跑批后刷新, 免人手换 cookie)
# 有 actions/cache 时用缓存文件 (比 env/secret 的 cookie 更新); 冇缓存时落 ACL_COOKIES
COOKIE_CACHE_FILE = os.environ.get("ACL_COOKIE_CACHE_FILE", "").strip()

def _load_cookie_cache():
    if COOKIE_CACHE_FILE and os.path.exists(COOKIE_CACHE_FILE):
        try:
            v = open(COOKIE_CACHE_FILE).read().strip()
            if v:
                print(f"📥 使用缓存 Cookie ({len(v)} 字符, 上一单写入)")
                return v
        except Exception:
            pass
    return ""

def save_cookie_cache(cookie_str):
    """把最新可用 Cookie 寫入緩存文件; 單尾 actions/cache 會把它持久化到 GitHub cache"""
    if not COOKIE_CACHE_FILE or not (cookie_str or "").strip():
        return
    try:
        with open(COOKIE_CACHE_FILE, "w") as f:
            f.write(cookie_str.strip() + "\n")
        log(f"💾 最新 Cookie 已寫入緩存文件 ({len(cookie_str.strip())} 字符)")
    except Exception as e:
        log(f"⚠️ Cookie 緩存文件寫入失敗: {e}")

COOKIE = _load_cookie_cache() or COOKIE

# 多账号支持 (可选), 格式: name1|||cookie1\nname2|||cookie2
MULTI_ACCOUNTS = os.environ.get("ACL_ACCOUNTS", "").strip()

# TG 通知
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

# 调试: 打印每个请求的原始响应
DEBUG = os.environ.get("DEBUG", "0") == "1"

# ==================== 浏览器自动登录 (可选, 免手动换 Cookie) ====================
# 配置 ACL_EMAIL + ACL_PASSWORD 后, Cookie 过期时脚本会用浏览器自动登录
# (过 Turnstile 需要干净代理, 与 bot-hosting 方案一致)
ACL_EMAIL = os.environ.get("ACL_EMAIL", "").strip()
ACL_PASSWORD = os.environ.get("ACL_PASSWORD", "").strip()
# Discord OAuth 回退登录 (账号需绑定 Discord; Turnstile 组件加载不出时自动使用)
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "socks5://127.0.0.1:1080"
HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()          # 自动更新 ACL_COOKIES Secret
# 不设 GH_REPO 就禁止自动回写 Cookie，避免误写上游仓库。
GH_REPO = os.environ.get("GH_REPO", "").strip()

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ==================== 工具函数 ====================
def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg):
    print(msg, flush=True)


def debug(msg):
    if DEBUG:
        print(f"  🐛 {msg}", flush=True)


def send_tg(text):
    """发送 TG 通知 (纯文本, 避免 Markdown 特殊字符解析失败); 返回是否成功"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("ℹ️ 未配置 TG_BOT_TOKEN / TG_CHAT_ID, 已跳过 TG 通知")
        log("   GitHub Actions: Settings → Secrets and variables → Actions 添加这两个 secret")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=15,
        )
        if r.status_code == 200:
            log("📨 TG 通知已发送")
            return True
        log(f"❌ TG 发送失败 HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        log(f"⚠️ TG 推送异常: {e}")
        return False


def fmt_remaining(seconds):
    if seconds is None:
        return "?"
    if seconds < 0:
        return "已过期"
    seconds = int(seconds)
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    if d > 0:
        return f"{d}d {h}h {m}m"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def parse_iso(s):
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ==================== API 会话 (纯 HTTP) ====================
def sanitize_cookie(cookie_str):
    """清洗 Cookie 字符串, 保证可以作为 HTTP header 发送

    常见坑 (会触发 requests 的 Invalid header value 异常):
    - 复制时带了换行/回车
    - 误粘贴了 curl 的 "Cookie: ..." 前缀
    - 值带了引号或非 latin-1 字符 (中文/emoji)
    返回清洗后的 "; " 分隔字符串。
    """
    s = (cookie_str or "").strip()
    if not s:
        return ""
    # 去掉误粘贴的 "Cookie:" 前缀
    if s.lower().startswith("cookie:"):
        s = s[len("cookie:"):].strip()
    # 去掉两端包裹的引号
    if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
        s = s[1:-1]
    # 折叠所有空白/控制字符 (换行、回车、制表符等)
    s = re.sub(r"\s+", " ", s)

    parts = []
    for kv in s.split(";"):
        kv = kv.strip()
        if not kv or "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        # 丢弃含非法字符的条目 (非 latin-1 或控制字符), 避免 header 校验失败
        try:
            b = v.encode("latin-1")
        except UnicodeEncodeError:
            log(f"⚠️ Cookie 条目 {k} 含非 latin-1 字符, 已丢弃 (请重新复制)")
            continue
        if any(c < 0x20 or c == 0x7f for c in b):
            log(f"⚠️ Cookie 条目 {k} 含控制字符, 已丢弃 (请重新复制)")
            continue
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def build_api_session(cookie_str):
    """构建 API session, 原样保留 __Host- 前缀 Cookie

    关键: 不再用 s.cookies.set (cookiejar 会剥离 __Host- 前缀,
    且 prepare_cookies 会用剥离后的名字重建 Cookie 头导致服务器不识别),
    而是保存原始字符串, 在每次请求时强制覆盖 Cookie 头。
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/dashboard",
    })
    s._raw_cookie = sanitize_cookie(cookie_str)
    if IS_PROXY and PROXY_SERVER:
        s.proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER}
        s.trust_env = False
        log(f"🔗 API session 走代理: {PROXY_SERVER.split('@')[-1]} (隐藏凭证)")
    return s


def get_xsrf(session):
    """从原始 Cookie 字符串提取 XSRF-TOKEN (Laravel 加密值, URL 解码后放 header)"""
    raw = getattr(session, '_raw_cookie', '')
    if raw:
        m = re.search(r'(?:^|;\s*)XSRF-TOKEN=([^;]+)', raw)
        if m:
            return urllib.parse.unquote(m.group(1).strip())
    return None


def _send(session, method, path, payload=None):
    """发送请求: 注入 X-XSRF-TOKEN 头 + 强制覆盖原始 Cookie 头"""
    headers = {}
    token = get_xsrf(session)
    if token:
        headers["X-XSRF-TOKEN"] = token

    req = requests.Request(method, f"{BASE_URL}{path}",
                           headers=headers, json=payload)
    prepared = session.prepare_request(req)
    # prepare_cookies 会删掉手动 Cookie 头, 这里写回原样 (含 __Host- 前缀)
    if getattr(session, '_raw_cookie', None):
        prepared.headers['Cookie'] = session._raw_cookie
    r = session.send(prepared, timeout=30)

    # 重定向防护: 如果响应 302 且跳去了别的域, Cookie 已被 requests 丢弃,
    # 手动把 Location 和目标打出来, 方便定位"BASE_URL 指错域"问题
    if r.is_redirect:
        debug(f"{method} {path} -> 302 {r.headers.get('Location')} (重定向后 Cookie 会被丢弃, 检查 ACL_BASE_URL)")

    debug(f"{method} {path} -> HTTP {r.status_code} | {r.text[:300]}")
    return r


def api_get(session, path):
    return _send(session, "GET", path)


def api_post(session, path, payload=None):
    return _send(session, "POST", path, payload or {})


# ==================== 数据解析 ====================
def list_servers(session):
    """拉取服务器列表 (改版后仍为 GET /api/client)"""
    r = api_get(session, "/api/client")
    if r.status_code == 401:
        log(f"❌ 登录失败 (401): Cookie 无效或过期")
        log(f"   请重新登录 https://aclclouds.com/dashboard 并复制新 Cookie")
        return []
    if r.status_code != 200:
        log(f"❌ /api/client 返回 HTTP {r.status_code}: {r.text[:300]}")
        return []
    j = r.json()
    if isinstance(j, dict):
        return j.get("data", [])
    return j if isinstance(j, list) else []


def server_detail(session, sid):
    r = api_get(session, f"/api/client/servers/{sid}")
    if r.status_code != 200:
        debug(f"server_detail {sid} -> HTTP {r.status_code}")
        return None
    try:
        j = r.json()
        return j.get("attributes", j) if isinstance(j, dict) else j
    except Exception:
        return None


def extract_server_id(attrs):
    """提取续期接口需要的短标识 (Pterodactyl 短 id = uuid 前 8 位)

    改版后 attributes 里可能只有 uuid (完整 UUID), 直接用完整 UUID 调
    /api/client/servers/{id} 会得到 404, 这里自动截取前 8 位。
    """
    if not isinstance(attrs, dict):
        return None
    for k in ("identifier", "uuid_short", "short_uuid", "server_id"):
        v = attrs.get(k)
        if v:
            return str(v)
    u = attrs.get("uuid") or attrs.get("id")
    if u:
        u = str(u)
        if "-" in u:          # 完整 UUID 形如 xxxxxxxx-xxxx-...
            return u.split("-")[0]
        return u[:8]
    return None


def find_expire(attrs, detail=None):
    """从多个可能字段找到期时间 (改版后主字段为 expires_at)"""
    candidates = []
    if attrs:
        candidates.append(attrs)
    if detail:
        candidates.append(detail)
    for c in list(candidates):
        rel = c.get("relationships") if isinstance(c, dict) else None
        if rel:
            candidates.append(rel)
    for c in candidates:
        if not isinstance(c, dict):
            continue
        for key in ("expires_at", "expire_at", "renew_at", "renewable_at",
                    "expiration_date", "expires", "expiry"):
            v = c.get(key)
            if v:
                return key, v
    return None, None


def renewal_availability(attrs, detail=None):
    """读取改版后的续期状态字段

    返回 (can_renew, free_remaining, reason):
      - can_renew: True/False/None(旧结构, 无此字段)
      - free_remaining: 剩余免费续期次数 (可能为 None)
      - reason: 不可续期的原因文本
    """
    for c in (attrs, detail):
        if not isinstance(c, dict):
            continue
        if c.get("can_renew") is not None:
            can = bool(c.get("can_renew"))
            free = c.get("free_renewals_remaining")
            reason = None
            if not can:
                if free == 0:
                    reason = "免费续期次数已用完"
                else:
                    reason = "面板标记暂不可续期 (can_renew=false)"
            return can, free, reason
    return None, None, None


def renew_server_api(session, sid):
    """调用续期接口 (路由已确认仍存在)"""
    r = api_post(session, f"/api/client/servers/{sid}/upgrade/renew")
    captcha_required = False
    if r.status_code == 403:
        body = r.text
        try:
            j = r.json()
            code = j.get("code") if isinstance(j, dict) else None
            if code == "captcha_required" or "captcha" in body.lower():
                captcha_required = True
        except Exception:
            if "captcha" in body.lower():
                captcha_required = True
    return r, captcha_required


def renew_error_msg(r):
    """从续期响应中提取后端错误 (可能 2xx 但 body 带错误, 如 renewNotAvailableYet)"""
    try:
        j = r.json()
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    # Pterodactyl/Laravel 错误格式: { errors: [ { code, detail } ] }
    errors = j.get("errors")
    if isinstance(errors, list) and errors:
        e = errors[0]
        return f"{e.get('code', '')}: {e.get('detail', '')}".strip(": ")
    if j.get("code"):
        return f"{j.get('code')}: {j.get('detail', '')}".strip(": ")
    return None


# ==================== 浏览器自动登录 (SeleniumBase, 免手动换 Cookie) ====================
def _dump_login_form(sb):
    """诊断: 打印登录页可见的表单元素"""
    try:
        els = sb.execute_script("""
            (function(){
                return Array.from(document.querySelectorAll('input,button'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => el.tagName + '|name=' + (el.getAttribute('name')||'')
                          + '|type=' + (el.getAttribute('type')||'')
                          + '|ph=' + (el.getAttribute('placeholder')||'')
                          + '|' + ((el.innerText||el.textContent||'').trim().slice(0,25)))
                    .slice(0, 25);
            })()
        """)
        print("🔍 登录表单元素:")
        for x in els or []:
            print("   ", x)
    except Exception as e:
        print(f"⚠️ 枚举登录表单失败: {e}")


def _click_turnstile(sb):
    """切换到 Turnstile 复选框 iframe 直接点击 (无头/无显示器环境可用)

    返回是否找到并点击。uc_gui_click_captcha() 依赖真实屏幕坐标,
    在 GitHub Actions (无显示器) 下会一直返回 False。
    """
    try:
        sb.driver.switch_to.default_content()
        iframes = sb.driver.find_elements("css selector", "iframe")
        for ifr in iframes:
            src = (ifr.get_attribute("src") or "") + " " + (ifr.get_attribute("title") or "")
            if any(k in src.lower() for k in ("challenges.cloudflare.com", "turnstile", "checkbox")):
                sb.driver.switch_to.frame(ifr)
                ok = False
                for js in (
                    "var c=document.querySelector('input[type=checkbox]');if(c){c.click();true}else false",
                    "var e=document.querySelector('label,div[role=checkbox],.rc-anchor');if(e){e.click();true}else false",
                ):
                    try:
                        if sb.driver.execute_script(js):
                            ok = True
                            break
                    except Exception:
                        continue
                sb.driver.switch_to.default_content()
                return ok
    except Exception:
        pass
    return False


def _dump_captcha_widget(sb, max_elems=80):
    """Dump 'Click on X' 挑战 widget 的可见元素结构 (tag/class/text/位置) 到日志, 供排查点击为何未生效"""
    try:
        info = sb.driver.execute_script("""
            var all = document.querySelectorAll('*');
            var cands = [];
            for (var el of all) {
                var txt = (el.textContent || '').trim().toLowerCase();
                if (txt.indexOf('click on') === 0 && txt.length < 40) cands.push(el);
            }
            if (!cands.length) return null;
            cands.sort(function(a,b){ return a.querySelectorAll('*').length - b.querySelectorAll('*').length; });
            var anchor = cands[0];
            var box = anchor;
            for (var i = 0; i < 10 && box; i++) { box = box.parentElement; if (box === document.body) break; }
            if (!box) box = document.body;
            var out = [];
            var nodes = box.querySelectorAll('div, button, a, span, canvas, img, label, p, svg, input');
            for (var n of nodes) {
                var r = n.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) continue;
                out.push({
                    tag: n.tagName,
                    cls: (n.className || '').toString().slice(0, 50),
                    id: n.id,
                    txt: (n.textContent || '').trim().slice(0, 18),
                    w: Math.round(r.width), h: Math.round(r.height),
                    x: Math.round(r.x), y: Math.round(r.y)
                });
            }
            return JSON.stringify(out.slice(0, arguments[0]));
        """, max_elems)
        if info:
            log(f"   🔬 挑战 widget DOM (前 {max_elems} 个可见元素):")
            log("   " + info[:3000])
        else:
            log("   🔬 挑战 widget DOM: 未找到 'Click on' 锚点")
    except Exception as e:
        log(f"   挑战 widget DOM dump 失败: {e}")


def _challenge_card_ocr(sb, target):
    """点选题卡片 OCR 策略 (参照 bo-aclclouds): 卡片是 img/canvas 图片, 文本匹配找不到,
    逐张截图 OCR + difflib 模糊匹配目标词, 点最佳。得分 < 0.4 不盲点, 避免 'Captcha incorrect'。
    返回: True=点中; False=无锚点/无卡片/得分不足/异常。"""
    import difflib
    d = sb.driver
    try:
        from selenium.webdriver.common.by import By
    except Exception:
        return False
    try:
        # 1) 找最内层可见 'Click on' 锚点
        anchor = None
        for el in d.find_elements(By.XPATH, "//*[starts-with(normalize-space(text()),'Click on') or starts-with(normalize-space(text()),'click on')]"):
            try:
                if el.is_displayed():
                    anchor = el
                    break
            except Exception:
                continue
        if anchor is None:
            log("   ⚠️ OCR 卡策略: 无可见 'Click on' 锚点")
            return False
        # 2) 候选卡片: 锚点之后的 img/canvas (前 4) + role=dialog 内的 img/canvas
        xpaths = [
            "//*[contains(text(),'Click on') or contains(text(),'click on')]/following::canvas[position()<=4]",
            "//*[contains(text(),'Click on') or contains(text(),'click on')]/following::img[position()<=4]",
            "//div[contains(@role,'dialog')]//img",
            "//div[contains(@role,'dialog')]//canvas",
        ]
        cards, seen = [], set()
        for xp in xpaths:
            try:
                for c in d.find_elements(By.XPATH, xp):
                    try:
                        if not c.is_displayed():
                            continue
                        cid = c.id
                    except Exception:
                        continue
                    if cid in seen:
                        continue
                    seen.add(cid)
                    cards.append(c)
                    if len(cards) >= 4:
                        break
            except Exception:
                continue
            if len(cards) >= 4:
                break
        if not cards:
            log("   ⚠️ OCR 卡策略: 锚点附近无可见 img/canvas 卡片")
            return False
        # 3) 逐张 OCR + 模糊打分
        best, best_score, best_text = None, 0.0, ""
        for i, card in enumerate(cards):
            ocr_text = _ocr_card_text(card.screenshot_as_png).lower()
            score = difflib.SequenceMatcher(None, target, ocr_text).ratio()
            if target in ocr_text or (len(ocr_text) >= 4 and ocr_text in target):
                score = max(score, 0.85)
            log(f"   🔍 OCR 卡片 #{i+1}: [{ocr_text}] 相似度 {score:.2f}")
            if score > best_score:
                best, best_score, best_text = card, score, ocr_text
        if best is not None and best_score >= 0.4:
            log(f"   ✨ 最佳卡片 [{best_text}] (得分 {best_score:.2f}), 点击中")
            d.execute_script("arguments[0].scrollIntoView({block:'center'});", best)
            time.sleep(0.3)
            try:
                best.click()
            except Exception:
                d.execute_script("arguments[0].click();", best)
            return True
        log(f"   ⚠️ OCR 卡策略: 最佳得分 {best_score:.2f} < 0.4, 不盲点")
        return False
    except Exception as e:
        log(f"   ⚠️ OCR 卡策略异常: {e}")
        return False


def _challenge_card_text(sb, target):
    """文本卡回退策略: 挑战盒 (锚点祖先容器) 内直接点同名文字卡片。
    返回: True=点中; 'no-anchor'/'no-box'/'no-card'/False=原因。"""
    try:
        clicked = sb.driver.execute_script("""
            var t = arguments[0].toLowerCase();
            var known = ['vps', 'minecraft', 'discord', 'cloud'];
            // 1) 找最内层 "Click on" 锚点元素
            var all = document.querySelectorAll('*');
            var cands = [];
            for (var el of all) {
                var txt = (el.textContent || '').trim().toLowerCase();
                if (txt.indexOf('click on') === 0 && txt.length < 40) cands.push(el);
            }
            if (!cands.length) return 'no-anchor';
            cands.sort(function(a,b){ return a.querySelectorAll('*').length - b.querySelectorAll('*').length; });
            var anchor = cands[0];
            // 2) 找挑战盒: 最浅一层包含 >=3 个已知卡片的祖先 (排除登录表里的 OAuth 按钮干扰)
            function cardCount(box) {
                var n = 0;
                var nodes = box.querySelectorAll('div, button, a, span, p, label');
                for (var c of nodes) {
                    if (known.indexOf((c.textContent || '').trim().toLowerCase()) !== -1) n++;
                }
                return n;
            }
            var box = null, p = anchor.parentElement;
            for (var i = 0; i < 10 && p; i++) {
                if (cardCount(p) >= 3) { box = p; break; }
                p = p.parentElement;
            }
            if (!box) return 'no-box';
            // 3) 挑战盒内: 点目标词 (选最小可见元素, 事件序列保证冒泡)
            var matches = [];
            var nodes = box.querySelectorAll('div, button, a, span, p, label');
            for (var c of nodes) {
                if ((c.textContent || '').trim().toLowerCase() === t) {
                    var r0 = c.getBoundingClientRect();
                    if (r0.width > 5 && r0.height > 5) matches.push({el: c, area: r0.width * r0.height});
                }
            }
            if (!matches.length) return 'no-card';
            matches.sort(function(a,b){ return a.area - b.area; });
            var el = matches[0].el;
            var r = el.getBoundingClientRect();
            var x = r.left + r.width / 2, y = r.top + r.height / 2;
            var opts = {bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, button: 0};
            try { el.dispatchEvent(new PointerEvent('pointerdown', opts)); } catch(e){}
            try { el.dispatchEvent(new MouseEvent('mousedown', opts)); } catch(e){}
            try { el.dispatchEvent(new PointerEvent('pointerup', opts)); } catch(e){}
            try { el.dispatchEvent(new MouseEvent('mouseup', opts)); } catch(e){}
            el.click();
            return true;
        """, target)
    except Exception:
        clicked = False
    if clicked is True:
        log(f"   ✅ 已点击 '{target}' 卡片 (文本卡)")
        return True
    log(f"   ⚠️ 文本卡未点中 (原因: {clicked})")
    return False


def _challenge_card(sb):
    """面板自定义挑战 'Click on X': 解析目标词, 先 OCR 卡片 (图片卡), 再回退文本卡。
    返回: True=点中卡片; False=页面无该挑战或没点中。"""
    try:
        sb.driver.switch_to.default_content()
        pg = sb.get_page_source()
    except Exception:
        return False
    m = re.search(r'[Cc]lick on\s+([A-Za-z0-9_-]+)', pg)
    if not m:
        return False
    target = m.group(1).strip().lower()
    log(f"   🔣 自定义挑战: 需点击 '{target}' 卡片")
    if not _challenge_card_ocr(sb, target):
        _challenge_card_text(sb, target)
    _dump_captcha_widget(sb)
    return True


def _discord_oauth_login(sb):
    """Discord OAuth 登录回退 (账号需绑定 Discord; Turnstile 不可用时自动使用)

    流程: 点登录页 Discord 按钮 → 抓 authorize URL (client_id/state/redirect_uri)
    → 用 Discord token 完成授权拿 code → 浏览器打开回调 → 提取 cookie。
    """
    if not DISCORD_TOKEN:
        log("ℹ️ 未配置 DISCORD_TOKEN, 跳过 Discord OAuth 回退")
        return None
    log("🔑 尝试 Discord OAuth 登录 (账号需绑定 Discord)...")
    try:
        # 1) 点击登录页的 Discord 按钮
        clicked = False
        for sel in ('a:contains("Discord")', 'button:contains("Discord")',
                    'a[href*="discord.com/oauth2"]', '[data-provider="discord"]'):
            try:
                if sb.is_element_visible(sel):
                    sb.click(sel)
                    clicked = True
                    log(f"✅ 已点击 Discord 登录按钮: {sel}")
                    break
            except Exception:
                continue
        if not clicked:
            log("❌ 未找到 Discord 登录按钮")
            return None

        # 2) 等待跳转到 discord.com 并抓取 authorize URL
        auth_url = None
        for _ in range(20):
            url = sb.get_current_url()
            if "discord.com/oauth2/authorize" in url or "discord.com/api/oauth2" in url:
                auth_url = url
                break
            sb.sleep(1)
        if not auth_url:
            log(f"❌ 未跳转到 Discord, 当前 URL: {sb.get_current_url()[:120]}")
            try:
                sb.save_screenshot("acl_discord_no_redirect.png")
            except Exception:
                pass
            return None
        log(f"📝 Discord authorize URL: {auth_url[:160]}")

        params = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        client_id = (params.get("client_id") or [""])[0]
        state = (params.get("state") or [""])[0]
        redirect_uri = (params.get("redirect_uri") or [""])[0]
        scope = (params.get("scope") or [""])[0]
        if not (client_id and state and redirect_uri):
            log("❌ authorize URL 缺少参数 (client_id/state/redirect_uri)")
            return None
        log(f"   client_id={client_id} | redirect={redirect_uri[:60]} | scope={scope or '(默认)'}")

        # 3) 用 Discord token 请求授权, 拿带 code 的回调 URL
        #    GET 若返回 200 (同意授权页) 则改 POST 自动授权 (authorize:true)
        disc_url = ("https://discord.com/api/v9/oauth2/authorize?"
                    + urllib.parse.urlencode({
                        "client_id": client_id,
                        "response_type": "code",
                        "redirect_uri": redirect_uri,
                        "scope": scope or "identify email guilds",
                        "state": state,
                    }))
        dheaders = {
            "Authorization": DISCORD_TOKEN,
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"),
        }

        def _disc_authorize(method="GET"):
            if method == "POST":
                payload = {
                    "permissions": "0",
                    "authorize": True,
                    "integration_type": 0,
                    "client_id": client_id,
                    "response_type": "code",
                    "redirect_uri": redirect_uri,
                    "scope": scope or "identify email guilds",
                    "state": state,
                }
                return requests.post(disc_url, headers={**dheaders, "Content-Type": "application/json"},
                                     json=payload, allow_redirects=False, timeout=25)
            return requests.get(disc_url, headers=dheaders, allow_redirects=False, timeout=25)

        try:
            resp = _disc_authorize("GET")
            if resp.status_code not in (301, 302):
                log(f"ℹ️ GET 授权返回 HTTP {resp.status_code} (可能需要同意授权), 尝试 POST 自动授权...")
                resp = _disc_authorize("POST")
        except Exception as e:
            log(f"❌ Discord 授权请求异常: {e}")
            return None

        callback = ""
        if resp.status_code in (301, 302):
            callback = resp.headers.get("Location", "")
        else:
            # 个别情况下 200 响应体里带 location 字段
            try:
                j = resp.json()
                callback = j.get("location") or ""
            except Exception:
                callback = ""
        if not callback:
            log(f"❌ Discord 授权失败: HTTP {resp.status_code} - {resp.text[:200]}")
            return None
        log(f"✅ 拿到回调 URL: {re.sub(r'code=[^&]+', 'code=***', callback)[:120]}")

        # 4) 浏览器打开回调完成登录
        sb.uc_open_with_reconnect(callback, reconnect_time=4)
        sb.sleep(3)
        logged_in = False
        for _ in range(40):
            url = sb.get_current_url()
            if "auth/login" not in url and "login" not in url.lower():
                logged_in = True
                break
            sb.sleep(1)
        if not logged_in:
            log(f"❌ OAuth 回调后未跳转, 当前: {sb.get_current_url()[:120]}")
            try:
                print("   📝 页面内容:", sb.get_text("body")[:200])
            except Exception:
                pass
            return None
        log(f"✅ Discord OAuth 登录成功: {sb.get_current_url()[:80]}")

        # 5) 提取 cookie
        cookies = sb.get_cookies()
        xsrf = session = None
        for c in cookies:
            if c.get("name") == "XSRF-TOKEN":
                xsrf = c.get("value")
            if c.get("name") == "__Host-aclclouds_session":
                session = c.get("value")
        if not (xsrf and session):
            log("❌ 登录成功但未拿到 cookie")
            print("   cookie 名:", [c.get("name") for c in cookies])
            return None
        cookie = f"XSRF-TOKEN={xsrf}; __Host-aclclouds_session={session}"
        log(f"✅ Discord OAuth 登录完成, 已获取新 Cookie (session 前 8 位: {session[:8]}...)")
        return cookie
    except Exception as e:
        log(f"💥 Discord OAuth 异常: {e}")
        return None



# ==================== 登录页交互 (抽自 browser_login, 供续期 fallback 复用) ====================
# 在已打开的 sb 上执行: 打开 /auth/login → 填账密 → 过 Turnstile → 提交 → 等跳转
# 成功返回 True (sb 处于已登录状态), 失败返回 False
def _browser_do_login(sb):
    sb.open("https://aclclouds.com/auth/login")
    sb.wait_for_ready_state_complete()
    sb.sleep(3)
    log(f"📝 当前 URL: {sb.get_current_url()}")
    _dump_login_form(sb)

    # 填邮箱
    email_filled = False
    for sel in ('input[name="email"]', 'input[type="email"]',
                'input[autocomplete="email"]', 'input[placeholder*="example.com"]'):
        try:
            if sb.is_element_visible(sel):
                sb.type(sel, ACL_EMAIL)
                email_filled = True
                log(f"✅ 已填写邮箱: {sel}")
                break
        except Exception:
            continue

    # 填密码
    pw_filled = False
    for sel in ('input[name="password"]', 'input[type="password"]',
                'input[autocomplete="current-password"]'):
        try:
            if sb.is_element_visible(sel):
                sb.type(sel, ACL_PASSWORD)
                pw_filled = True
                log(f"✅ 已填写密码: {sel}")
                break
        except Exception:
            continue

    if not (email_filled and pw_filled):
        log("❌ 未找到邮箱/密码输入框 (表单结构可能已变)")
        _dump_login_form(sb)
        try:
            sb.save_screenshot("acl_login_form.png")
        except Exception:
            pass
        return False

    # Turnstile: 先等组件 iframe 出现, 再点击直到 token 真正生成
    log("🔒 尝试通过 Turnstile 验证...")

    def _page_iframes():
        """原生 driver 枚举 iframe (sb.execute_script 在登录页返回值不可靠)"""
        try:
            sb.driver.switch_to.default_content()
            frs = sb.driver.find_elements("css selector", "iframe")
            return [(f.get_attribute("src") or "") + " " + (f.get_attribute("title") or "")
                    for f in frs]
        except Exception:
            return []

    def _captcha_passed():
        try:
            sb.driver.switch_to.default_content()
            tok = sb.driver.execute_script(
                "var el=document.querySelector('textarea[name=\"cf-turnstile-response\"],"
                "input[name=\"cf-turnstile-response\"]');"
                "return !!(el&&el.value&&el.value.length>10);")
            if tok:
                return True
        except Exception:
            pass
        try:
            pg = sb.get_page_source().lower()
            return not any(k in pg for k in
                           ("i am not a robot", "captcha incorrect", "secured by aclclouds"))
        except Exception:
            return True

    # 1) 等 Turnstile iframe 出现 (最多 30s)
    def _submit_and_wait():
        """提交登录表单并等待跳转, 返回是否离开登录页 (验证码失败时自动重试一轮)"""
        for cycle in range(1, 3):
            submit_ok = False
            for sel in ('button[type="submit"]',
                        'button:contains("Sign in")', 'button:contains("Login")',
                        'button:contains("Se connecter")', 'button:contains("Connexion")'):
                try:
                    if sb.is_element_visible(sel):
                        sb.click(sel)
                        submit_ok = True
                        log(f"✅ 已点击登录按钮: {sel}")
                        break
                except Exception:
                    continue
            if not submit_ok:
                log("⚠️ 未找到登录提交按钮, 尝试回车提交")
                try:
                    sb.enter()
                except Exception:
                    pass
            # 等待跳转离开登录页 (被弹去外部站点 = OAuth 未完成, 视为失败)
            for _ in range(40):
                url = sb.get_current_url()
                host = urllib.parse.urlparse(url).netloc.lower()
                if host and not host.endswith("aclclouds.com"):
                    log(f"⚠️ 浏览器被重定向到外部站点 {host} (误点 OAuth 按钮?), 视为登录失败")
                    return False
                url_clear = "auth/login" not in url and "login" not in url.lower()
                # 佐证: 密码输入框还在 = 仍在登录页 (防 URL 不变但 body 已切换的 SPA 情形)
                pwd_present = False
                try:
                    pwd_present = bool(sb.driver.find_elements("css selector", 'input[name="password"]'))
                except Exception:
                    pwd_present = None  # 探测失败不阻塞
                if url_clear and not pwd_present:
                    log(f"✅ 登录成功, 当前页面: {url}")
                    return True
                sb.sleep(1)
            # 检查是否验证码错误, 是则重新点验证框再提交一轮
            try:
                body = sb.get_text("body") or ""
            except Exception:
                body = ""
            if "captcha" in body.lower():
                log(f"⏳ 第 {cycle} 次提交后提示验证码错误, 重新解挑战卡并提交...")
                try:
                    sb.uc_gui_click_captcha()
                except Exception:
                    pass
                _challenge_card(sb)
                sb.sleep(4)
                continue
            break
        log("❌ 登录后未跳转 (可能 2FA / 凭证错 / 验证码未提交)")
        try:
            err = sb.driver.execute_script(
                "var els=document.querySelectorAll('.alert, [role=alert], .text-danger, .error');"
                "return Array.from(els).map(e=>e.textContent.trim()).filter(t=>t).join(' | ').slice(0,300);")
            if err:
                log("   🚨 页面错误提示: " + err)
        except Exception:
            pass
        try:
            log("   🔗 当前 URL: " + sb.get_current_url())
        except Exception:
            pass
        try:
            print("   📝 页面内容:", sb.get_text("body")[:300])
        except Exception:
            pass
        try:
            sb.save_screenshot("acl_login_failed.png")
        except Exception:
            pass
        return False

    # 2) 解人机验证: 面板自定义挑战
    #    "I am not a robot" 复选框 → "Click on X" 刮刮卡 (目标词每轮随机: VPS/Minecraft/Discord/Cloud)
    def _challenge_pending():
        try:
            pg = sb.get_page_source().lower()
        except Exception:
            return False
        if re.search(r"click on\s+[a-z]+", pg):
            return True
        return "i am not a robot" in pg

    dumped = False
    solved = False
    for attempt in range(1, 6):
        if not _challenge_pending():
            solved = True
            log("✅ 页面无待解人机验证")
            break
        if not dumped:
            _dump_captcha_widget(sb)
            dumped = True
        # 2a) 先点复选框 (激活挑战可能需要这一步)
        try:
            sb.driver.switch_to.default_content()
            for el in sb.driver.find_elements("css selector", "div, span, label"):
                try:
                    t = (el.text or "").strip().lower()
                    if t == "i am not a robot" and el.is_displayed():
                        el.click()
                        break
                except Exception:
                    continue
        except Exception:
            pass
        sb.sleep(2)
        # 2b) 点 'Click on X' 挑战卡
        _challenge_card(sb)
        log(f"   第 {attempt} 轮: 已处理挑战, 等待刷新...")
        sb.sleep(4)
    if not solved:
        log("⚠️ 5 轮后自定义挑战未解, 仍尝试提交 (面板可能未强制)")
    # 3) 提交登录 (captcha 错误时 _submit_and_wait 会自动重点卡片再试)
    return _submit_and_wait()


def browser_login():
    """用浏览器登录 aclclouds.com, 返回 Cookie 字符串; 失败返回 None

    流程: 打开 /auth/login → 填账密 → 过 Turnstile → 提交 → 等跳转 → 取 cookie
    需要: pip install seleniumbase; 过 Turnstile 需要干净代理 (IS_PROXY=true)。
    """
    if not ACL_EMAIL or not ACL_PASSWORD:
        log("ℹ️ 未配置 ACL_EMAIL/ACL_PASSWORD, 跳过浏览器自动登录")
        return None
    try:
        from seleniumbase import SB
    except ImportError:
        log("❌ 未安装 seleniumbase, 无法浏览器登录 (pip install seleniumbase)")
        return None

    kwargs = {"uc": True, "headless": HEADLESS}
    if IS_PROXY:
        log(f"🔗 浏览器走代理: {PROXY_SERVER}")
        kwargs["proxy"] = PROXY_SERVER
    else:
        log("🌐 浏览器直连 (Turnstile 过不去时请设 IS_PROXY=true + 干净代理)")

    log("🚀 启动浏览器登录 aclclouds.com ...")
    try:
        with SB(**kwargs) as sb:
            if not _browser_do_login(sb):
                return None

            # 提取 cookie
            cookies = sb.get_cookies()
            xsrf = session = None
            for c in cookies:
                if c.get("name") == "XSRF-TOKEN":
                    xsrf = c.get("value")
                if c.get("name") == "__Host-aclclouds_session":
                    session = c.get("value")
            if not (xsrf and session):
                log("❌ 登录成功但未拿到 XSRF-TOKEN/__Host-aclclouds_session")
                print("   cookie 名:", [c.get("name") for c in cookies])
                return None
            cookie = f"XSRF-TOKEN={xsrf}; __Host-aclclouds_session={session}"
            log(f"✅ 浏览器登录完成, 已获取新 Cookie (session 前 8 位: {session[:8]}...)")
            return cookie
    except Exception as e:
        log(f"💥 浏览器登录异常: {e}")
        return None


def update_acl_secret(cookie_str):
    """用 gh CLI 把新 Cookie 写回 GitHub Secret ACL_COOKIES (Actions 里 gh 已认证)"""
    if not GH_TOKEN:
        log("ℹ️ 未配置 GH_TOKEN, 不更新 GitHub Secret (本地下次仍需浏览器登录)")
        return False
    import subprocess
    masked = (cookie_str[:20] + "..." + cookie_str[-10:]) if len(cookie_str) > 30 else "***"
    log(f"🔄 更新 Secret ACL_COOKIES (新值: {masked})")
    try:
        env = os.environ.copy()
        env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(
            ["gh", "secret", "set", "ACL_COOKIES", "--repo", GH_REPO, "--body", cookie_str],
            capture_output=True, text=True, timeout=60, check=False, env=env,
        )
        if proc.returncode == 0:
            log("✅ ACL_COOKIES 更新成功")
            return True
        log(f"❌ 更新失败: {proc.stderr.strip()[:200]}")
        return False
    except Exception as e:
        log(f"❌ 更新 Secret 异常: {e}")
        return False


# ==================== 浏览器续期 fallback (API 被 Turnstile 拦截时使用) ====================
# 纯 API 的 renew POST 在数据中心 IP 上会被 Cloudflare 风控要求 Turnstile 人机验证 (403 + captcha_required)。
# 此时切真实浏览器 (undetected-chrome + xvfb, 同 katabump/icehost 过 CF 盾套路):
# 注入账号 Cookie → 开面板 → 找服务器页 → 点续期按钮 → 过 Turnstile → API 验证 expires_at 是否后移。

_DROP_COOKIE_NAMES = {"cf_clearance", "__cf_bm", "cf_obfuscate"}  # IP/UA 绑定, 旧值反而触发风控


def _cookie_list(cookie_str):
    """把 Cookie 字符串解析为 selenium add_cookie 字典列表 (丢掉 IP 绑定型)"""
    out = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in _DROP_COOKIE_NAMES:
            continue
        d = {"name": k, "value": v, "domain": "aclclouds.com", "path": "/"}
        if k.startswith("__Host-") or k.startswith("cf-"):
            d["secure"] = True
        out.append(d)
    return out


def _pass_cf_challenge(sb, timeout=90):
    """等待 Cloudflare 挑战/拦截页消除 (出现 Turnstile 复选框则点击)"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = sb.get_current_url().lower()
        except Exception:
            url = ""
        src = ""
        try:
            src = sb.get_page_source().lower()
        except Exception:
            pass
        if "cdn-cgi/challenge" not in url and not any(k in src for k in ("just a moment", "cf-chl", "challenge-platform")):
            return True
        if _click_turnstile(sb):
            log("   点击挑战页 Turnstile 复选框, 等待自动通过...")
        sb.sleep(3)
    return False


def renew_via_browser(srv, cookie_str, session, old_remaining):
    """浏览器 fallback 续期。成功返回新剩余秒数, 失败返回 None (日志带细节)。

    步骤: 注入 Cookie → 首页过 CF 挑战 → 打开 /servers/{短id} (Pterodactyl 路由, 失败则列表按名找)
          → 点续期按钮 (支持中/英/法 UI) → 确认框 → Turnstile → API 验证 expires_at 后移。
    """
    try:
        from seleniumbase import SB
    except ImportError:
        log("❌ 浏览器 fallback 失败: 未安装 seleniumbase")
        return None

    kwargs = {"uc": True, "headless": HEADLESS}
    if IS_PROXY:
        kwargs["proxy"] = PROXY_SERVER
        log(f"🔗 浏览器 fallback 走代理: {PROXY_SERVER}")

    use_creds = bool(ACL_EMAIL and ACL_PASSWORD)
    log(f"🌐 浏览器 fallback: 启动 undetected-chrome ({'账密登录' if use_creds else 'cookie 注入'})")

    renew_kw = ("renew", "extension", "extend", "add time",
                "renouveler", "renouvel", "prolonger", "prolongation",
                "续期", "续费", "延期")
    skip_kw = ("auto renew", "autorenew", "auto-renew", "auto-renouvel", "renewal status")
    confirm_kw = ("confirm", "ok", "yes", "确认", "确定", "oui",
                  "confirmer", "valider", "validate", "continuer", "continue", "submit",
                  "prolonger", "extend", "renouveler")

    try:
        with SB(**kwargs) as sb:
            driver = sb.driver
            # 1) 登录: 账密优先 (浏览器持有自己的 cf_clearance + 真 session); 无账密才 cookie 注入
            logged_in = False
            if use_creds:
                logged_in = _browser_do_login(sb)
                if logged_in:
                    log("✅ 浏览器账密登录成功, 进入服务器页")
            if not logged_in:
                cks = _cookie_list(cookie_str)
                log(f"   cookie 注入 fallback: 注入 {len(cks)} 个 cookie")
                sb.open(BASE_URL + "/")
                sb.wait_for_ready_state_complete()
                sb.sleep(2)
                _pass_cf_challenge(sb)
                try:
                    driver.delete_all_cookies()
                except Exception:
                    pass
                for c in cks:
                    try:
                        driver.add_cookie(c)
                    except Exception as e:
                        log(f"   ⚠️ 注入 cookie {c['name']} 失败: {str(e)[:100]}")
                sb.open(BASE_URL + "/")
                sb.wait_for_ready_state_complete()
                sb.sleep(2)
            # 3) 找服务器页: 先由 dashboard 按 server 名/href 找 link (发现式, 适配自定义面板),
            #    找不到再试多个候选直连路径
            page_ok = False
            srv_name_l = srv['name'].lower()
            for base in (BASE_URL + "/", BASE_URL + "/dashboard"):
                if page_ok:
                    break
                try:
                    sb.open(base)
                    sb.wait_for_ready_state_complete()
                    sb.sleep(3)
                    # 调试: dump 全页可见连结 (text + href), 方便定位真实服务器页路径
                    try:
                        links_dbg = []
                        for a in driver.find_elements("css selector", "a"):
                            try:
                                if a.is_displayed():
                                    links_dbg.append(f"{(a.text or '').strip()[:30]} -> {(a.get_attribute('href') or '')[:80]}")
                            except Exception:
                                continue
                        log("   [debug] 页面连结:\n     " + "\n     ".join(links_dbg[:40]))
                    except Exception:
                        pass
                    for a in driver.find_elements("css selector", "a"):
                        try:
                            if not a.is_displayed():
                                continue
                            txt_l = (a.text or '').lower()
                            href_l = (a.get_attribute('href') or '').lower()
                            # 按名称或 href 含 server id / 名称匹配
                            if srv_name_l in txt_l or srv_name_l in href_l or srv['id'] in href_l or f"/servers/{srv['id']}" in href_l:
                                a.click()
                                sb.sleep(3)
                                page_ok = True
                                break
                        except Exception:
                            continue
                except Exception as e:
                    log(f"   {base} 查找异常: {e}")
            if not page_ok:
                # 多候选直连路径 (适配不同面板路由)
                for path in (f"/servers/{srv['id']}", f"/dashboard/servers/{srv['id']}",
                             f"/server/{srv['id']}", f"/jeux/{srv['id']}", f"/servers"):
                    if page_ok:
                        break
                    try:
                        sb.open(BASE_URL + path)
                        sb.wait_for_ready_state_complete()
                        sb.sleep(3)
                        body_txt = sb.get_text("body").lower()
                        cur = sb.get_current_url()
                        if srv_name_l in body_txt or srv['id'] in body_txt or f"{srv['id']}" in cur:
                            log(f"   ✅ 直连命中: {path}")
                            page_ok = True
                        else:
                            log(f"   直连未命中: {path} (当前 {cur[:70]})")
                    except Exception as e:
                        log(f"   直连 {path} 异常: {e}")
            if not page_ok:
                log(f"❌ 浏览器 fallback 失败: 找不到服务器 {srv['name']} 的页面")
                try:
                    sb.save_screenshot("acl_browser_notfound.png")
                except Exception:
                    pass
                return None
            # 4) 找续期按钮并点击 (中/英/法 UI; 排除 auto-renew 开关; 主页面找不到则翻 sub-tab)
            def _find_and_click_renew():
                for el in driver.find_elements("css selector", "button, a, [role='button']"):
                    try:
                        if not el.is_displayed():
                            continue
                        txt = (el.text or "").strip()
                        if not txt:
                            continue
                        t = txt.lower()
                        if any(k in t for k in renew_kw) and not any(k in t for k in skip_kw):
                            log(f"   找到续期按钮: '{txt[:40]}'")
                            try:
                                el.click()
                            except Exception:
                                # SPA re-render 令 element stale → JS click 兜底 (繞過攔截/失效)
                                log("   ⚠️ 原生 click 爆 (可能 stale), 改 JS click")
                                driver.execute_script("arguments[0].click();", el)
                            return True
                    except Exception:
                        continue
                return False

            clicked = _find_and_click_renew()
            if not clicked:
                # 调试: dump 服务器页全部连结+按钮 (text+href), 定位 renew 真实位置
                try:
                    dbg = []
                    for el in driver.find_elements("css selector", "a, button, [role='button']"):
                        try:
                            if el.is_displayed():
                                dbg.append(f"{(el.text or '').strip()[:30]} -> {(el.get_attribute('href') or '')[:70]}")
                        except Exception:
                            continue
                    log("   [debug] 服务器页元素:\n     " + "\n     ".join(dbg[:50]))
                except Exception:
                    pass
                # 服务器页概要页: 试点 'Ouvrir mon panel' (开 panel) 再找 renew
                try:
                    for b in driver.find_elements("css selector", "button, a, [role='button']"):
                        try:
                            t = (b.text or "").strip().lower()
                            if b.is_displayed() and ("ouvrir mon panel" in t or "open my panel" in t or "mon panel" in t):
                                log(f"   撳 '{(b.text or '').strip()[:40]}' 入 panel...")
                                old_handles = set(driver.window_handles)
                                b.click()
                                sb.sleep(4)
                                # 可能开新 tab, 切过去
                                new_h = [h for h in driver.window_handles if h not in old_handles]
                                if new_h:
                                    driver.switch_to.window(new_h[0])
                                    log(f"   切入新 tab: {sb.get_current_url()[:80]}")
                                sb.sleep(3)
                                # panel 界面再 dump 一次
                                try:
                                    dbg2 = []
                                    for el in driver.find_elements("css selector", "a, button, [role='button']"):
                                        try:
                                            if el.is_displayed():
                                                dbg2.append(f"{(el.text or '').strip()[:30]} -> {(el.get_attribute('href') or '')[:70]}")
                                        except Exception:
                                            continue
                                    log("   [debug] panel 页元素:\n     " + "\n     ".join(dbg2[:50]))
                                except Exception:
                                    pass
                                clicked = _find_and_click_renew()
                                if clicked:
                                    break
                        except Exception:
                            continue
                except Exception as e:
                    log(f"   panel 撳钮异常: {e}")
            if not clicked:
                tab_kw = ("billing", "renew", "facturation", "extension",
                          "abonnement", "paiement", "payment", "续期", "计费", "账单")
                for a in driver.find_elements("css selector", "a, [role='tab'], .tab, .nav-link"):
                    try:
                        if not a.is_displayed():
                            continue
                        t = (a.text or "").strip().lower()
                        if any(k in t for k in tab_kw):
                            log(f"   翻 sub-tab 找续期钮: '{t[:30]}'")
                            a.click()
                            sb.sleep(3)
                            clicked = _find_and_click_renew()
                            if clicked:
                                break
                    except Exception:
                        continue
            if not clicked:
                log("❌ 浏览器 fallback 失败: 服务器页未找到续期按钮 (面板 UI 可能变了)")
                try:
                    sb.save_screenshot("acl_browser_norenewbtn.png")
                    names = []
                    for b in driver.find_elements("css selector", "button, a"):
                        try:
                            if b.is_displayed() and (b.text or "").strip():
                                names.append(b.text.strip()[:30])
                        except Exception:
                            continue
                    log(f"   页面可见按钮: {names[:30]}")
                except Exception:
                    pass
                return None
            sb.sleep(2)
            # 5) 确认对话框 (Renew 後可能彈確認/選時長 modal; 兩輪確認, JS click 兜底)
            for round_i in range(1, 3):
                confirmed = False
                try:
                    for el in driver.find_elements("css selector", "button, [role='button'], .btn"):
                        try:
                            if not el.is_displayed():
                                continue
                            t = (el.text or "").strip().lower()
                            if any(k in t for k in confirm_kw):
                                log(f"   点击确认 {round_i}: '{t[:30]}'")
                                try:
                                    el.click()
                                except Exception:
                                    log("   ⚠️ 确认原生 click 爆, 改 JS click")
                                    driver.execute_script("arguments[0].click();", el)
                                confirmed = True
                                sb.sleep(3)
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
                if not confirmed:
                    break
            # Renew 後狀態 dump + 截圖 (定位卡位)
            try:
                dbg3 = []
                for el in driver.find_elements("css selector", "a, button, [role='button'], div, span"):
                    try:
                        if el.is_displayed():
                            t3 = (el.text or '').strip()
                            if t3 and t3 not in dbg3:
                                dbg3.append(t3[:28])
                    except Exception:
                        continue
                log("   [debug] Renew 後可見元素: " + " | ".join(dbg3[:40]))
            except Exception as e:
                log(f"   [debug] dump 異常: {e}")
            try:
                sb.save_screenshot("acl_after_renew.png")
            except Exception as e:
                log(f"   [debug] 截圖異常: {e}")
            sb.sleep(2)
            # 5b) Renew 後等 SPA re-render (撳完即刻 dump 會見白屏 loading)
            sb.sleep(8)
            try:
                cur_u = sb.get_current_url()
                log(f"   [Renew 後] 當前 URL: {cur_u[:90]}")
            except Exception:
                pass
            # 深挖: iframe / modal / dialog / shadow DOM
            try:
                frames = driver.find_elements("css selector", "iframe")
                log(f"   [iframes] {len(frames)} 個: {[ (f.get_attribute('src') or '')[:80] for f in frames ]}")
            except Exception as e:
                log(f"   [iframes] 異常: {e}")
            try:
                mods = driver.find_elements("css selector", "[class*=modal], [class*=dialog], [class*=overlay], [class*=popup], dialog, [role=dialog]")
                log(f"   [modal/dialog] {len(mods)} 個")
                for mi, m in enumerate(mods[:5]):
                    try:
                        if m.is_displayed():
                            mt = (m.text or '').strip()[:150].replace("\n", " | ")
                            log(f"   [modal {mi}] text: {mt}")
                    except Exception:
                        continue
            except Exception as e:
                log(f"   [modal] 異常: {e}")
            # 挑戰循環: widget 可能延遲 render/藏 iframe, 每輪先 sleep 再偵測
            try:
                for i in range(1, 4):
                    sb.sleep(5)
                    try:
                        pg = sb.get_page_source().lower()
                    except Exception:
                        continue
                    has_clickon = bool(re.search(r"click on\s+[a-z]+", pg))
                    has_widget = ("confirmez" in pg) or ("challenge-card" in pg) or ("vérifiez" in pg)
                    log(f"   [挑戰偵測 {i}] click_on={has_clickon} widget={has_widget}")
                    if has_clickon or has_widget:
                        _challenge_card(sb)
                        ok_ts = _click_turnstile(sb)
                        if ok_ts:
                            log(f"   第 {i} 次處理挑戰/Turnstile 完成")
                    else:
                        log(f"   ✅ 第 {i} 輪冇挑戰殘留")
                        break
            except Exception as e:
                log(f"   [挑戰偵測] 異常: {e}")
            # 5c) 攞 Turnstile token — 後端 renew 無論 XHR/API 都要求 cf-turnstile-response
            token = None
            sitekey = None
            try:
                html = driver.page_source
                for m in re.finditer(r'data-sitekey="([^"]+)"', html):
                    sitekey = m.group(1)
                    break
                if not sitekey:
                    m = re.search(r'sitekey["\']?\s*[:=]\s*["\']([^"\']{10,})["\']', html)
                    if m:
                        sitekey = m.group(1)
                if sitekey:
                    log(f"   [turnstile] sitekey: {sitekey}")
                elif 'turnstile' in html.lower():
                    log("   [turnstile] 頁面有 turnstile 引用但未找到 sitekey")
                    # turnstile.render 調用段搵 sitekey
                    m = re.search(r'turnstile\.render\([^)]*["\']([^"\']{10,})["\']', html)
                    if m:
                        sitekey = m.group(1)
                        log(f"   [turnstile] 由 render 調用段找到 sitekey: {sitekey}")
                else:
                    log("   [turnstile] 頁面冇 turnstile 引用 (後端或用自製挑戰, token 由挑戰流程發出)")
            except Exception as e:
                log(f"   [turnstile] sitekey 探測異常: {e}")
            if sitekey:
                try:
                    token = driver.execute_async_script(
                        """
                        var sitekey = arguments[0];
                        var cb = arguments[arguments.length - 1];
                        var el = document.createElement('div');
                        el.id = 'renew-turnstile';
                        document.body.appendChild(el);
                        try {
                          turnstile.render(el, {sitekey: sitekey, callback: function(t){ cb(t); }});
                        } catch(e) { cb('ERR ' + e); }
                        """,
                        sitekey,
                    )
                    if token and not token.startswith('ERR'):
                        log(f"   [turnstile] token: {token[:60]}…")
                    else:
                        log(f"   [turnstile] token: {token}")
                        token = None
                except Exception as e:
                    log(f"   [turnstile] token 攞取異常: {e}")
            # 5c) 页面 context XHR 直接發 renew (有 token 帶 token,無 token 照發記錄後端回應)
            try:
                res = driver.execute_async_script(
                    """
                    var cb = arguments[arguments.length - 1];
                    var token = arguments[0];
                    var m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
                    var tok = m ? decodeURIComponent(m[1]) : '';
                    var hdrs = {'Accept': 'application/json',
                                'X-Requested-With': 'XMLHttpRequest',
                                'X-XSRF-TOKEN': tok};
                    if (token) { hdrs['cf-turnstile-response'] = token; }
                    fetch('/api/client/servers/%s/upgrade/renew', {
                      method: 'POST',
                      credentials: 'include',
                      headers: hdrs
                    }).then(function(r){
                        return r.text().then(function(t){
                            cb('HTTP ' + r.status + ' | ' + t.slice(0, 400));
                        });
                    }).catch(function(e){ cb('ERR ' + e); });
                    """ % srv['id'],
                    token,
                )
                log(f"   [XHR renew] {res}")
            except Exception as e:
                log(f"   [XHR renew] 異常: {e}")
            sb.sleep(3)
            # 6) 续期动作可能再弹人机验证 (CF Turnstile 或 'Click on X' 卡片), 循环处理直到无
            for i in range(1, 6):
                pending = False
                try:
                    pg = sb.get_page_source().lower()
                    if re.search(r"click on\s+[a-z]+", pg):
                        pending = True
                    if "i am not a robot" in pg:
                        pending = True
                except Exception:
                    pass
                if not pending:
                    log("   页面无待解人机验证")
                    break
                _challenge_card(sb)
                if _click_turnstile(sb):
                    log(f"   第 {i} 次点击续期动作 Turnstile...")
                sb.sleep(4)
            sb.sleep(4)
            try:
                sb.save_screenshot("acl_browser_renew_clicked.png")
            except Exception:
                pass
    except Exception as e:
        log(f"❌ 浏览器 fallback 异常: {e}")
        return None

    # 7) API 验证 expires_at 是否后移 (后端生效可能有延迟, 重试 3 轮)
    time.sleep(3)
    for _ in range(3):
        try:
            detail = server_detail(session, srv["id"])
            _, new_str = find_expire({}, detail)
            new_exp = parse_iso(new_str) if new_str else None
            if new_exp:
                new_rem = (new_exp - datetime.now(timezone.utc)).total_seconds()
                if new_rem > old_remaining + 60:
                    log(f"✅ 浏览器 fallback 续期成功: {fmt_remaining(old_remaining)} → {fmt_remaining(new_rem)}")
                    return int(new_rem)
        except Exception:
            pass
        time.sleep(5)
    log("❌ 浏览器 fallback: 按钮已点击但 expires_at 未后移 (动作未真正发出或后端未生效)")
    return None


# ==================== 单账号续期流程 ====================
def process_account(label, cookie_str):
    log(f"\n{'='*60}")
    log(f"👤 账号: {label}")
    log(f"🌐 站点: {BASE_URL}")
    log(f"{'='*60}")

    if not cookie_str:
        return {"label": label, "ok": False, "msg": "Cookie 为空", "renewed": 0, "failed": 0}

    # 友好提示: Cookie 里的 __Host- 域名与 BASE_URL 是否匹配
    if "dash.aclclouds.com" in BASE_URL:
        log("⚠️ ACL_BASE_URL 指向 dash.aclclouds.com, 该域名现已 302 到 aclclouds.com")
        log("   (重定向会丢 Cookie 导致 401), 建议改回 https://aclclouds.com")

    session = build_api_session(cookie_str)

    # 1. 登录自检
    try:
        test_r = api_get(session, "/api/client")
    except Exception as e:
        log(f"❌ 网络异常: {e}")
        return {"label": label, "ok": False, "msg": f"网络异常: {e}", "renewed": 0, "failed": 0}

    if test_r.status_code == 200:
        log("✅ API 登录验证通过")
    elif test_r.status_code == 401:
        log("❌ 登录失败: HTTP 401")
        log("   原因排查:")
        log("   1) Cookie 过期 -> 重新登录 https://aclclouds.com/dashboard 复制新 Cookie")
        log("   2) Cookie 是从 dash.aclclouds.com 复制的旧会话 -> 必须从 aclclouds.com 复制")
        log("   3) ACL_BASE_URL 指向了 dash.aclclouds.com -> 改回 https://aclclouds.com")
        log(f"   响应: {test_r.text[:300]}")
        return {"label": label, "ok": False, "msg": "登录失败 HTTP 401 (Cookie 无效/过期/域名不匹配)", "renewed": 0, "failed": 0}
    elif test_r.status_code == 403:
        log("❌ 登录失败: HTTP 403")
        log("   IP 被 Cloudflare 风控或需要 Turnstile 验证 (本机/数据中心 IP 常见)")
        return {"label": label, "ok": False, "msg": "登录失败 HTTP 403 (CF 风控/Turnstile)", "renewed": 0, "failed": 0}
    else:
        log(f"❌ 登录失败: HTTP {test_r.status_code}")
        log(f"   响应: {test_r.text[:300]}")
        return {"label": label, "ok": False, "msg": f"登录失败 HTTP {test_r.status_code}", "renewed": 0, "failed": 0}

    # 2. 拉取服务器列表
    servers = list_servers(session)
    if not servers:
        log("📦 没有服务器")
        return {"label": label, "ok": True, "msg": "无服务器", "renewed": 0, "failed": 0}

    log(f"📦 共 {len(servers)} 台服务器")
    if servers:
        first = servers[0]
        attrs = first.get("attributes", first) if isinstance(first, dict) else {}
        debug(f"首台服务器字段: {list(attrs.keys()) if isinstance(attrs, dict) else type(attrs).__name__}")

    now = datetime.now(timezone.utc)
    to_renew = []
    skipped = []

    for idx, srv in enumerate(servers, 1):
        attrs = srv.get("attributes", srv) if isinstance(srv, dict) else {}
        sid = extract_server_id(attrs)
        name = attrs.get("name", f"server-{idx}")
        if not sid:
            debug(f"{name}: 缺少 server id, attrs keys={list(attrs.keys()) if isinstance(attrs, dict) else '?'}")
            skipped.append(f"⚠️ {name}: 缺少 server id")
            continue

        # 到期时间 (列表没有则拉详情)
        _, expire_str = find_expire(attrs)
        detail = None
        if not expire_str:
            detail = server_detail(session, sid)
            if detail:
                _, expire_str = find_expire(attrs, detail)

        # 改版后的续期状态字段
        can_renew, free_left, reason = renewal_availability(attrs, detail)
        # 面板自动续期开关 (改版新增): auto_renew=true 时由面板自己续, 脚本跳过
        auto_renew = None
        for c in (attrs, detail):
            if isinstance(c, dict) and c.get("auto_renew") is not None:
                auto_renew = bool(c.get("auto_renew"))
                break
        if auto_renew:
            log(f"  ⏭️ {name}: 面板已开启自动续期 (auto_renew=true), 无需脚本处理")
            skipped.append(f"⏭️ {name}: auto_renew=true (面板自动续期)")
            continue
        if can_renew is False:
            log(f"  ⏭️ {name}: {reason or '不可续期'}")
            skipped.append(f"⏭️ {name}: {reason or '不可续期'}")
            continue
        if can_renew is True and free_left == 0:
            log(f"  ⏭️ {name}: 免费续期次数已用完 (free_renewals_remaining=0)")
            skipped.append(f"⏭️ {name}: 免费续期次数已用完")
            continue

        if not expire_str:
            log(f"  ⏭️ {name}: 无到期时间字段 (attrs keys 见 DEBUG=1)")
            skipped.append(f"⚠️ {name}: 无到期时间字段")
            continue

        expire = parse_iso(expire_str)
        if not expire:
            log(f"  ⏭️ {name}: 到期时间格式错误 ({expire_str})")
            skipped.append(f"⚠️ {name}: 到期时间格式错误")
            continue

        remaining = (expire - now).total_seconds()
        remaining_h = remaining / 3600
        log(f"  - {name}: 到期 {expire_str} | 剩余 {fmt_remaining(remaining)} ({remaining_h:.1f}h)"
            + (f" | 免费续期余 {free_left}" if free_left is not None else ""))

        if remaining_h < RENEW_THRESHOLD_HOURS:
            to_renew.append({"id": sid, "name": name, "remaining": remaining})
        else:
            skipped.append(f"⏭️ {name}: 剩 {fmt_remaining(remaining)}, 未到阈值 {RENEW_THRESHOLD_HOURS:g}h")

    if not to_renew:
        msg = f"所有服务器剩余时间充足, 无需续期" if not skipped else f"无需续期 ({len(skipped)} 台跳过)"
        log(f"ℹ️ {msg}")
        return {"label": label, "ok": True, "msg": msg, "renewed": 0, "failed": 0, "results": skipped}

    log(f"🔄 需要续期 {len(to_renew)} 台服务器")

    # 3. 执行续期
    renewed = 0
    failed = 0
    results = list(skipped)

    for srv in to_renew:
        log(f"\n🖥️ 续期: {srv['name']} (id={srv['id']})")
        try:
            r, captcha = renew_server_api(session, srv["id"])

            if r.status_code == 404:
                # 大概率是 id 类型不对 (完整 uuid 而非短标识)
                log(f"❌ HTTP 404: 服务器 id 可能不是短标识 ({srv['id']})")
                results.append(f"❌ {srv['name']}: 404 (id 格式问题)")
                failed += 1
            elif r.status_code == 401:
                log(f"❌ HTTP 401: 会话失效, 本次不再继续")
                results.append(f"❌ {srv['name']}: 401 会话失效")
                failed += 1
                break
            elif r.status_code == 403 and captcha:
                log(f"🛡️ API 续期被 Cloudflare 风控拦截 (需要 Turnstile), 切浏览器 fallback...")
                new_rem = renew_via_browser(srv, cookie_str, session, srv["remaining"])
                if new_rem is not None:
                    results.append(f"✅ {srv['name']}: API 被 Turnstile 拦截, 浏览器 fallback 续期成功 "
                                   f"({fmt_remaining(srv['remaining'])} → {fmt_remaining(new_rem)})")
                    log(f"✅ {srv['name']}: 浏览器 fallback 续期成功")
                    renewed += 1
                else:
                    results.append(f"❌ {srv['name']}: 被 Turnstile 拦截, 浏览器 fallback 亦失败")
                    log(f"❌ {srv['name']}: 浏览器 fallback 失败")
                    failed += 1
            elif r.status_code in (200, 201, 202, 204):
                # 2xx 也可能 body 带错误 (如 renewNotAvailableYet)
                err = renew_error_msg(r)
                if err:
                    log(f"⏭️ 后端拒绝: {err}")
                    results.append(f"⏭️ {srv['name']}: {err}")
                    # 未真正续期, 但也不算失败
                else:
                    time.sleep(1.5)
                    new_detail = server_detail(session, srv["id"])
                    _, new_expire_str = find_expire({}, new_detail)
                    new_expire = parse_iso(new_expire_str) if new_expire_str else None
                    if new_expire:
                        # 只接受到期时间实际向后移动；HTTP 2xx 本身不等于续期成功。
                        old_expire = now + timedelta(seconds=srv['remaining'])
                        if new_expire <= old_expire:
                            results.append(f"❌ {srv['name']}: 返回 2xx 但 expires_at 没有增加")
                            log(f"❌ 续期未确认: expires_at 未增加 ({new_expire.isoformat()})")
                            failed += 1
                        else:
                            new_remaining = (new_expire - now).total_seconds()
                            results.append(f"✅ {srv['name']}: {fmt_remaining(srv['remaining'])} → {fmt_remaining(new_remaining)}")
                            log(f"✅ 续期成功: {fmt_remaining(srv['remaining'])} → {fmt_remaining(new_remaining)}")
                            renewed += 1
                    else:
                        results.append(f"❌ {srv['name']}: 2xx 但无法读取续期后的 expires_at")
                        log("❌ 续期未确认: 2xx 但详情无 expires_at")
                        failed += 1
            else:
                body = r.text[:200]
                err = renew_error_msg(r)
                results.append(f"❌ {srv['name']}: HTTP {r.status_code} {err or body}")
                failed += 1
                log(f"❌ 续期失败: HTTP {r.status_code} {err or body}")
        except Exception as e:
            results.append(f"❌ {srv['name']}: {e}")
            failed += 1
            log(f"❌ 异常: {e}")

        time.sleep(2)

    return {
        "label": label,
        "ok": failed == 0,
        "msg": f"成功 {renewed} 台, 失败 {failed} 台",
        "renewed": renewed,
        "failed": failed,
        "results": results,
    }


# ==================== 主入口 ====================
def collect_accounts():
    """返回 [(label, cookie_str), ...]"""
    accounts = []

    if MULTI_ACCOUNTS:
        for line in MULTI_ACCOUNTS.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|||" in line:
                name, ck = line.split("|||", 1)
                accounts.append((name.strip(), ck.strip()))
            else:
                accounts.append((f"account-{len(accounts)+1}", line))

    if not accounts and COOKIE:
        accounts.append(("main", COOKIE))

    return accounts


def build_summary(all_results):
    """构建 TG 汇总消息"""
    renewed_total = sum(r.get("renewed", 0) for r in all_results)
    failed_total = sum(r.get("failed", 0) for r in all_results)

    lines = ["🎮 ACLClouds 自动续期", f"⏰ {now_str()}", ""]
    lines.append(f"📊 ✅ {renewed_total} | ❌ {failed_total}")
    lines.append("")

    for r in all_results:
        if not r.get("ok"):
            lines.append(f"👤 {r['label']}: ❌ {r.get('msg', '失败')}")
        else:
            lines.append(f"👤 {r['label']}: ✅ {r.get('msg', '成功')}")
        if r.get("results"):
            for res in r["results"]:
                lines.append(f"  {res}")
        lines.append("")

    return "\n".join(lines)


def main():
    log(f"🚀 ACLClouds 续期脚本启动 @ {now_str()}")
    log(f"🌐 站点: {BASE_URL}")
    log(f"⏰ 续期阈值: {RENEW_THRESHOLD_HOURS:g}h")

    if "dash.aclclouds.com" in BASE_URL:
        log("⚠️ 检测到 ACL_BASE_URL=dash.aclclouds.com, 该域名已废弃(302->aclclouds.com)")
        log("  请使用: export ACL_BASE_URL=https://aclclouds.com")

    accounts = collect_accounts()

    # 没有 Cookie 但有账密 → 直接用浏览器登录获取
    if not accounts and ACL_EMAIL and ACL_PASSWORD:
        log("🔑 未配置 Cookie, 尝试浏览器自动登录...")
        fresh = browser_login()
        if fresh:
            accounts = [("main", fresh)]
            log("✅ 浏览器登录成功, 使用新 Cookie 执行续期")
            save_cookie_cache(fresh)
            if GH_TOKEN:
                update_acl_secret(fresh)
        else:
            msg = ("❌ 浏览器登录失败\n\n"
                   "请检查:\n"
                   "- ACL_EMAIL/ACL_PASSWORD 是否正确\n"
                   "- 代理能否过 Turnstile (IS_PROXY=true + 干净代理)\n"
                   "- 是否触发 2FA (需手动处理)")
            log(msg)
            send_tg(msg)
            sys.exit(1)

    if not accounts:
        msg = ("❌ 未配置 ACL_COOKIES / ACL_ACCOUNTS / (ACL_EMAIL+ACL_PASSWORD)\n\n"
               "请使用以下环境变量之一:\n"
               "- ACL_COOKIES: 单账号 Cookie (来自 https://aclclouds.com)\n"
               "- ACL_ACCOUNTS: 多账号 (格式: name|||cookie)\n"
               "- ACL_EMAIL + ACL_PASSWORD: 浏览器自动登录 (免手动换 Cookie)")
        log(msg)
        send_tg(msg)
        sys.exit(1)

    log(f"📋 共 {len(accounts)} 个账号")

    all_results = []
    for label, ck in accounts:
        try:
            res = process_account(label, ck)
            if res.get("ok"):
                # API 登录成功 → 把当前可用 cookie 刷入缓存 (首单预热 + 保活)
                save_cookie_cache(ck)
            # Cookie 登录 401 且有账密 → 浏览器自动登录重试 (免手动换 Cookie)
            if (not res.get("ok")) and ACL_EMAIL and ACL_PASSWORD and "401" in str(res.get("msg", "")):
                log("🔑 Cookie 登录 401, 尝试浏览器自动登录重试...")
                fresh = browser_login()
                if fresh:
                    log("✅ 浏览器登录成功, 用新 Cookie 重试续期")
                    res = process_account(label, fresh)
                    save_cookie_cache(fresh)
                    if GH_TOKEN:
                        update_acl_secret(fresh)
                else:
                    log("⚠️ 浏览器登录失败, 维持 401 结果")
        except Exception as e:
            res = {"label": label, "ok": False, "msg": f"异常: {e}", "renewed": 0, "failed": 1}
        all_results.append(res)

    summary = build_summary(all_results)
    print("\n" + summary + "\n")
    send_tg(summary)
    if any(not r.get("ok", False) for r in all_results):
        log("❌ 存在续期失败, exit 1 (Actions 将标红)")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n用户中断")
    except Exception as e:
        log(f"💥 未捕获异常: {e}")
        send_tg(f"🎮 ACLClouds 续期\n\n💥 脚本崩溃: {e}")
        sys.exit(1)
