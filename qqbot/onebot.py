import asyncio
import json
import logging
import uuid

from websockets.asyncio.client import connect

from .config import Config

log = logging.getLogger(__name__)


class OneBotError(Exception):
    pass


class OneBot:
    """One receive loop routes both events and API responses by echo."""

    def __init__(self, config: Config):
        self.c = config
        self.ws = None
        self.pending = {}
        self.reader = None
        self.events = asyncio.Queue(maxsize=100)
        self.closed = asyncio.Event()

    async def __aenter__(self):
        self.ws = await connect(
            self.c.ws_url,
            additional_headers={"Authorization": "Bearer " + self.c.token},
            proxy=None,
            open_timeout=10,
            close_timeout=3,
            max_size=2**20,
        )
        self.reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *_):
        await self.ws.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)

    async def _read(self):
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                echo = data.get("echo")
                if isinstance(echo, str) and echo in self.pending:
                    f = self.pending[echo]
                    if not f.done():
                        f.set_result(data)
                elif data.get("post_type") == "message" and data.get("message_type") == "group":
                    try:
                        self.events.put_nowait(data)
                    except asyncio.QueueFull:
                        log.warning("消息队列已满，忽略新事件。")
        except Exception as exc:
            log.warning("OneBot 接收连接中断：%s", type(exc).__name__)
        finally:
            self.closed.set()
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(OneBotError("OneBot 连接已断开"))

    async def call(self, action, params=None, timeout=15):
        if self.closed.is_set():
            raise OneBotError("OneBot 连接已断开")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.ws.send(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            result = await asyncio.wait_for(future, timeout)
            if result.get("status") != "ok" or result.get("retcode") != 0:
                raise OneBotError(f"OneBot API {action} 调用失败")
            return result.get("data")
        except TimeoutError:
            raise OneBotError(f"OneBot API {action} 超时；发送结果可能不确定") from None
        finally:
            self.pending.pop(echo, None)
            if not future.done():
                future.cancel()

    async def reply(self, group: int, user: int, text: str):
        # Text segments keep model output literal, including any CQ-like text.
        return await self.call("send_group_msg", {
            "group_id": group,
            "message": [{"type": "text", "data": {"text": text}}],
        })

    async def reply_segments(self, group: int, user: int, segments):
        if not segments or any(x.get('type') not in {'text', 'face', 'image'} for x in segments):
            raise OneBotError('回复包含不支持的消息类型。')
        return await self.call('send_group_msg', {'group_id': group, 'message': segments})
