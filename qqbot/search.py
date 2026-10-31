"""Bounded calls to DeepSeek's read-only Search and Fetch APIs."""
import asyncio
import ipaddress
import logging
import time
from collections import deque
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)
TOOLS = [
    {"type": "function", "function": {
        "name": "web_search", "description": "查询实时信息、新闻、事实依据；普通闲聊不需要调用。",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "1至128字的检索关键词"}}, "required": ["query"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "fetch_page", "description": "读取一个公开网页的正文，用于群友提供的链接或搜索结果。",
        "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "公开网页的HTTP(S)链接"}}, "required": ["url"], "additionalProperties": False},
    }},
]


def public_url(value):
    try:
        p = urlsplit(value)
        host = p.hostname or ""
        if (p.scheme not in {"https", "http"} or not host or p.username or p.password
                or p.port not in {None, 80, 443} or len(value) > 8192
                or host.lower().rstrip(".") in {"localhost", "localhost.localdomain"}
                or host.lower().endswith((".local", ".localhost", ".internal"))):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except (ValueError, TypeError):
        return False


class Web:
    def __init__(self, config, client):
        self.c, self.client = config, client
        self.attempts = deque()

    async def call(self, name, arguments):
        if not self.c.search_enabled or urlsplit(self.c.base_url).hostname != "api.deepseek.com":
            return {"error": "当前未开启联网搜索。"}
        if not isinstance(arguments, dict):
            return {"error": "工具参数格式错误。"}
        if name == "web_search":
            query = arguments.get("query")
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
                return {"error": "检索词必须为1至128个字符。"}
            endpoint, payload = "search", {"query": query.strip(), "count": 3, "need_content": False}
        elif name == "fetch_page":
            url = arguments.get("url")
            if not isinstance(url, str) or not public_url(url):
                return {"error": "只能读取不带账号密码的公开HTTP(S)网页。"}
            endpoint, payload = "fetch", {"url": url}
        else:
            return {"error": "不支持的工具名称。"}
        now = time.monotonic()
        while self.attempts and now - self.attempts[0] >= 3600:
            self.attempts.popleft()
        if len(self.attempts) >= self.c.search_max_calls_per_hour:
            return {"error": "本进程当前小时的联网调用额度已用尽，请不要编造查询结果。"}
        self.attempts.append(now)
        try:
            async with asyncio.timeout(self.c.search_timeout):
                response = await self.client.post(
                    "https://api.deepseek.com/v1/tools/" + endpoint,
                    headers={"Authorization": "Bearer " + self.c.api_key},
                    json=payload, timeout=self.c.search_timeout,
                )
            if response.status_code != 200:
                log.warning("联网工具失败 tool=%s HTTP=%s", name, response.status_code)
                return {"error": f"联网工具请求失败（HTTP {response.status_code}），未获得可用查询结果。"}
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            if endpoint == "search":
                rows = body.get("results")
                if not isinstance(rows, list):
                    raise ValueError
                results = []
                for row in rows[:3]:
                    if isinstance(row, dict) and public_url(row.get("url")):
                        results.append({"title": str(row.get("title") or "")[:200], "url": row["url"],
                                        "summary": str(row.get("summary") or row.get("snippet") or "")[:1800],
                                        "date": str(row.get("date") or "")[:40]})
                log.info("联网搜索完成 results=%s", len(results))
                return {"results": results}
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            log.info("网页读取完成")
            return {"title": str(body.get("title") or "")[:200], "url": payload["url"], "content": content[:8000]}
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError):
            return {"error": "联网工具连接失败或未返回有效内容，请如实说明未能查询。"}
