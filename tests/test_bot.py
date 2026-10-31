import asyncio
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx
from websockets.asyncio.server import serve

from qqbot.bot import Bot, parse_message
from qqbot.config import Config
from qqbot.llm import LLM, ModelError
from qqbot.onebot import OneBot, OneBotError


def event(text="/ai 你好", user=10, group=100, mid=1):
    return {"post_type": "message", "message_type": "group", "sub_type": "normal", "self_id": 999,
            "user_id": user, "group_id": group, "message_id": mid,
            "message": [{"type": "text", "data": {"text": text}}]}


class Replies:
    def __init__(self):
        self.items = []

    async def reply(self, group, user, text):
        self.items.append((group, user, text))


class Model:
    def __init__(self):
        self.calls = []
        self.contexts = []

    async def ask(self, history, text, images=(), *, context=None):
        self.calls.append((history, text))
        self.contexts.append(context)
        return "回答：" + text


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.c = Config(groups=frozenset({100, 200}), cooldown=0)
        self.model, self.out = Model(), Replies()
        self.bot = Bot(self.c, self.model)

    async def test_trigger_whitelist_self_and_private(self):
        for e in [event("普通群聊"), event("/aix 你好"), event(group=300), event(user=999),
                  dict(event(), message_type="private"), dict(event(), post_type="message_sent")]:
            await self.bot.handle(e, self.out)
        self.assertEqual(self.model.calls, [])
        e = event("问题", mid=2)
        e["message"].insert(0, {"type": "at", "data": {"qq": "999"}})
        await self.bot.handle(e, self.out)
        self.assertEqual(self.model.calls[0][1], "问题")

    async def test_context_isolation_and_reset(self):
        await self.bot.handle(event(mid=1), self.out)
        await self.bot.handle(event("/ai 继续", mid=2), self.out)
        self.assertEqual(len(self.model.calls[-1][0]), 2)
        await self.bot.handle(event(user=11, mid=3), self.out)
        self.assertEqual(self.model.calls[-1][0], [])
        await self.bot.handle(event(group=200, mid=4), self.out)
        self.assertEqual(self.model.calls[-1][0], [])
        await self.bot.handle(event("/ai reset", mid=5), self.out)
        await self.bot.handle(event(mid=6), self.out)
        self.assertEqual(self.model.calls[-1][0], [])

    async def test_dedup_across_connections(self):
        await self.bot.handle(event(), self.out)
        await self.bot.handle(event(), Replies())
        self.assertEqual(len(self.model.calls), 1)

    async def test_history_bound_ttl_and_session_limit(self):
        self.bot.c = replace(self.c, history_turns=1, max_sessions=2)
        await self.bot.handle(event(mid=1), self.out)
        await self.bot.handle(event(mid=2), self.out)
        self.assertEqual(len(self.bot.sessions[(100, 10)].history), 2)
        self.bot.sessions[(100, 10)].touched = time.monotonic() - 4000
        await self.bot.handle(event(mid=3), self.out)
        self.assertEqual(self.model.calls[-1][0], [])
        await self.bot.handle(event(user=11, mid=4), self.out)
        await self.bot.handle(event(user=12, mid=5), self.out)
        self.assertEqual(len(self.bot.sessions), 2)

    async def test_help_status_length_and_zero_memory(self):
        for mid, text in enumerate(["/ai", "/ai help", "/ai status", "/ai " + "长"*4001]):
            await self.bot.handle(event(text, mid=mid), self.out)
        self.assertEqual(len(self.out.items), 4)
        self.assertEqual(self.model.calls, [])
        self.bot.c = replace(self.c, history_turns=0)
        await self.bot.handle(event(mid=10), self.out)
        self.assertEqual(self.bot.sessions[(100, 10)].history, [])

    async def test_cooldown(self):
        self.bot.c = replace(self.c, cooldown=3)
        await self.bot.handle(event(mid=1), self.out)
        await self.bot.handle(event(mid=2), self.out)
        self.assertEqual(len(self.model.calls), 1)

    async def test_concurrency_drops_busy_requests(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(history, text, **kwargs):
            entered.set()
            await release.wait()
            return "完成"

        self.model.ask = slow
        self.bot.c = replace(self.c, concurrency=1)
        task = asyncio.create_task(self.bot.handle(event(), self.out))
        await entered.wait()
        await self.bot.handle(event(mid=2), self.out)
        await self.bot.handle(event(user=11, mid=3), self.out)
        self.assertEqual(len(self.bot.active), 1)
        release.set()
        await task
        self.assertEqual(len(self.out.items), 1)
        self.assertEqual(self.bot.active, set())

    async def test_failed_send_does_not_commit_history_or_retry(self):
        attempts = []

        async def fail(*args):
            attempts.append(args)
            raise OneBotError("发送结果不确定")

        self.out.reply = fail
        await self.bot.handle(event(), self.out)
        self.assertEqual(self.bot.sessions[(100, 10)].history, [])
        self.assertEqual(len(attempts), 1)

    async def test_model_error_does_not_commit_history(self):
        async def fail(*args, **kwargs):
            raise ModelError("余额不足")
        self.model.ask = fail
        await self.bot.handle(event(), self.out)
        self.assertEqual(self.out.items[-1][2], "余额不足")
        self.assertEqual(self.bot.sessions[(100, 10)].history, [])


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_websocket_http_and_literal_output(self):
        requests, replies = [], []
        delivered = asyncio.Event()

        def provider(request):
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer local-test-key")
            body = json.loads(request.content)
            current = json.loads(body["messages"][-1]["content"])
            self.assertEqual(current['current_message']['text'], '你好')
            requests.append(body)
            return httpx.Response(200, json={"choices": [{"message": {"content": "你好 [CQ:at,qq=all]"}}]})

        async def napcat(ws):
            self.assertEqual(ws.request.headers["Authorization"], "Bearer local-test-token")
            async for raw in ws:
                call = json.loads(raw)
                if call["action"] == "get_login_info":
                    await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"user_id": 999}, "echo": call["echo"]}))
                    await ws.send(json.dumps(event()))
                else:
                    self.assertEqual(call["action"], "send_group_msg")
                    replies.append(call["params"])
                    await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 2}, "echo": call["echo"]}))
                    delivered.set()

        async with serve(napcat, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            c = Config(ws_url=f"ws://127.0.0.1:{port}", token="local-test-token", groups=frozenset({100}),
                       base_url="https://model.test/v1", api_key="local-test-key")
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                async with OneBot(c) as conn:
                    self.assertEqual((await conn.call("get_login_info"))["user_id"], 999)
                    bot = Bot(c, LLM(c, client))
                    runner = asyncio.create_task(bot.serve(conn))
                    await asyncio.wait_for(delivered.wait(), 5)
                    # Wait for handling to finish, including acknowledgement and history.
                    for _ in range(100):
                        if not bot.active:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(len(bot.sessions[(100, 10)].history), 2)
                    await conn.ws.close()
                    await asyncio.wait_for(runner, 3)
        self.assertEqual(len(requests), 1)
        self.assertEqual(replies[0]["group_id"], 100)
        self.assertEqual([part["type"] for part in replies[0]["message"]], ["text"])
        self.assertEqual(replies[0]["message"][0]["data"]["text"], "你好 [CQ:at,qq=all]")

    async def test_parallel_api_responses_out_of_order_and_disconnect(self):
        async def napcat(ws):
            first = json.loads(await ws.recv())
            second = json.loads(await ws.recv())
            for call in [second, first]:
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": call["action"], "echo": call["echo"]}))
            await ws.recv()
            await ws.close()

        async with serve(napcat, "127.0.0.1", 0) as server:
            c = Config(ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            async with OneBot(c) as conn:
                self.assertEqual(await asyncio.gather(conn.call("first"), conn.call("second")), ["first", "second"])
                with self.assertRaises(OneBotError):
                    await conn.call("third")
                self.assertEqual(conn.pending, {})

    async def test_api_errors_are_sanitized_and_not_retried(self):
        for status in (401, 429, 500):
            requests = []
            def provider(request):
                requests.append(request)
                return httpx.Response(status, text="SECRET API_KEY and private request body")
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                with self.assertRaises(ModelError) as caught:
                    await LLM(Config(), client).ask([], "你好")
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(len(requests), 1)

    async def test_malformed_model_response(self):
        for body in [{}, {"choices": []}, {"choices": [{"message": {"content": None}}]}]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
                with self.assertRaises(ModelError):
                    await LLM(Config(), client).ask([], "你好")

    async def test_total_model_timeout(self):
        async def provider(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with self.assertRaisesRegex(ModelError, "超时"):
                await LLM(Config(timeout=0.01), client).ask([], "你好")

    async def test_failed_ack_and_timeout_cleanup(self):
        async def napcat(ws):
            call = json.loads(await ws.recv())
            await ws.send(json.dumps({"status": "failed", "retcode": 100, "echo": call["echo"], "message": "SECRET"}))
            await ws.recv()
            await ws.wait_closed()
        async with serve(napcat, "127.0.0.1", 0) as server:
            c = Config(ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            async with OneBot(c) as conn:
                with self.assertRaises(OneBotError) as caught:
                    await conn.call("send_group_msg")
                self.assertNotIn("SECRET", str(caught.exception))
                with self.assertRaisesRegex(OneBotError, "超时"):
                    await conn.call("send_group_msg", timeout=0.01)
                self.assertEqual(conn.pending, {})


class ParsingAndConfigTests(unittest.TestCase):
    def test_string_and_array_cq_parsing(self):
        e = event()
        e["message"] = "[CQ:reply,id=1][CQ:at,qq=999] A &amp; B &#91;CQ:at,qq=all&#93;"
        self.assertEqual(parse_message(e), ("A & B [CQ:at,qq=all]", True))
        e["message"] = "[CQ:at,qq=9999] 你好"
        self.assertEqual(parse_message(e), ("@群友(QQ:9999)  你好", False))

    def test_required_config_and_extra_overrides(self):
        env = {"ONEBOT_TOKEN": "test", "ALLOWED_GROUPS": "100,200", "LLM_API_KEY": "test"}
        with patch("qqbot.config.load_dotenv"), patch.dict("os.environ", env, clear=True):
            self.assertEqual(Config.load().groups, frozenset({100, 200}))
        for extra in [{"ALLOWED_GROUPS": ""}, {"LLM_EXTRA_JSON": '{"messages":[]}'}, {"MAX_CONCURRENT": "0"}, {"SESSION_TTL_SECONDS": "nan"}]:
            with patch("qqbot.config.load_dotenv"), patch.dict("os.environ", {**env, **extra}, clear=True):
                with self.assertRaises(ValueError):
                    Config.load()


if __name__ == "__main__":
    unittest.main()
