#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Scyed 免费服自动续期（浏览器点击版，单账号，一次 +30 天）
# 主链：SeleniumBase 注入 Cookie → 打开续期页 → 点免费延期按钮 → toast/日期验成功
# 备链：Cookie 401/登录失效时，用 DISCORD_TOKEN 走纯 HTTP OAuth 重登并回写
# Secrets（密钥只进 Secrets，永不落盘）：
#   SCYED_COOKIE     必填，scyed.com 的 Cookie 串（至少含 __Secure-better-auth.session_token，约7天命）
#   SCYED_SERVER_IDS 必填，服务器 ID（如 f8f9d4fa；多服逗号分隔）
#   DISCORD_TOKEN    可选，Discord Token；登录失效时自动 OAuth 重登并回写
#   GH_TOKEN         可选，GitHub classic PAT（回写 SCYED_COOKIE 用）
#   EMAIL            可选，通知备注名
#   NODE_LINK        可选，代理链接（sing-box，站有 CF）
#   TG_BOT_TOKEN / TG_CHAT_ID 可选，通知用（含截图）

import os, re, sys, time, json, subprocess, requests
import urllib.parse
from datetime import datetime, timezone, timedelta
from seleniumbase import SB

BASE_URL = "https://scyed.com"
COOKIE_RAW = os.environ.get("SCYED_COOKIE") or ""
IDS_RAW    = os.environ.get("SCYED_SERVER_IDS") or ""
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""
GH_TOKEN   = os.environ.get("GH_TOKEN") or ""
EMAIL      = os.environ.get("EMAIL") or ""
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
TIMEOUT = 30
DOMAIN = "scyed.com"
SESSION_COOKIE_NAME = "__Secure-better-auth.session_token"

# 冷却文案关键词 → 视为"未到窗口"，不算失败
COOLDOWN_KEYWORDS = ("wait", "hour", "extension failed", "extending again",
                     "too soon", "cooldown", "try again later")
# 成功 toast 关键词（多语言）
SUCCESS_KEYWORDS = ("erfolg", "success", "成功", "erweitert", "extended",
                    "verlängert", "renewed", "neues ablaufdatum")


def mask_email(email: str) -> str:
    if "@" in email:
        name, domain = email.split("@", 1)
        return f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else f"{name}@{domain}"
    return (email[:2] + "****") if email else "（未填）"


def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 文字通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")


def send_telegram_photo(message: str, image_path: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    try:
        if image_path and os.path.isfile(image_path):
            with open(image_path, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                    data={"chat_id": TG_CHAT_ID, "caption": message[:1000]},
                    files={"photo": (os.path.basename(image_path), f, "image/png")},
                    timeout=20)
            if r.status_code == 200:
                print("✅ Telegram 截图通知已发送")
                return
            print(f"⚠️ sendPhoto 失败({r.status_code})，降级为文字通知")
    except Exception as e:
        print(f"⚠️ 截图通知异常，降级为文字通知: {e}")
    send_telegram_message(message)


def format_notification(status: str, server_id: str = "", old: str = "",
                        new: str = "", error: str = "", extra: str = "") -> str:
    now = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    lines = ["🆓 Scyed 续期通知", "", f"{status}",
             f"👤 账户: {mask_email(EMAIL)}"]
    if server_id:
        lines.append(f"🆔 服务器: {server_id}")
    if new:
        lines.append(f"📅 续期前到期: {old or '（未获取到）'}")
        lines.append(f"📅 续期后到期: {new}")
    elif old:
        lines.append(f"📅 当前到期: {old}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 执行时间: {now}(北京时间)")
    return "\n".join(lines)


def parse_cookie_str(raw: str):
    pairs = []
    for item in (raw or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name, value = name.strip(), value.strip()
        if name and value:
            pairs.append((name, value))
    return pairs


def get_current_ip(proxy_server: str = "") -> str:
    proxies = {"http": proxy_server, "https": proxy_server} if proxy_server else None
    r = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    r.raise_for_status()
    return r.text.strip()


GERMAN_MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni",
                 "Juli", "August", "September", "Oktober", "November", "Dezember"]


def normalize_german_date(s: str):
    mmap = {m.lower(): i + 1 for i, m in enumerate(GERMAN_MONTHS)}
    m = re.search(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})(?:\s*um\s*(\d{1,2}):(\d{2}))?",
                  (s or "").strip())
    if not m:
        return (s or "").strip(), None
    mon = mmap.get(m.group(2).strip().lower())
    if not mon:
        return s.strip(), None
    try:
        dt = datetime(int(m.group(3)), mon, int(m.group(1)),
                      int(m.group(4) or 0), int(m.group(5) or 0))
    except ValueError:
        return s.strip(), None
    return dt.strftime("%Y-%m-%d %H:%M"), dt


def extract_expiry(text: str):
    for pattern in (r"Neues Ablaufdatum[^<]*?>\s*([^<]+?)\s*<",
                    r"Läuft ab am[^<]*?>\s*([^<]+?)\s*<",
                    r"Ablaufdatum[^<]*?>\s*([^<]+?)\s*<"):
        m = re.search(pattern, text or "", re.IGNORECASE)
        if m and m.group(1).strip():
            return normalize_german_date(m.group(1))
    m = re.search(r"\d{1,2}\.\s*(?:Januar|Februar|März|Mai|Juni|Juli|August|September|"
                  r"Oktober|November|Dezember)\s+\d{4}(?:\s*um\s*\d{1,2}:\d{2})?",
                  text or "", re.IGNORECASE)
    if m:
        return normalize_german_date(m.group(0))
    # 中文翻译页兜底：2026年10月21日 / 2026年11月20日 12:01
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?", text or "")
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          int(m.group(4) or 0), int(m.group(5) or 0))
            return dt.strftime("%Y-%m-%d %H:%M"), dt
        except ValueError:
            pass
    return "", None


def has_cooldown(text: str) -> str:
    low = (text or "").lower()
    if "wait" in low and "hour" in low:
        m = re.search(r"wait\s+\d+\s+more\s+hours?.{0,40}", text, re.IGNORECASE)
        return m.group(0).strip() if m else "Extension cooldown"
    if "extension failed" in low:
        return "Extension Failed"
    return ""


def has_success(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in SUCCESS_KEYWORDS)


# ---------- Discord OAuth 自动重登（备链，纯 HTTP） ----------

DISCORD_API = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")


def update_github_secret(secret_name, new_value):
    if not new_value:
        return False
    print(f"🔄 更新 Secret: {secret_name}（长度 {len(new_value)}）")
    try:
        env = os.environ.copy()
        if GH_TOKEN:
            env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(["gh", "secret", "set", secret_name, "--body", new_value],
                              capture_output=True, text=True, timeout=30,
                              check=False, env=env)
        if proc.returncode == 0:
            return True
        print(f"❌ 更新失败: {proc.stderr.strip()}")
        return False
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False


def parse_dc_token(raw: str) -> str:
    return raw.split(",", 1)[-1].strip() if raw else ""


def signin_social_url(proxies):
    try:
        r = requests.post(f"{BASE_URL}/api/auth/sign-in/social",
                          json={"provider": "discord", "callbackURL": "/",
                                "errorCallbackURL": "/sign-in",
                                "newUserCallbackURL": "/"},
                          headers={"Content-Type": "application/json",
                                   "Origin": BASE_URL, "Referer": f"{BASE_URL}/",
                                   "User-Agent": UA},
                          timeout=TIMEOUT, proxies=proxies or None)
        if r.status_code != 200:
            return "", f"sign-in/social HTTP {r.status_code}: {r.text[:200]}"
        url = r.json().get("url", "")
        return (url, "") if url else ("", f"响应无 url 字段: {str(r.json())[:200]}")
    except Exception as e:
        return "", f"sign-in/social 异常: {e}"


def discord_authorize(authorize_url: str, dc_token: str, proxies) -> str:
    try:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(authorize_url).query))
        referer = "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode({
            k: q[k] for k in ("client_id", "redirect_uri", "response_type", "scope", "state")
            if k in q})
        headers = {"accept": "*/*", "authorization": dc_token,
                   "content-type": "application/json", "origin": "https://discord.com",
                   "referer": referer, "user-agent": DISCORD_UA,
                   "x-discord-locale": "zh-CN"}
        body = json.dumps({"permissions": "0", "authorize": True,
                           "integration_type": 0,
                           "location_context": {"guild_id": "10000", "channel_id": "10000",
                                                "channel_type": 10000}})
        resp = requests.post(authorize_url, headers=headers, data=body,
                             timeout=20, proxies=proxies or None)
        if resp.status_code != 200:
            print(f"❌ Discord 授权失败: HTTP {resp.status_code} - {resp.text[:200]}")
            return ""
        location = resp.json().get("location", "")
        if location:
            print("✅ Discord 授权成功，拿到 callback URL")
        return location
    except Exception as e:
        print(f"❌ Discord 授权异常: {e}")
        return ""


def do_oauth_relogin(old_raw: str, proxies):
    dc_token = parse_dc_token(DISCORD_TOKEN)
    if not dc_token:
        return "", "未配置 DISCORD_TOKEN"
    print("🔑 Cookie 失效，走 Discord OAuth 重登...")
    authorize_url, err = signin_social_url(proxies)
    if not authorize_url or "discord.com" not in authorize_url:
        return "", err or f"授权 URL 异常: {authorize_url[:120]}"
    location = discord_authorize(authorize_url, dc_token, proxies)
    if not location or "scyed.com" not in location:
        return "", "Discord 授权未返回站方 callback"
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        r = s.get(location, timeout=TIMEOUT, proxies=proxies or None)
        print(f"📡 callback → HTTP {r.status_code}")
        token = s.cookies.get(SESSION_COOKIE_NAME, domain="scyed.com") or ""
        if not token:
            for c in s.cookies:
                if "session_token" in c.name:
                    token = c.value
                    break
        if not token:
            return "", "callback 后未捕获到 session Cookie"
        merged = dict(parse_cookie_str(old_raw))
        merged[SESSION_COOKIE_NAME] = token
        for c in s.cookies:
            if c.value and c.name not in merged:
                merged[c.name] = c.value
        print("✅ 重登成功，拿到新 session")
        return "; ".join(f"{k}={v}" for k, v in merged.items() if v), ""
    except Exception as e:
        return "", f"callback 异常: {e}"


# ---------- 浏览器主链 ----------

def renew_page_url(server_id: str) -> str:
    return f"{BASE_URL}/de/gameserver/{server_id}/upgrade/freeServer?extend=30"


def browser_login(sb, cookie_raw, server_id) -> bool:
    pairs = parse_cookie_str(cookie_raw)
    if not pairs:
        print("❌ COOKIE 为空或格式错误")
        return False
    sb.open(BASE_URL + "/")
    sb.wait_for_ready_state_complete()
    sb.sleep(2)
    try:
        sb.delete_all_cookies()
    except Exception:
        pass
    for name, value in pairs:
        try:
            sb.add_cookie({"name": name, "value": value, "domain": DOMAIN})
        except Exception as e:
            print(f"⚠️ 注入 Cookie {name} 失败: {e}")
    sb.open(renew_page_url(server_id))
    sb.wait_for_ready_state_complete()
    sb.sleep(6)
    try:
        text = sb.get_text("body")
    except Exception:
        text = ""
    url = sb.get_current_url()
    if ("gameserver" in url and ("Ablaufdatum" in text or "Expiry" in text
        or "到期" in text or "freeServer" in text or "Verlänger" in text)):
        print("✅ Cookie 登录成功，已到达续期页")
        return True
    print(f"❌ Cookie 登录失败，URL={url}")
    return False


RENEW_BUTTON_SELECTORS = [
    'button:contains("Extend for Free")',  # 实测英文站精确文本，优先
    'button:contains("免费延期")',
    'button:contains("Verlängern")',
    'button:contains("Verlängerung")',
    'button:contains("Extend")',
    'button:contains("Renew")',
    'button:contains("Prolonger")',
]


def find_renew_button(sb, timeout=30):
    """轮询找续期按钮（含客户端 hydration 等待）；找不到则返回 (None, '')。"""
    start = time.time()
    while time.time() - start < timeout:
        for sel in RENEW_BUTTON_SELECTORS:
            try:
                if sb.is_element_visible(sel):
                    t = sb.get_text(sel)
                    # 排除升级付费按钮
                    if any(k in t for k in ("付费", "Premium", "Upgrade", "升级", "Bezah")):
                        continue
                    return sel, t.strip()
            except Exception:
                continue
        sb.sleep(2)
    return None, ""


def log_all_buttons(sb):
    """找不到按钮时打印全页按钮文本，下次加选择器用。"""
    try:
        texts = []
        for el in sb.find_elements("button"):
            try:
                t = (el.text or "").strip().replace("\n", " ")
                if t:
                    texts.append(t)
            except Exception:
                pass
        print(f"🔍 全页按钮文本: {texts}")
    except Exception as e:
        print(f"⚠️ 枚举按钮失败: {e}")


def renew_one_server(sb, cookie_raw, server_id) -> dict:
    result = {"ok": False, "summary": "未知", "old": "", "new": ""}
    shot = f"result_{server_id}.png"

    if not browser_login(sb, cookie_raw, server_id):
        result["summary"] = "❌ 登录失败（Cookie 失效）"
        return result

    try:
        page_text = sb.get_text("body")
    except Exception:
        page_text = ""
    old_raw, old_dt = extract_expiry(page_text)
    print(f"📅 旧到期: {old_raw or '（未提取到）'}")
    result["old"] = old_raw

    sel, btn_text = find_renew_button(sb)
    if not sel:
        cd = has_cooldown(page_text)
        if cd:
            result.update(ok=True, summary=f"⏳ 冷却中（{cd}），下次 cron 再续", new=old_raw)
            return result
        log_all_buttons(sb)
        result["summary"] = "ℹ️ 未找到续期按钮，请手动检查"
        return result

    print(f"✅ 发现续期按钮: '{btn_text}'，点击...")
    try:
        sb.click(sel)
    except Exception:
        try:
            el = sb.find_element(sel)
            sb.execute_script("arguments[0].click();", el)
        except Exception as e:
            result["summary"] = f"❌ 点击按钮失败: {e}"
            return result
    sb.sleep(8)
    try:
        new_text = sb.get_text("body")
    except Exception:
        new_text = ""
    try:
        sb.save_screenshot(shot)
    except Exception:
        shot = ""

    cd = has_cooldown(new_text)
    if cd:
        result.update(ok=True, summary=f"⏳ 冷却中（{cd}），下次 cron 再续", new=old_raw)
        return result
    new_raw, new_dt = extract_expiry(new_text)
    if old_dt and new_dt and new_dt > old_dt:
        gain = (new_dt - old_dt).days
        result.update(ok=True, summary=f"✅ 续期成功（+{gain}天）", new=new_raw)
    elif new_raw and new_raw != old_raw:
        result.update(ok=True, summary="✅ 续期成功（日期已变化）", new=new_raw)
    elif has_success(new_text):
        result.update(ok=True, summary="✅ 续期成功（toast 确认）", new=new_raw or "（已确认）")
    else:
        result.update(summary="⚠️ 结果未知，请手动检查", new=new_raw)
    result["shot"] = shot
    return result


def main():
    print("#" * 25)
    print("   Scyed 自动续期（浏览器点击版）")
    print("#" * 25)
    cookie_raw = (os.environ.get("SCYED_COOKIE") or "").strip()
    if not cookie_raw:
        print("ℹ️ 未配置 SCYED_COOKIE，脚本终止。")
        sys.exit(1)
    server_ids = [s.strip() for s in IDS_RAW.split(",") if s.strip()]
    if not server_ids:
        print("ℹ️ 未配置 SCYED_SERVER_IDS，脚本终止。")
        sys.exit(1)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
    sb_kwargs = {"uc": True, "headless": HEADLESS}
    proxies = None
    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
        proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER}
    else:
        print("🍭 未使用代理，直连访问")
    print(f"📋 {len(server_ids)} 台服：{', '.join(server_ids)}")

    results = []
    with SB(**sb_kwargs) as sb:
        try:
            print(f"📍 当前出口IP: {get_current_ip(PROXY_SERVER if IS_PROXY else '')}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")
        for sid in server_ids:
            print(f"\n▶️ {sid}")
            try:
                r = renew_one_server(sb, cookie_raw, sid)
            except Exception as e:
                import traceback
                traceback.print_exc()
                r = {"ok": False, "summary": f"执行异常：{e}", "old": "", "new": ""}
            # 登录失效 → OAuth 重登后整台重试一次
            if (not r["ok"] and "登录失败" in r["summary"]
                    and parse_dc_token(DISCORD_TOKEN)):
                new_raw, err = do_oauth_relogin(cookie_raw, proxies)
                if new_raw:
                    if GH_TOKEN and update_github_secret("SCYED_COOKIE", new_raw):
                        print("✅ SCYED_COOKIE 已回写")
                    else:
                        print("⚠️ 未配 GH_TOKEN，回写跳过")
                    cookie_raw = new_raw
                    try:
                        try:
                            sb.delete_all_cookies()
                        except Exception:
                            pass
                        r = renew_one_server(sb, cookie_raw, sid)
                        r["summary"] = "🔑 重登后" + r["summary"]
                    except Exception as e:
                        r = {"ok": False, "summary": f"重登后重试异常：{e}",
                             "old": "", "new": ""}
                else:
                    r["summary"] += f"；重登失败（{err}），请手动重拷 COOKIE"
            results.append((sid, r))
            print(f"   {r['summary']}")
            shot = r.get("shot", "")
            if r["ok"]:
                send_telegram_photo(format_notification(
                    "✅ 续期成功" if "成功" in r["summary"] else "ℹ️ 续期状态",
                    server_id=sid, old=r["old"], new=r["new"], extra=r["summary"]), shot)
            else:
                send_telegram_message(format_notification(
                    "❌ 续期失败", server_id=sid, old=r["old"], error=r["summary"]))

    ok_n = sum(1 for _, r in results if r["ok"])
    print(f"\n🏁 完成：{ok_n}/{len(results)} 成功")
    if ok_n < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
