import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def split_words(value):
    return tuple(dict.fromkeys(x.strip() for x in re.split(r"[,，;；\r\n]+", value) if x.strip()))


class WakeWords:
    """Reload only names and keywords; credentials and other settings stay untouched."""

    def __init__(self, config, path):
        self.c, self.path = config, Path(path)
        self.stamp = None

    def refresh(self):
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self.stamp:
                return False
            values = dotenv_values(self.path, encoding="utf-8-sig", interpolate=False)
        except (OSError, UnicodeError):
            return False  # Keep working while the file is unavailable during a save.
        changed = False
        for key, attribute in (("BOT_NAMES", "bot_names"), ("AUTO_REPLY_KEYWORDS", "auto_keywords")):
            value = values.get(key)
            if isinstance(value, str):
                words = split_words(value)
                if words != getattr(self.c, attribute):
                    setattr(self.c, attribute, words)
                    changed = True
        self.stamp = stamp
        return changed


@dataclass
class Config:
    ws_url: str = "ws://127.0.0.1:3001"
    token: str = ""
    groups: frozenset[int] = frozenset()
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-flash"
    extra: dict = field(default_factory=dict)
    prompt: str = "你是QQ群里的聊天助手，用中文简明回答，不确定时如实说明。"
    prefix: str = "/ai"
    history_turns: int = 8
    ttl: float = 3600
    max_sessions: int = 500
    cooldown: float = 3
    concurrency: int = 3
    max_input: int = 4000
    max_reply: int = 2800
    max_tokens: int = 1000
    timeout: float = 90
    vision_enabled: bool = True
    max_images: int = 3
    max_image_bytes: int = 6 * 1024 * 1024
    image_timeout: float = 20
    auto_mode: str = "off"
    bot_names: tuple[str, ...] = ("机器人", "老哥")
    reply_on_name: bool = True
    auto_keywords: tuple[str, ...] = ("帮忙", "求助", "看图")
    auto_check_interval: float = 20
    auto_reply_cooldown: float = 60
    auto_checks_per_hour: int = 60
    auto_replies_per_hour: int = 12
    group_context_messages: int = 16
    group_context_ttl: float = 600
    search_enabled: bool = False
    search_max_tool_calls: int = 2
    search_max_calls_per_hour: int = 60
    search_timeout: float = 20
    memory_enabled: bool = False
    memory_max_facts: int = 24
    memory_inject_facts: int = 6
    memory_fact_days: int = 180
    memory_history_days: int = 7
    memory_extract_interval: float = 120
    memory_extracts_per_hour: int = 40
    rich_reply_enabled: bool = False
    reply_max_messages: int = 2
    reply_message_delay: float = 0.8
    media_max_assets: int = 100
    media_catalogue_limit: int = 40

    @classmethod
    def load(cls, require_llm=True):
        load_dotenv(ROOT / ".env")
        e = os.environ
        def flag(name, default):
            value = e.get(name, str(default)).strip().lower()
            if value not in {"true", "false", "1", "0"}:
                raise ValueError(name)
            return value in {"true", "1"}

        def words(name, default):
            return split_words(e.get(name, ",".join(default)))

        try:
            c = cls(
                ws_url=e.get("ONEBOT_WS_URL", cls.ws_url).strip(),
                token=e.get("ONEBOT_TOKEN", "").strip(),
                groups=frozenset(int(x.strip()) for x in e.get("ALLOWED_GROUPS", "").split(",") if x.strip()),
                base_url=e.get("LLM_BASE_URL", cls.base_url).strip().rstrip("/"),
                api_key=e.get("LLM_API_KEY", "").strip(),
                model=e.get("LLM_MODEL", cls.model).strip(),
                extra=json.loads(e.get("LLM_EXTRA_JSON", "{}")),
                prompt=e.get("SYSTEM_PROMPT", cls.prompt),
                prefix=e.get("TRIGGER_PREFIX", cls.prefix).strip(),
                history_turns=int(e.get("HISTORY_TURNS", "8")),
                ttl=float(e.get("SESSION_TTL_SECONDS", "3600")),
                max_sessions=int(e.get("MAX_SESSIONS", "500")),
                cooldown=float(e.get("COOLDOWN_SECONDS", "3")),
                concurrency=int(e.get("MAX_CONCURRENT", "3")),
                max_input=int(e.get("MAX_INPUT_CHARS", "4000")),
                max_reply=int(e.get("MAX_REPLY_CHARS", "2800")),
                max_tokens=int(e.get("MAX_TOKENS", "1000")),
                timeout=float(e.get("LLM_TIMEOUT_SECONDS", "90")),
                vision_enabled=flag("VISION_ENABLED", True),
                max_images=int(e.get("MAX_IMAGES", "3")),
                max_image_bytes=int(e.get("MAX_IMAGE_BYTES", str(6 * 1024 * 1024))),
                image_timeout=float(e.get("IMAGE_TIMEOUT_SECONDS", "20")),
                auto_mode=e.get("AUTO_REPLY_MODE", "off").strip().lower(),
                bot_names=words("BOT_NAMES", cls.bot_names),
                reply_on_name=flag("REPLY_ON_NAME", True),
                auto_keywords=words("AUTO_REPLY_KEYWORDS", cls.auto_keywords),
                auto_check_interval=float(e.get("AUTO_CHECK_INTERVAL_SECONDS", "20")),
                auto_reply_cooldown=float(e.get("AUTO_REPLY_COOLDOWN_SECONDS", "60")),
                auto_checks_per_hour=int(e.get("AUTO_MAX_CHECKS_PER_HOUR", "60")),
                auto_replies_per_hour=int(e.get("AUTO_MAX_REPLIES_PER_HOUR", "12")),
                group_context_messages=int(e.get("GROUP_CONTEXT_MESSAGES", "16")),
                group_context_ttl=float(e.get("GROUP_CONTEXT_TTL_SECONDS", "600")),
                search_enabled=flag("SEARCH_ENABLED", False),
                search_max_tool_calls=int(e.get("SEARCH_MAX_TOOL_CALLS", "2")),
                search_max_calls_per_hour=int(e.get("SEARCH_MAX_CALLS_PER_HOUR", "60")),
                search_timeout=float(e.get("SEARCH_TIMEOUT_SECONDS", "20")),
                memory_enabled=flag("MEMORY_ENABLED", False),
                memory_max_facts=int(e.get("MEMORY_MAX_FACTS_PER_USER", "24")),
                memory_inject_facts=int(e.get("MEMORY_INJECT_FACTS", "6")),
                memory_fact_days=int(e.get("MEMORY_FACT_DAYS", "180")),
                memory_history_days=int(e.get("MEMORY_HISTORY_DAYS", "7")),
                memory_extract_interval=float(e.get("MEMORY_EXTRACT_INTERVAL_SECONDS", "120")),
                memory_extracts_per_hour=int(e.get("MEMORY_MAX_EXTRACTS_PER_HOUR", "40")),
                rich_reply_enabled=flag("RICH_REPLY_ENABLED", True),
                reply_max_messages=int(e.get("REPLY_MAX_MESSAGES", "2")),
                reply_message_delay=float(e.get("REPLY_MESSAGE_DELAY_SECONDS", "0.8")),
                media_max_assets=int(e.get("MEDIA_MAX_ASSETS_PER_GROUP", "100")),
                media_catalogue_limit=int(e.get("MEDIA_CATALOGUE_LIMIT", "40")),
            )
        except (ValueError, TypeError):
            raise ValueError("配置格式错误：请检查群号、数字和 LLM_EXTRA_JSON。") from None
        if not c.token or "\n" in c.token or "\r" in c.token:
            raise ValueError("请配置 ONEBOT_TOKEN，与 NapCat 的 WebSocket Token 一致。")
        ws, api = urlsplit(c.ws_url), urlsplit(c.base_url)
        if ws.scheme not in {"ws", "wss"} or not ws.hostname or ws.username or ws.query or ws.fragment:
            raise ValueError("ONEBOT_WS_URL 应为 ws://地址:端口，Token 请单独填写。")
        if api.scheme not in {"http", "https"} or not api.hostname or api.username or api.query or api.fragment:
            raise ValueError("LLM_BASE_URL 必须是有效的 HTTP(S) 基础地址。")
        if api.path.endswith("/chat/completions"):
            raise ValueError("LLM_BASE_URL 请填写基础地址，不包含 /chat/completions。")
        if not c.prefix or not isinstance(c.extra, dict):
            raise ValueError("触发前缀不能为空，LLM_EXTRA_JSON 必须是 JSON 对象。")
        if {"messages", "model", "stream", "max_tokens"} & c.extra.keys():
            raise ValueError("LLM_EXTRA_JSON 不能覆盖 messages/model/stream/max_tokens。")
        if c.auto_mode not in {"off", "smart", "keywords"}:
            raise ValueError("AUTO_REPLY_MODE 只能是 off、smart 或 keywords。")
        if c.search_enabled and api.hostname != "api.deepseek.com":
            raise ValueError("SEARCH_ENABLED 当前仅支持 DeepSeek 官方 API 地址。")
        if c.search_max_tool_calls > 4:
            raise ValueError("单条消息的联网工具调用上限最多设置为 4 次。")
        if not 1 <= c.reply_max_messages <= 5 or not 0 <= c.reply_message_delay <= 5:
            raise ValueError("每轮回复条数须为1至5，消息间隔须为0至5秒。")
        if c.media_max_assets < 1 or not 1 <= c.media_catalogue_limit <= 100:
            raise ValueError("素材容量须为正数，提供给模型的素材目录最多100项。")
        for n in (c.ttl, c.max_sessions, c.concurrency, c.max_input, c.max_reply, c.max_tokens, c.timeout,
                  c.max_images, c.max_image_bytes, c.image_timeout, c.auto_check_interval,
                  c.auto_reply_cooldown, c.auto_checks_per_hour, c.auto_replies_per_hour,
                  c.group_context_messages, c.group_context_ttl, c.search_max_tool_calls,
                  c.search_max_calls_per_hour, c.search_timeout, c.memory_max_facts,
                  c.memory_inject_facts, c.memory_fact_days, c.memory_history_days,
                  c.memory_extract_interval, c.memory_extracts_per_hour):
            if not math.isfinite(n) or n <= 0:
                raise ValueError("容量、超时和长度配置必须为正数。")
        if c.history_turns < 0 or not math.isfinite(c.cooldown) or c.cooldown < 0:
            raise ValueError("记忆轮数和冷却时间不能为负数。")
        if any(g <= 0 for g in c.groups):
            raise ValueError("群号必须为正整数。")
        if require_llm and (not c.api_key or not c.model or not c.groups):
            raise ValueError("请先运行 configure.cmd，配置 API Key、模型和允许回复的群号。")
        return c
