#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取公开热榜，输出 Markdown 或 JSON。

支持：
- 百度热搜（直连页面，解析 s-data）
- 微博热搜（60s 公共接口）
- 抖音热榜（60s 公共接口）
- 微信视频号 / 微信公众号（今日热榜 tophub，直连失败时走 Jina Reader）
- 小红书（默认不可用；可用 XHS_HOT_URL 指定自定义 JSON 接口，或尝试 RedFox 账号指数榜）

用法：
    python scripts/fetch_hot.py --limit 5
    python scripts/fetch_hot.py --platform weibo --limit 10 --json
    python scripts/fetch_hot.py --out 热点.md

注意：热榜是快照，平台随时变化；第三方接口可能失效，脚本会逐个报告失败原因。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TOPHUB_NODES = {
    "wechat-video": ("微信视频号 · 今日视频榜", "W1VdJPZoLQ"),
    "wechat-articles": ("微信公众号 · 24h热文榜", "WnBe01o371"),
}


def _request(url: str, timeout: float = 20, data: bytes | None = None,
             headers: dict | None = None) -> str:
    h = {
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/json,*/*",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _get_json(url: str, timeout: float = 20) -> dict:
    return json.loads(_request(url, timeout=timeout))


def fetch_baidu(limit: int, timeout: float) -> list[dict]:
    html = _request("https://top.baidu.com/board?tab=realtime", timeout=timeout)
    m = re.search(r"<!--s-data:(.*?)-->", html, re.S)
    if not m:
        raise RuntimeError("百度页面结构变化，未找到 s-data")
    data = json.loads(m.group(1))
    for card in data.get("data", {}).get("cards", []):
        content = card.get("content") or []
        items = []
        for x in content:
            title = x.get("word") or x.get("query")
            if not title:
                continue
            items.append({
                "title": title,
                "hot": x.get("hotScore", ""),
                "url": x.get("url") or x.get("rawUrl", ""),
            })
        if items:
            return items[:limit]
    return []


def _fetch_60s(kind: str, limit: int, timeout: float) -> list[dict]:
    data = _get_json(f"https://60s.viki.moe/v2/{kind}", timeout=timeout)
    items = []
    for x in (data.get("data") or [])[:limit]:
        items.append({
            "title": x.get("title", ""),
            "hot": x.get("hot_value") or x.get("hot") or "",
            "url": x.get("link") or x.get("url") or "",
        })
    return items


def fetch_weibo(limit: int, timeout: float) -> list[dict]:
    return _fetch_60s("weibo", limit, timeout)


def fetch_douyin(limit: int, timeout: float) -> list[dict]:
    return _fetch_60s("douyin", limit, timeout)


def _parse_tophub_markdown(md: str, limit: int) -> list[dict]:
    items = []
    for line in md.splitlines():
        m = re.match(r"^\s*(\d+)\.\[(.+?)\]\((.+?)\)(.*)$", line)
        if not m:
            continue
        title = m.group(2).strip()
        if not title:
            continue
        extra = m.group(4).strip()
        # 去掉聚合站附带的 markdown 链接/图标残留，只保留热度文本
        extra = re.sub(r"\[[^\]]*\]\([^)]*\)", "", extra)
        extra = re.sub(r"[_\[\]]+", " ", extra)
        extra = " ".join(extra.split())[:20]
        items.append({
            "title": title,
            "hot": extra,
            "url": m.group(3),
        })
        if len(items) >= limit:
            break
    return items


def fetch_tophub(kind: str, limit: int, timeout: float) -> list[dict]:
    _, node = TOPHUB_NODES[kind]
    url = f"https://tophub.today/n/{node}"
    try:
        html = _request(url, timeout=timeout)
        items = _parse_tophub_markdown(html, limit)
        if items:
            return items
        raise RuntimeError("直连页面未解析到榜单条目")
    except Exception as direct_err:
        # 直连 403 时走 Jina Reader；Jina 也可能限流
        try:
            md = _request("https://r.jina.ai/" + url, timeout=max(timeout, 40))
            items = _parse_tophub_markdown(md, limit)
            if items:
                return items
            raise RuntimeError("Jina 返回内容未解析到榜单条目")
        except Exception as jina_err:
            raise RuntimeError(f"直连失败({direct_err})；Jina 失败({jina_err})")


def fetch_xhs(limit: int, timeout: float) -> list[dict]:
    """小红书没有稳定公开热榜。

    优先读 XHS_HOT_URL 环境变量指向的自定义 JSON（格式同 [{title,hot,url}]）；
    否则尝试 RedFox 公开的“小红书指数榜”（账号榜，不是热词榜），失败则报错。
    """
    custom = os.environ.get("XHS_HOT_URL")
    if custom:
        data = _get_json(custom, timeout=timeout)
        rows = data.get("data") if isinstance(data, dict) else data
        out = []
        for x in (rows or [])[:limit]:
            out.append({
                "title": x.get("title") or x.get("accountName") or "",
                "hot": x.get("hot") or x.get("fansCount") or "",
                "url": x.get("url") or x.get("accountLink") or "",
            })
        return out

    today = datetime.now().strftime("%Y-%m-%d")
    payload = json.dumps({
        "dateType": 1,
        "rankDate": today,
        "type": "综合全部",
        "source": "小红书指数榜",
    }, ensure_ascii=False).encode("utf-8")
    raw = _request(
        "https://redfox.hk/story/xhsData/query",
        timeout=min(timeout, 12),
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    data = json.loads(raw)
    if data.get("code") != 2000:
        raise RuntimeError(f"RedFox 返回 code={data.get('code')}")
    out = []
    for x in (data.get("data") or [])[:limit]:
        out.append({
            "title": x.get("accountName", ""),
            "hot": "粉丝 " + str(x.get("fansCount", "")),
            "url": x.get("accountLink", ""),
        })
    return out


SOURCES = {
    "baidu": ("百度热搜", fetch_baidu),
    "weibo": ("微博热搜", fetch_weibo),
    "douyin": ("抖音热榜", fetch_douyin),
    "wechat-video": ("微信视频号 · 今日视频榜", lambda n, t: fetch_tophub("wechat-video", n, t)),
    "wechat-articles": ("微信公众号 · 24h热文榜", lambda n, t: fetch_tophub("wechat-articles", n, t)),
    "xiaohongshu": ("小红书热榜", fetch_xhs),
}


def gather(platforms: list[str], limit: int, timeout: float) -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=min(6, len(platforms))) as pool:
        futures = {
            pool.submit(SOURCES[p][1], limit, timeout): p for p in platforms
        }
        for fut in as_completed(futures):
            p = futures[fut]
            name = SOURCES[p][0]
            try:
                results[p] = {"name": name, "items": fut.result(), "error": None}
            except Exception as e:
                results[p] = {"name": name, "items": [], "error": str(e)}
    return results


def to_markdown(results: dict, limit: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 热点抓取｜{now}",
        "",
        "> 数据来自公开页面/公共接口，为抓取瞬间快照；各平台热度口径不同，不可跨平台比较。",
        "",
    ]
    order = ["baidu", "weibo", "douyin", "wechat-video", "wechat-articles", "xiaohongshu"]
    for p in order:
        if p not in results:
            continue
        r = results[p]
        lines.append(f"## {r['name']}")
        lines.append("")
        if r["error"]:
            lines.append(f"**未获取**：{r['error']}")
        else:
            for i, x in enumerate(r["items"][:limit], 1):
                hot = f" — {x['hot']}" if x.get("hot") else ""
                lines.append(f"{i}. {x['title']}{hot}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="抓取公开热榜")
    parser.add_argument("--platform", default="all",
                        help="all 或逗号分隔：baidu,weibo,douyin,wechat-video,wechat-articles,xiaohongshu")
    parser.add_argument("--limit", type=int, default=5, help="每个平台条数，默认 5")
    parser.add_argument("--timeout", type=float, default=20, help="单源超时秒数")
    parser.add_argument("--out", help="写入 Markdown 文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    all_keys = list(SOURCES.keys())
    if args.platform.strip().lower() == "all":
        platforms = all_keys
    else:
        platforms = [p.strip() for p in args.platform.split(",") if p.strip()]
        bad = [p for p in platforms if p not in SOURCES]
        if bad:
            parser.error("未知平台: " + ", ".join(bad))

    results = gather(platforms, args.limit, args.timeout)

    if args.json:
        payload = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "results": {
                p: {
                    "name": r["name"],
                    "items": r["items"],
                    "error": r["error"],
                }
                for p, r in results.items()
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = to_markdown(results, args.limit)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已写入 {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
