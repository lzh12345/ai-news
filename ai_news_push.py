#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日 AI 资讯推送 —— 抓 aihot 日报 + DeepSeek 速览,推送到个人微信(Server酱 / PushPlus)。

纯标准库(urllib + json),无第三方依赖。设计为在 GitHub Actions cron 上每天运行,
也可本地运行测试。

用法:
    python ai_news_push.py              # 完整流程:抓取 + 速览 + 推送
    python ai_news_push.py --dry-run    # 只构建并打印,不推送(本地验证用)
    python ai_news_push.py --no-summary # 跳过 DeepSeek 速览
    python ai_news_push.py --date 2026-06-14  # 指定某天(测试)

配置(优先级:环境变量 > config.json > 单值文件):
    SERVERCHAN_SENDKEY  Server酱 SENDKEY(推荐;SCT 或 sctp 开头)
    PUSHPLUS_TOKEN      PushPlus token(与 Server酱 二选一;两者都配则优先 Server酱)
    DEEPSEEK_API_KEY    DeepSeek API key
    DEEPSEEK_MODEL      DeepSeek 模型(默认 deepseek-chat)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
AIHOT_BASE = "https://aihot.virxact.com"
# aihot 不带浏览器 UA 会被 nginx 返回 403,所有请求统一带上。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

# Server酱 / PushPlus 的 desp/content 上限较大(约 32KB),通常一条发完;
# 留足余量按 30000 字节切块,极重的日子才会拆成两条。
CHUNK_SOFT_LIMIT = 30000
# 个人微信渠道节流,块间 1 秒足够。
SEND_INTERVAL = 1.0

PUSH_TITLE_PREFIX = "今日 AI 资讯"

SECTION_ORDER = ["模型发布/更新", "产品发布/更新", "行业动态", "论文研究", "技巧与观点"]
CATEGORY_TO_LABEL = {
    "ai-models": "模型发布/更新",
    "ai-products": "产品发布/更新",
    "industry": "行业动态",
    "paper": "论文研究",
    "tip": "技巧与观点",
}

BJ = timezone(timedelta(hours=8))

OVERVIEW_SYSTEM = (
    "你是一名资深 AI 行业分析师,负责为忙碌的从业者撰写「今日 AI 速览」。要求:\n"
    "- 用简体中文。\n"
    "- 输出 3-5 条要点,每条一行,以「• 」开头。\n"
    "- 每条聚焦当天最重要的进展(模型发布、重大产品、关键行业动态、值得注意的论文/观点)。\n"
    "- 每条不超过 40 字,先说结论(谁做了什么 / 影响是什么),不要套话、不要寒暄、不要前后缀。\n"
    "- 只输出要点本身,不要标题、解释或多余文本。"
)
OVERVIEW_USER_PREFIX = (
    "以下是今天的 AI 资讯条目(按板块分组,标题 + 摘要),请据此生成「今日速览」要点:\n\n"
)


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
class AppError(Exception):
    """带 HTTP 状态码与响应体的统一错误,便于调用方分支处理(如识别 404)。"""

    def __init__(self, message, status=None, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


def log(msg):
    print(msg, flush=True)


SCRIPT_DIR = Path(__file__).resolve().parent
_CONFIG_CACHE = None


def _read_text_file(name):
    try:
        return (SCRIPT_DIR / name).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _load_config_json():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        path = SCRIPT_DIR / "config.json"
        if path.exists():
            try:
                _CONFIG_CACHE = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log("[warn] 读取 config.json 失败: %s" % exc)
                _CONFIG_CACHE = {}
        else:
            _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def get_conf(name, default=""):
    """配置读取:环境变量 > config.json > 默认值。"""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    val = str(_load_config_json().get(name, "") or "").strip()
    if val:
        return val
    return default


def get_deepseek_key():
    return get_conf("DEEPSEEK_API_KEY") or _read_text_file("deepseek_key.txt")


def detect_push_provider():
    """返回 (provider, secret):'serverchan' / 'pushplus' / (None, '')。Server酱 优先。"""
    sendkey = get_conf("SERVERCHAN_SENDKEY") or _read_text_file("serverchan_sendkey.txt")
    if sendkey:
        return "serverchan", sendkey
    token = get_conf("PUSHPLUS_TOKEN") or _read_text_file("pushplus_token.txt")
    if token:
        return "pushplus", token
    return None, ""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _read_error_body(exc):
    try:
        return exc.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", BROWSER_UA)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AppError(
            "HTTP %s GET %s" % (exc.code, url),
            status=exc.code,
            body=_read_error_body(exc),
        )
    except urllib.error.URLError as exc:
        raise AppError("网络错误 GET %s: %s" % (url, exc.reason))
    except Exception as exc:
        raise AppError("GET %s 失败: %s" % (url, exc))


def http_post_json(url, payload, headers=None, timeout=30):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", BROWSER_UA)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AppError(
            "HTTP %s POST %s" % (exc.code, url),
            status=exc.code,
            body=_read_error_body(exc),
        )
    except urllib.error.URLError as exc:
        raise AppError("网络错误 POST %s: %s" % (url, exc.reason))
    except Exception as exc:
        raise AppError("POST %s 失败: %s" % (url, exc))


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def to_beijing(iso_utc):
    """ISO8601 UTC(带 Z)-> 北京时间 'MM-DD HH:MM';解析失败返回原串。"""
    if not iso_utc:
        return ""
    try:
        s = iso_utc.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BJ).strftime("%m-%d %H:%M")
    except Exception:
        return iso_utc


def utc_iso_since(date_str=None):
    """兜底窗口的 since 参数。指定日期则取当天 0 点 UTC,否则取 24 小时前。"""
    if date_str:
        return date_str + "T00:00:00Z"
    dt = datetime.now(timezone.utc) - timedelta(hours=24)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def beijing_today_str():
    return datetime.now(BJ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 数据获取(含兜底)
# ---------------------------------------------------------------------------
def fetch_daily(date_str=None):
    """GET /api/public/daily。报告未生成(404)返回 None;其它错误上抛。"""
    if date_str:
        url = "%s/api/public/daily/%s" % (AIHOT_BASE, date_str)
    else:
        url = "%s/api/public/daily" % AIHOT_BASE
    try:
        return http_get_json(url)
    except AppError as exc:
        if exc.status == 404:
            return None
        raise


def fetch_items_fallback(since_iso, take=50):
    qs = urllib.parse.urlencode({"mode": "selected", "since": since_iso, "take": take})
    data = http_get_json("%s/api/public/items?%s" % (AIHOT_BASE, qs))
    return data.get("items") or []


def synthesize_daily_from_items(items, date_str):
    """把 /items 列表按 category 合成成 /daily 同构的结构。"""
    buckets = {}
    for it in items:
        label = CATEGORY_TO_LABEL.get(it.get("category") or "", "行业动态")
        buckets.setdefault(label, []).append(
            {
                "title": it.get("title") or "",
                "summary": it.get("summary") or "",
                "sourceUrl": it.get("url") or "",
                "sourceName": it.get("source") or "",
            }
        )
    sections = [
        {"label": label, "items": buckets[label]}
        for label in SECTION_ORDER
        if buckets.get(label)
    ]
    return {
        "date": date_str,
        "lead": None,
        "sections": sections,
        "flashes": [],
        "synthesized": True,
    }


def get_report(date_str=None):
    """优先 /daily;未生成则退回 /items 合成。返回归一化的 daily dict。"""
    daily = fetch_daily(date_str)
    if daily is not None:
        daily.setdefault("sections", [])
        daily.setdefault("flashes", [])
        if "lead" not in daily:
            daily["lead"] = None
        return daily
    log("[info] 当日编辑版日报尚未生成(404),退回最近精选列表合成。")
    items = fetch_items_fallback(utc_iso_since(date_str))
    return synthesize_daily_from_items(items, date_str or beijing_today_str())


# ---------------------------------------------------------------------------
# DeepSeek 速览(best-effort)
# ---------------------------------------------------------------------------
def build_summary_input(daily, max_items=25, summary_cap=200):
    lines = []
    count = 0
    for sec in daily.get("sections", []):
        items = sec.get("items") or []
        if not items:
            continue
        lines.append("【%s】" % (sec.get("label") or ""))
        for it in items:
            if count >= max_items:
                break
            title = (it.get("title") or "").strip()
            summary = (it.get("summary") or "").strip()
            if len(summary) > summary_cap:
                summary = summary[:summary_cap] + "…"
            lines.append("- %s:%s" % (title, summary) if summary else "- %s" % title)
            count += 1
        if count >= max_items:
            break
    return "\n".join(lines)


def generate_overview(daily, api_key, model):
    body = build_summary_input(daily)
    if not body.strip():
        return ""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": OVERVIEW_SYSTEM},
            {"role": "user", "content": OVERVIEW_USER_PREFIX + body},
        ],
        "temperature": 0.3,
        "stream": False,
    }
    try:
        data = http_post_json(
            DEEPSEEK_URL,
            payload,
            headers={"Authorization": "Bearer " + api_key},
            timeout=60,
        )
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        log("[warn] DeepSeek 速览生成失败,本次跳过速览: %s" % exc)
        return ""


# ---------------------------------------------------------------------------
# 渲染(标准 Markdown,Server酱 / PushPlus 通用)
# ---------------------------------------------------------------------------
def render_item(it):
    title = (it.get("title") or "").strip()
    summary = (it.get("summary") or "").strip()
    url = (it.get("sourceUrl") or "").strip()
    src = (it.get("sourceName") or "").strip()
    block = "**%s**" % title
    if summary:
        block += "\n> %s" % summary
    if url:
        block += "\n[%s](%s)" % (src or "查看原文", url)
    elif src:
        block += "\n*%s*" % src
    return block


def render_overview_block(overview_text, date):
    if not overview_text.strip():
        return None
    return "# 今日 AI 速览 · %s\n\n%s" % (date, overview_text.strip())


def render_report_md(daily, max_flashes=15):
    """返回原子块列表(切块时不可再拆的单位)。"""
    blocks = []
    date = daily.get("date") or beijing_today_str()

    title = "# AI 日报 · %s" % date
    if daily.get("synthesized"):
        title += "\n*(编辑版日报尚未生成,以下为最近 24 小时精选)*"
    blocks.append(title)

    lead = daily.get("lead")
    if lead and (lead.get("title") or lead.get("leadParagraph")):
        parts = []
        if (lead.get("title") or "").strip():
            parts.append("## %s" % lead["title"].strip())
        if (lead.get("leadParagraph") or "").strip():
            parts.append("> %s" % lead["leadParagraph"].strip())
        blocks.append("\n".join(parts))

    for sec in daily.get("sections", []):
        items = sec.get("items") or []
        if not items:
            continue
        blocks.append("## %s" % (sec.get("label") or ""))
        for it in items:
            blocks.append(render_item(it))

    flashes = daily.get("flashes") or []
    if flashes:
        lines = ["## 快讯"]
        for f in flashes[:max_flashes]:
            t = (f.get("title") or "").strip()
            url = (f.get("sourceUrl") or "").strip()
            meta = " · ".join(
                x for x in [(f.get("sourceName") or "").strip(), to_beijing(f.get("publishedAt"))] if x
            )
            line = "- [%s](%s)" % (t, url) if url else "- %s" % t
            if meta:
                line += " · %s" % meta
            lines.append(line)
        blocks.append("\n".join(lines))

    return blocks


# ---------------------------------------------------------------------------
# 切块
# ---------------------------------------------------------------------------
def utf8len(s):
    return len(s.encode("utf-8"))


def hard_split(text, soft):
    """把超长单块切成多片,每片 UTF-8 字节 <= soft;尽量在分隔符处切。"""
    pieces = []
    remaining = text
    seps = ["\n\n", "\n", "。", ". ", " "]
    while utf8len(remaining) > soft:
        # 二分找出 UTF-8 字节 <= soft 的最大字符前缀。
        lo, hi = 0, len(remaining)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if utf8len(remaining[:mid]) <= soft:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        # 回退到最近的分隔符,避免切在词/句中间。
        best = -1
        for sep in seps:
            idx = remaining.rfind(sep, 0, cut)
            if idx > best:
                best = idx + (0 if sep == " " else len(sep))
        if best <= 0:
            best = cut
        pieces.append(remaining[:best].rstrip())
        remaining = remaining[best:].lstrip()
    if remaining:
        pieces.append(remaining)
    return pieces


def chunk_blocks(blocks, soft=CHUNK_SOFT_LIMIT):
    chunks = []
    cur = ""
    for block in blocks:
        if not block:
            continue
        b = block.strip("\n")
        if not b:
            continue
        if utf8len(b) > soft:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(hard_split(b, soft))
            continue
        candidate = b if not cur else cur + "\n\n" + b
        if utf8len(candidate) <= soft:
            cur = candidate
        else:
            chunks.append(cur)
            cur = b
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------------------
# 推送(Server酱 / PushPlus)
# ---------------------------------------------------------------------------
def serverchan_endpoint(sendkey):
    # sctp 开头(Server酱³)走 ft07 域名,其余(SCT Turbo)走 ftqq。
    if sendkey.startswith("sctp"):
        m = re.match(r"^sctp(\d+)t", sendkey)
        if m:
            return "https://%s.push.ft07.com/send/%s.send" % (m.group(1), sendkey)
    return "https://sctapi.ftqq.com/%s.send" % sendkey


def push_serverchan(sendkey, title, desp):
    data = http_post_json(
        serverchan_endpoint(sendkey), {"title": title, "desp": desp}, timeout=20
    )
    if data.get("code") != 0:
        raise AppError(
            "Server酱推送失败 code=%s message=%s" % (data.get("code"), data.get("message"))
        )
    return True


def push_pushplus(token, title, content):
    payload = {"token": token, "title": title, "content": content, "template": "markdown"}
    data = http_post_json("https://www.pushplus.plus/send", payload, timeout=20)
    if data.get("code") != 200:
        raise AppError("PushPlus推送失败 code=%s msg=%s" % (data.get("code"), data.get("msg")))
    return True


def push_one(provider, secret, title, content):
    if provider == "serverchan":
        return push_serverchan(secret, title, content)
    return push_pushplus(secret, title, content)


def push_all(provider, secret, title, chunks):
    n = len(chunks)
    ok = 0
    for i, chunk in enumerate(chunks, 1):
        chunk_title = title if n == 1 else "%s (%d/%d)" % (title, i, n)
        try:
            push_one(provider, secret, chunk_title, chunk)
            ok += 1
            log("[%d/%d] 已推送 (%d 字节)" % (i, n, utf8len(chunk)))
        except AppError as exc:
            log("[%d/%d] 推送失败: %s" % (i, n, exc))
        if i < n:
            time.sleep(SEND_INTERVAL)
    return ok, n


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_chunks(daily, overview):
    blocks = []
    overview_block = render_overview_block(overview, daily.get("date") or beijing_today_str())
    if overview_block:
        blocks.append(overview_block)
    blocks.extend(render_report_md(daily))
    return chunk_blocks(blocks)


def main(argv):
    parser = argparse.ArgumentParser(description="每日 AI 资讯推送到个人微信(Server酱 / PushPlus)")
    parser.add_argument("--dry-run", action="store_true", help="只构建并打印,不推送")
    parser.add_argument("--no-summary", action="store_true", help="跳过 DeepSeek 速览")
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD(测试)")
    args = parser.parse_args(argv)

    # 1. 抓取日报(含兜底)
    try:
        daily = get_report(args.date)
    except AppError as exc:
        log("[error] 获取 AI 日报失败: %s" % exc)
        if exc.body:
            log("        响应: %s" % exc.body[:300])
        return 1

    # 2. DeepSeek 速览(best-effort)
    overview = ""
    if not args.no_summary:
        key = get_deepseek_key()
        if key:
            overview = generate_overview(daily, key, get_conf("DEEPSEEK_MODEL", DEFAULT_MODEL))
        else:
            log("[warn] 未找到 DEEPSEEK_API_KEY,跳过速览。")

    # 3. 渲染 + 切块
    chunks = build_chunks(daily, overview)
    if not chunks:
        log("[error] 没有可推送的内容。")
        return 1

    date = daily.get("date") or beijing_today_str()
    title = "%s · %s" % (PUSH_TITLE_PREFIX, date)

    # 4. dry-run 或推送
    if args.dry_run:
        provider, _ = detect_push_provider()
        log("=== DRY RUN:共 %d 条消息(不推送)===" % len(chunks))
        log("标题:%s" % title)
        log("推送渠道:%s" % (provider or "未配置(SERVERCHAN_SENDKEY / PUSHPLUS_TOKEN)"))
        for i, chunk in enumerate(chunks, 1):
            log("\n----- 第 %d/%d 条 (%d 字节) -----" % (i, len(chunks), utf8len(chunk)))
            log(chunk)
        return 0

    provider, secret = detect_push_provider()
    if not provider:
        log("[error] 未配置推送渠道(需要 SERVERCHAN_SENDKEY 或 PUSHPLUS_TOKEN)。")
        return 1

    ok, total = push_all(provider, secret, title, chunks)
    log("推送完成:%d/%d 成功(渠道 %s)。" % (ok, total, provider))
    return 0 if ok == total else 1


if __name__ == "__main__":
    # Windows 控制台默认 cp936,强制 UTF-8 避免中文/符号打印崩溃。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main(sys.argv[1:]))
