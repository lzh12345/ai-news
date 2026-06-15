#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""翻译型中文 AI RSS —— 抓一批外文 AI RSS 源,DeepSeek 把非中文条目译成中文,
生成一个中文 RSS feed(docs/feed.xml),发布到 GitHub Pages 供订阅。

纯标准库(urllib + xml.etree + json),无第三方依赖。设计为在 GitHub Actions
(墙外 runner)上定时运行,抓取与翻译都在云端完成,本地无需架梯子。

用法:
    python rss_translate.py                # 抓取 + 翻译 + 写 docs/feed.xml + cache.json
    python rss_translate.py --dry-run      # 抓取 + 试译 3 条样本,打印不写文件(本地验证)
    python rss_translate.py --no-translate # 不翻译(原文聚合,排查抓取/解析用)
    python rss_translate.py --limit 40     # 限制 feed 条目数(默认 60)
    python rss_translate.py --feeds other.txt  # 用别的源清单

配置(优先级:环境变量 > config.json > 单值文件):
    DEEPSEEK_API_KEY  DeepSeek API key(翻译用;deepseek_key.txt 也可)
    DEEPSEEK_MODEL    DeepSeek 模型(默认 deepseek-chat)
    FEED_URL          feed 自链接(本地可不填;Actions 里自动按仓库推导)
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

DEFAULT_MAX_ITEMS = 60          # feed 最多保留多少条
MAX_TRANSLATE_PER_RUN = 90      # 单次运行最多新译多少条(成本/时间上限)
MAX_CACHE_ENTRIES = 1200        # 翻译缓存最多保留多少条(按插入序裁剪)
SUMMARY_CAP = 600               # 摘要送翻译前截断字符数

SCRIPT_DIR = Path(__file__).resolve().parent
FEEDS_FILE = SCRIPT_DIR / "feeds.txt"
CACHE_FILE = SCRIPT_DIR / "cache.json"
OUTPUT_FILE = SCRIPT_DIR / "docs" / "feed.xml"

TRANSLATE_SYSTEM = (
    "你是专业的科技/AI 资讯中英翻译。把用户给的标题和摘要准确、通顺地翻译成简体中文。\n"
    "- 保留专有名词、产品名、公司名、模型名(可中英并存,如 GPT-5、Claude)。\n"
    "- 不要增删信息、不要发表评论、不要寒暄。\n"
    "- 严格按下面格式输出,不要多余文字:\n"
    "标题:<中文标题>\n"
    "摘要:<中文摘要>"
)


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
class AppError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def log(msg):
    print(msg, flush=True)


_CONFIG_CACHE = None


def _read_text_file(name):
    try:
        return (SCRIPT_DIR / name).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def get_conf(name, default=""):
    """配置读取:环境变量 > config.json > 默认值。"""
    global _CONFIG_CACHE
    val = os.environ.get(name, "").strip()
    if val:
        return val
    if _CONFIG_CACHE is None:
        path = SCRIPT_DIR / "config.json"
        try:
            _CONFIG_CACHE = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception as exc:
            log("[warn] 读取 config.json 失败: %s" % exc)
            _CONFIG_CACHE = {}
    val = str(_CONFIG_CACHE.get(name, "") or "").strip()
    return val or default


def get_deepseek_key():
    return get_conf("DEEPSEEK_API_KEY") or _read_text_file("deepseek_key.txt")


def default_feed_url():
    # Actions 里 GITHUB_REPOSITORY = "owner/name",推导 Pages 地址。
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return "https://%s.github.io/%s/feed.xml" % (owner.lower(), name)
    return get_conf("FEED_URL", "https://your-name.github.io/ai-news/feed.xml")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_get_bytes(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", BROWSER_UA)
    req.add_header("Accept", "application/rss+xml, application/atom+xml, application/xml, text/xml, */*")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise AppError("HTTP %s GET %s" % (exc.code, url), status=exc.code)
    except urllib.error.URLError as exc:
        raise AppError("网络错误 GET %s: %s" % (url, exc.reason))
    except Exception as exc:
        raise AppError("GET %s 失败: %s" % (url, exc))


def deepseek_chat(messages, api_key, model, timeout=60):
    payload = {"model": model, "messages": messages, "temperature": 0.2, "stream": False}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(DEEPSEEK_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        return (obj["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        raise AppError("DeepSeek HTTP %s %s" % (exc.code, body))
    except Exception as exc:
        raise AppError("DeepSeek 调用失败: %s" % exc)


# ---------------------------------------------------------------------------
# 源清单 + RSS/Atom 解析
# ---------------------------------------------------------------------------
def load_feeds(path):
    feeds = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                feeds.append(line)
    except Exception as exc:
        log("[error] 读取源清单 %s 失败: %s" % (path, exc))
    return feeds


def _localname(tag):
    return tag.rsplit("}", 1)[-1]


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_entry(el, source_name):
    d = {"title": "", "link": "", "summary": "", "published": "", "guid": "", "source": source_name}
    for c in el:
        ln = _localname(c.tag)
        text = (c.text or "").strip()
        if ln == "title" and not d["title"]:
            d["title"] = strip_html(text)
        elif ln == "link":
            href = c.get("href")
            if href:
                rel = c.get("rel")
                if rel in (None, "alternate") or not d["link"]:
                    d["link"] = href
            elif text and not d["link"]:
                d["link"] = text
        elif ln in ("encoded", "description", "summary", "content") and not d["summary"]:
            d["summary"] = strip_html(text)
        elif ln in ("pubDate", "published", "updated", "date") and not d["published"]:
            d["published"] = text
        elif ln in ("guid", "id") and not d["guid"]:
            d["guid"] = text
    if not d["guid"]:
        d["guid"] = d["link"] or d["title"]
    return d if (d["title"] or d["summary"]) else None


def _sanitize_xml(data):
    """容错:删掉 XML 声明、非法控制字符,转义裸 &,返回 bytes 供重新解析。"""
    text = data.decode("utf-8", "ignore") if isinstance(data, (bytes, bytearray)) else data
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"&(?!#?\w+;)", "&amp;", text)
    return text.encode("utf-8")


def parse_feed(data, source_name):
    try:
        root = ET.fromstring(data)
    except Exception:
        # 真实世界不少 feed 有轻微 XML 瑕疵(裸 &、控制字符),清洗后重试。
        try:
            root = ET.fromstring(_sanitize_xml(data))
        except Exception as exc:
            raise AppError("XML 解析失败: %s" % exc)
    items = []
    for el in root.iter():
        if _localname(el.tag) in ("item", "entry"):
            parsed = _parse_entry(el, source_name)
            if parsed:
                items.append(parsed)
    return items


def source_name_from_url(url):
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def parse_date(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        return datetime.fromisoformat(t)
    except Exception:
        return None


def to_rfc822(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


# ---------------------------------------------------------------------------
# 语言判定 + 翻译
# ---------------------------------------------------------------------------
def is_chinese(text, threshold=0.2):
    """中日韩汉字占比 >= 阈值视为中文(无需翻译)。空文本视为中文(没东西可译)。"""
    if not text:
        return True
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    letters = sum(1 for ch in text if ch.isalpha())
    total = cjk + letters
    if total == 0:
        return True
    return (cjk / total) >= threshold


def translate_item(title, summary, api_key, model):
    """返回 (中文标题, 中文摘要);失败时回退原文。"""
    summary_in = (summary or "")[:SUMMARY_CAP]
    user = "标题:%s\n摘要:%s" % (title or "(无)", summary_in or "(无)")
    content = deepseek_chat(
        [{"role": "system", "content": TRANSLATE_SYSTEM},
         {"role": "user", "content": user}],
        api_key, model,
    )
    m_t = re.search(r"标题[:：]\s*(.+)", content)
    m_s = re.search(r"摘要[:：]\s*([\s\S]+)", content)
    title_zh = m_t.group(1).strip() if m_t else (title or "")
    summary_zh = m_s.group(1).strip() if m_s else (content.strip() if not m_t else (summary or ""))
    return title_zh or title, summary_zh or summary


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache):
    if len(cache) > MAX_CACHE_ENTRIES:
        # dict 保插入序,保留最后 MAX_CACHE_ENTRIES 条。
        keys = list(cache.keys())[-MAX_CACHE_ENTRIES:]
        cache = {k: cache[k] for k in keys}
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")


# ---------------------------------------------------------------------------
# RSS 生成
# ---------------------------------------------------------------------------
def xml_escape(s):
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def cdata(s):
    return "<![CDATA[%s]]>" % (s or "").replace("]]>", "]]&gt;")


def build_rss(items, feed_url, build_dt):
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title>AI 资讯 · 中文(翻译聚合)</title>",
        "<link>%s</link>" % xml_escape(feed_url),
        "<description>外文 AI 资讯经 DeepSeek 自动翻译成中文的聚合源</description>",
        "<language>zh-CN</language>",
        "<lastBuildDate>%s</lastBuildDate>" % to_rfc822(build_dt),
        '<atom:link href="%s" rel="self" type="application/rss+xml"/>' % xml_escape(feed_url),
    ]
    for it in items:
        parts.append("<item>")
        parts.append("<title>%s</title>" % xml_escape(it["title_zh"]))
        if it.get("link"):
            parts.append("<link>%s</link>" % xml_escape(it["link"]))
        parts.append('<guid isPermaLink="false">%s</guid>' % xml_escape(it["guid"]))
        if it.get("pubdate"):
            parts.append("<pubDate>%s</pubDate>" % it["pubdate"])
        parts.append("<source>%s</source>" % xml_escape(it["source"]))
        desc = "【%s】%s" % (it["source"], it["summary_zh"])
        if it.get("link"):
            desc += '<br/><br/><a href="%s">阅读原文</a>' % xml_escape(it["link"])
        parts.append("<description>%s</description>" % cdata(desc))
        parts.append("</item>")
    parts.append("</channel>")
    parts.append("</rss>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def collect_items(feeds):
    """抓取并解析所有源,返回 (items, 每源统计)。单源失败不影响其它源。"""
    all_items = []
    stats = []
    for url in feeds:
        name = source_name_from_url(url)
        try:
            data = http_get_bytes(url)
            items = parse_feed(data, name)
            all_items.extend(items)
            stats.append((url, len(items), ""))
        except AppError as exc:
            stats.append((url, 0, str(exc)))
    return all_items, stats


def dedupe_and_rank(items, limit):
    seen = set()
    unique = []
    for it in items:
        key = it.get("guid") or it.get("link") or it.get("title")
        if key in seen:
            continue
        seen.add(key)
        it["_dt"] = parse_date(it.get("published"))
        unique.append(it)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    unique.sort(key=lambda x: x["_dt"] or epoch, reverse=True)
    return unique[:limit]


def main(argv):
    parser = argparse.ArgumentParser(description="翻译型中文 AI RSS 生成器")
    parser.add_argument("--dry-run", action="store_true", help="抓取 + 试译 3 条样本,打印不写文件")
    parser.add_argument("--no-translate", action="store_true", help="不翻译(原文聚合)")
    parser.add_argument("--limit", type=int, default=DEFAULT_MAX_ITEMS, help="feed 条目上限")
    parser.add_argument("--feeds", default=str(FEEDS_FILE), help="源清单路径")
    args = parser.parse_args(argv)

    feeds = load_feeds(args.feeds)
    if not feeds:
        log("[error] 源清单为空。")
        return 1
    log("[info] 源清单 %d 个,开始抓取…" % len(feeds))

    items, stats = collect_items(feeds)
    for url, n, err in stats:
        log("  %s  %s" % (("✗ " + err) if err else ("✓ %d 条" % n), url))
    ok_feeds = sum(1 for _, _, e in stats if not e)
    log("[info] 抓取完成:%d/%d 源可用,共 %d 条(去重前)。" % (ok_feeds, len(feeds), len(items)))
    if not items:
        log("[error] 没有抓到任何条目。")
        return 1

    ranked = dedupe_and_rank(items, args.limit)
    log("[info] 去重排序后保留 %d 条。" % len(ranked))

    api_key = get_deepseek_key()
    model = get_conf("DEEPSEEK_MODEL", DEFAULT_MODEL)
    do_translate = not args.no_translate
    if do_translate and not api_key:
        log("[warn] 未找到 DEEPSEEK_API_KEY,本次不翻译(原文聚合)。")
        do_translate = False

    # ---- dry-run:只试译前 3 条非中文样本 ----
    if args.dry_run:
        log("\n=== DRY RUN(不写文件)===")
        samples = 0
        for it in ranked:
            need = do_translate and not is_chinese(it["title"] + " " + it["summary"])
            if not need:
                continue
            try:
                t_zh, s_zh = translate_item(it["title"], it["summary"], api_key, model)
            except AppError as exc:
                log("  [试译失败] %s" % exc)
                t_zh, s_zh = it["title"], it["summary"]
            log("\n----- 样本 %d (源 %s) -----" % (samples + 1, it["source"]))
            log("原标题: %s" % it["title"])
            log("译标题: %s" % t_zh)
            log("译摘要: %s" % (s_zh[:160] + ("…" if len(s_zh) > 160 else "")))
            log("链接  : %s" % it["link"])
            samples += 1
            if samples >= 3:
                break
        if samples == 0:
            log("(保留条目里没有需要翻译的非中文样本)")
        zh = sum(1 for it in ranked if is_chinese(it["title"] + " " + it["summary"]))
        log("\n保留 %d 条中:已是中文 %d 条,需翻译 %d 条。" % (len(ranked), zh, len(ranked) - zh))
        return 0

    # ---- 正式:翻译(带缓存)+ 生成 feed ----
    cache = load_cache()
    translated_now = 0
    for it in ranked:
        guid = it["guid"]
        if guid in cache:
            it["title_zh"] = cache[guid].get("t") or it["title"]
            it["summary_zh"] = cache[guid].get("s") or it["summary"]
        elif not do_translate or is_chinese(it["title"] + " " + it["summary"]):
            it["title_zh"], it["summary_zh"] = it["title"], it["summary"]
        elif translated_now >= MAX_TRANSLATE_PER_RUN:
            it["title_zh"], it["summary_zh"] = it["title"], it["summary"]
            log("[warn] 已达单次翻译上限 %d,其余保留原文。" % MAX_TRANSLATE_PER_RUN)
        else:
            try:
                t_zh, s_zh = translate_item(it["title"], it["summary"], api_key, model)
            except AppError as exc:
                log("[warn] 翻译失败(保留原文): %s" % exc)
                t_zh, s_zh = it["title"], it["summary"]
            it["title_zh"], it["summary_zh"] = t_zh, s_zh
            cache[guid] = {"t": t_zh, "s": s_zh}
            translated_now += 1
            time.sleep(0.3)
        it["pubdate"] = to_rfc822(it.get("_dt"))

    log("[info] 本次新翻译 %d 条。" % translated_now)

    build_dt = datetime.now(timezone.utc)
    feed_url = default_feed_url()
    xml = build_rss(ranked, feed_url, build_dt)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(xml, encoding="utf-8")
    save_cache(cache)
    log("[info] 已写出 %s(%d 条),feed 自链接 %s" % (OUTPUT_FILE, len(ranked), feed_url))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main(sys.argv[1:]))
