import argparse
import asyncio
import base64
import getpass
import logging
from logging.handlers import RotatingFileHandler
import shutil
import sys
from pathlib import Path
from contextlib import AsyncExitStack

import httpx
from dotenv import dotenv_values, set_key

from .bot import Bot
from .config import Config, ROOT, WakeWords
from .llm import LLM, ModelError
from .images import Images, image_mime
from .onebot import OneBot
from .memory import Memory
from .media import Media

log = logging.getLogger("qqbot")


def configure():
    path = ROOT / ".env"
    if not path.exists():
        shutil.copyfile(ROOT / ".env.example", path)
    values = dotenv_values(path)
    fields = [
        ("ONEBOT_WS_URL", "NapCat 正向 WebSocket 地址", False),
        ("ONEBOT_TOKEN", "NapCat WebSocket Token（与 WebUI 登录口令不同）", True),
        ("ALLOWED_GROUPS", "允许回复的群号，多个用英文逗号分隔", False),
        ("LLM_BASE_URL", "大模型 API 基础地址", False),
        ("LLM_MODEL", "模型名称", False),
        ("LLM_API_KEY", "大模型 API Key", True),
        ("AUTO_REPLY_MODE", "自动接话模式（smart / keywords / off）", False),
        ("VISION_ENABLED", "是否识图（true / false，模型需支持图片）", False),
    ]
    print("配置保存在本机 .env；已有值直接回车保留。")
    for key, label, secret in fields:
        current = values.get(key) or ""
        hint = ("已设置" if current else "未设置") if secret else current
        prompt = f"{label} [{hint}]: "
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if value:
            set_key(str(path), key, value)
    Config.load()
    print("配置已保存。接下来运行 doctor.cmd 检查连接，再运行 start.cmd。")


def setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    handler = RotatingFileHandler(ROOT / "logs" / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(), handler])
    # Avoid upstream URLs, request bodies or provider responses in logs.
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


async def doctor(config, test_llm, image_path=None):
    print("检查 NapCat（只读取账号和群列表，不发送群消息）…")
    async with OneBot(config) as conn:
        info = await conn.call("get_login_info")
        if not info or not info.get("user_id"):
            raise ValueError("NapCat 未返回有效登录账号，请先登录 QQ。")
        print(f"QQ 登录成功：{info['user_id']}")
        groups = await conn.call("get_group_list")
        available = {int(g["group_id"]) for g in groups or []}
        print("当前 QQ 所在群号：" + (", ".join(map(str, sorted(available))) or "无"))
        missing = config.groups - available
        if missing:
            raise ValueError("机器人账号未加入这些白名单群：" + ", ".join(map(str, sorted(missing))))
        if not config.groups:
            print("尚未配置群白名单；启动前请运行 configure.cmd。")
    if test_llm:
        if not config.api_key or not config.model:
            raise ValueError("请先配置大模型 API Key 和模型名。")
        print("检查大模型（会产生一次实际 API 调用）…")
        images = []
        if image_path:
            if not config.vision_enabled:
                raise ValueError("识图已关闭，请开启 VISION_ENABLED。")
            path = Path(image_path)
            if not path.is_file() or path.stat().st_size > config.max_image_bytes:
                raise ValueError("测试图片不存在或超过 MAX_IMAGE_BYTES 限制。")
            data = path.read_bytes()
            images.append(f"data:{image_mime(data)};base64," + base64.b64encode(data).decode("ascii"))
        async with httpx.AsyncClient() as client:
            answer = await LLM(config, client).ask([], "请简短描述这张图片，用于验证识图连接。" if images else "请只回复：连接成功", images=images)
            print("大模型返回：" + answer[:200])
    print(f"功能配置：识图={'开' if config.vision_enabled else '关'}，联网={'开' if config.search_enabled else '关'}，自动接话={config.auto_mode}；"
          f"每群每小时最多检查{config.auto_checks_per_hour}次、自动回复{config.auto_replies_per_hour}次。")
    print("检查完成。")


async def run(config):
    async with httpx.AsyncClient() as client, httpx.AsyncClient(trust_env=False) as image_client, AsyncExitStack() as cleanup:
        llm = LLM(config, client)
        memory = Memory(config, llm) if config.memory_enabled else None
        if memory:
            cleanup.push_async_callback(memory.close)
        bot = Bot(config, llm, Images(config, image_client), memory=memory,
                  wake_words=WakeWords(config, ROOT / ".env"),
                  media=Media(config) if config.rich_reply_enabled else None)
        delay = 2
        while True:
            try:
                async with OneBot(config) as conn:
                    info = await conn.call("get_login_info")
                    if not info or not info.get("user_id"):
                        raise ValueError("未登录 QQ")
                    log.info("QQ %s 已连接，启用 %s 个群；@机器人 或 %s 问题。", info['user_id'], len(config.groups), config.prefix)
                    log.info("识图=%s，自动接话=%s；每群每小时检查上限=%s，自动回复上限=%s。",
                             config.vision_enabled, config.auto_mode, config.auto_checks_per_hour, config.auto_replies_per_hour)
                    log.info("名字唤醒=%s；唤醒名字=%s；名字和关键词修改后在下一条群消息时生效。",
                             config.reply_on_name, ", ".join(config.bot_names))
                    log.info("联网搜索=%s；每条消息工具调用上限=%s，本进程每小时联网调用上限=%s。",
                             config.search_enabled, config.search_max_tool_calls, config.search_max_calls_per_hour)
                    log.info("持久记忆=%s；每群每人最多%s条，近期对话保存%s天。",
                             config.memory_enabled, config.memory_max_facts, config.memory_history_days)
                    log.info("文字和图片回复均参考本群上下文；窗口=%s条，保留=%s秒。",
                             config.group_context_messages, config.group_context_ttl)
                    log.info("图文回复=%s；每轮最多%s条，条间隔%s秒；支持按指令收藏本群图片。",
                             config.rich_reply_enabled, config.reply_max_messages, config.reply_message_delay)
                    delay = 2
                    await bot.serve(conn)
            except Exception as exc:
                log.warning("连接不可用（%s），%s 秒后重试。检查 NapCat 是否启动、QQ 是否登录、地址和 Token 是否一致。", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="QQ群大模型聊天机器人")
    parser.add_argument("command", choices=["run", "configure", "doctor"], nargs="?", default="run")
    parser.add_argument("--llm", action="store_true", help="doctor 同时实际调用大模型（可能计费）")
    parser.add_argument("--image", metavar="PATH", help="doctor 将本地图片发给模型验证识图（隐含 --llm，可能计费）")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            configure()
        else:
            config = Config.load(require_llm=args.command == "run")
            setup_logging()
            asyncio.run(doctor(config, args.llm or bool(args.image), args.image) if args.command == "doctor" else run(config))
    except (ValueError, ModelError) as exc:
        print(f"提示：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已停止。")
    except Exception as exc:
        print(f"连接检查失败（{type(exc).__name__}）。请检查 NapCat 是否启动、QQ 登录、地址和 Token。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
