#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Scyed 免费服自动续期（纯 HTTP，单账号单服，一次 +30 天）
# 接口基于 jacksun-king/Scyed-Renew，通知/告警按自家标准重做
# 流程：Cookie + next-action 头 POST 续期 → GET 回页面抓德语日期验成功
# Secrets（密钥只进 Secrets，永不落盘）：
#   SCYED_COOKIE     必填，scyed.com 的 Cookie 串（至少含 __Secure-better-auth.session_token，约7天命）
#   SCYED_SERVER_IDS 必填，服务器 ID（面板 URL 里那段，如 f8f9d4fa；多服逗号分隔）
#   NEXT_ACTION      可选，Next.js Server Action 哈希（默认内置，站改版失效时更新此项）
#   EMAIL            可选，通知备注名
#   NODE_LINK        可选，代理链接（CF 403 必须挂代理）
#   TG_BOT_TOKEN / TG_CHAT_ID 可选，通知用

import os, re, sys, time, json, requests
from datetime import datetime, timezone, timedelta

BASE_URL = "https://scyed.com"
# jacksun 实测有效的 next-action；站改版后若失效，用 Secrets 的 NEXT_ACTION 覆盖
DEFAULT_NEXT_ACTION = "40e3b5155f7802d90d31a92e262afcf125cf630a5f"
NEXT_ACTION = os.environ.get("NEXT_ACTION") or DEFAULT_NEXT_ACTION

COOKIE_RAW = os.environ.get("SCYED_COOKIE") or ""
IDS_RAW    = os.environ.get("SCYED_SERVER_IDS") or ""
EMAIL      = os.environ.get("EMAIL") or ""
NODE_LINK  = os.environ.get("NODE_LINK") or ""
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
TIMEOUT = 30

# 代理：显式 SCYED_PROXY > workflow 的 IS_PROXY/PROXY_SERVER > 标准 HTTP(S)_PROXY
PROXIES = {}
_ep = os.environ.get("SCYED_PROXY") or ""
if _ep:
    PROXIES = {"http": _ep, "https": _ep}
elif os.environ.get("IS_PROXY", "false").lower() == "true":
    _p = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    PROXIES = {"http": _p, "https": _p}
else:
    _h = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    _s = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if _h or _s:
        PROXIES = {"http": _h, "https": _s or _h}

# 冷却文案关键词 → 视为"未到窗口"，不算失败
COOLDOWN_KEYWORDS = ("wait", "hour", "extension failed", "extending again",
                     "too soon", "cooldown", "try again later")


def log(msg):
    print(msg, flush=True)


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
                      json={"chat_id": TG_CHAT_ID, "text": message},
                      timeout=10, proxies=PROXIES or None)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")


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


GERMAN_MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni",
                 "Juli", "August", "September", "Oktober", "November", "Dezember"]


def normalize_german_date(s: str) -> str:
    """10. Oktober 2026 um 10:20 → 2026-10-10 10:20，顺带返回可比较的 datetime。"""
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
    """从升级页 HTML 提到期时间。返回 (原文, datetime|None)。优先 Neues Ablaufdatum。"""
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
    return "", None


def has_cooldown(text: str) -> str:
    """检查冷却文案，命中返回原文片段，否则返回空串。"""
    low = (text or "").lower()
    if "wait" in low and "hour" in low:
        m = re.search(r"wait\s+\d+\s+more\s+hours?.{0,40}", text, re.IGNORECASE)
        return m.group(0).strip() if m else "Extension cooldown"
    if "extension failed" in low:
        return "Extension Failed"
    return ""


def renew_page_url(server_id: str) -> str:
    return f"{BASE_URL}/de/gameserver/{server_id}/upgrade/freeServer?extend=30"


def do_renew_once(cookies: dict, server_id: str):
    """单次 POST 续期。返回 (status, info)：success / cooling / failed + 说明。"""
    url = renew_page_url(server_id)
    headers = {
        "accept": "text/x-component",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "text/plain;charset=UTF-8",
        "next-action": NEXT_ACTION,
        "origin": BASE_URL,
        "referer": url,
        "user-agent": UA,
    }
    try:
        r = requests.post(url, headers=headers, cookies=cookies,
                          data=json.dumps([server_id]),
                          timeout=TIMEOUT, proxies=PROXIES or None)
    except Exception as e:
        return "failed", f"POST 异常: {e}"
    body = r.text or ""
    print(f"📡 POST renew → HTTP {r.status_code}")
    if r.status_code == 403:
        return "failed", "403 Forbidden：Cookie 失效或被 CF 拦截，请重拷 COOKIE / 检查代理"
    if r.status_code == 401:
        return "failed", "401：Cookie 已失效，请重拷 COOKIE"
    if r.status_code != 200:
        return "failed", f"HTTP {r.status_code}: {body[:200]}"
    cd = has_cooldown(body)
    if cd:
        return "cooling", cd
    return "success", body


def fetch_expiry(cookies: dict, server_id: str):
    """GET 升级页抓到期时间。返回 (原文, datetime|None, err)。"""
    headers = {"accept": "text/html,*/*;q=0.8", "user-agent": UA,
               "referer": f"{BASE_URL}/de/gameserver/{server_id}/upgrade"}
    try:
        r = requests.get(renew_page_url(server_id), headers=headers,
                         cookies=cookies, timeout=TIMEOUT, proxies=PROXIES or None)
        if r.status_code != 200:
            return "", None, f"GET {r.status_code}"
        return (*extract_expiry(r.text), "")
    except Exception as e:
        return "", None, f"GET 异常: {e}"


def renew_server(cookies: dict, server_id: str) -> dict:
    """完整流程：先 GET 记旧日期 → POST → 冷却即停 → GET 验新日期。"""
    old_raw, old_dt, err = fetch_expiry(cookies, server_id)
    if err:
        print(f"⚠️ 旧日期抓取失败: {err}（继续 POST，用回包判定）")
    else:
        print(f"📅 旧到期: {old_raw or '（未提取到）'}")

    st, info = do_renew_once(cookies, server_id)
    if st == "cooling":
        return {"ok": True, "summary": f"⏳ 冷却中（{info}），下次 cron 再续",
                "old": old_raw, "new": old_raw}
    if st == "failed":
        # 疑似 next-action 失效：200 以外且非 401/403 时提示检查哈希
        extra = ""
        if not info.startswith(("403", "401")) and "HTTP" in info:
            extra = "（若持续出现，可能是 next-action 哈希失效，需更新 NEXT_ACTION）"
        return {"ok": False, "summary": f"续期失败: {info}{extra}",
                "old": old_raw, "new": ""}

    time.sleep(5)
    new_raw, new_dt, err2 = fetch_expiry(cookies, server_id)
    if err2:
        return {"ok": True, "summary": "✅ POST 成功，但新日期抓取失败，请人工核对",
                "old": old_raw, "new": "（请人工核对）"}
    if old_dt and new_dt and new_dt > old_dt:
        gain = (new_dt - old_dt).days
        return {"ok": True, "summary": f"✅ 续期成功（+{gain}天）",
                "old": old_raw, "new": new_raw}
    if new_raw and new_raw != old_raw:
        return {"ok": True, "summary": "✅ 续期成功（日期已变化）",
                "old": old_raw, "new": new_raw}
    return {"ok": True, "summary": "✅ POST 成功（日期未见变化，请人工核对）",
            "old": old_raw, "new": new_raw or "（请人工核对）"}


def main():
    print("#" * 25)
    print("   Scyed 自动续期（一次+30天）")
    print("#" * 25)
    if not COOKIE_RAW:
        print("ℹ️ 未配置 SCYED_COOKIE，脚本终止。")
        sys.exit(1)
    server_ids = [s.strip() for s in IDS_RAW.split(",") if s.strip()]
    if not server_ids:
        print("ℹ️ 未配置 SCYED_SERVER_IDS，脚本终止。")
        sys.exit(1)
    cookies = dict(parse_cookie_str(COOKIE_RAW))
    if "__Secure-better-auth.session_token" not in cookies:
        print("⚠️ COOKIE 里没找到 session_token，登录态可能无效，继续尝试…")
    print(f"📋 {len(server_ids)} 台服：{', '.join(server_ids)}")
    print(f"🔗 代理：{'已配置' if PROXIES else '直连'}")
    print(f"🔑 next-action：{NEXT_ACTION[:8]}…（{'默认' if NEXT_ACTION == DEFAULT_NEXT_ACTION else '自定义'}）")

    results = []
    for sid in server_ids:
        print(f"\n▶️ {sid}")
        try:
            r = renew_server(cookies, sid)
        except Exception as e:
            import traceback
            traceback.print_exc()
            r = {"ok": False, "summary": f"执行异常：{e}", "old": "", "new": ""}
        results.append((sid, r))
        print(f"   {r['summary']}")
        if r["ok"]:
            send_telegram_message(format_notification(
                "✅ 续期成功" if "成功" in r["summary"] else "ℹ️ 续期状态",
                server_id=sid, old=r["old"], new=r["new"], extra=r["summary"]))
        else:
            send_telegram_message(format_notification(
                "❌ 续期失败", server_id=sid, old=r["old"], error=r["summary"]))

    ok_n = sum(1 for _, r in results if r["ok"])
    print(f"\n🏁 完成：{ok_n}/{len(results)} 成功")
    if ok_n < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
