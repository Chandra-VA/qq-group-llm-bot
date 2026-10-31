import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

import httpx
from websockets.asyncio.server import serve

from qqbot.bot import Bot, Group
from qqbot.config import Config
from qqbot.images import Images
from qqbot.llm import DECISION_NAME, LLM, ModelError
from qqbot.media import Media
from qqbot.onebot import OneBot
from qqbot.replies import Bubble, Reply, reply_plan
from test_bot import Replies, event
from test_features import PNG, SmartModel, decision_response, photo


class RichReplies(Replies):
    async def reply_segments(self, group, user, segments):
        self.items.append((group, user, segments))
        return {'message_id': len(self.items)}


class RichTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = Config(groups=frozenset({100, 200}), cooldown=0, auto_mode='smart',
                        rich_reply_enabled=True, reply_message_delay=0, reply_max_messages=3)
        self.media = Media(self.c, Path(self.temp.name) / 'assets')

    async def test_collect_preserves_gif_bytes_and_scopes_to_one_group(self):
        gif = b'GIF89a' + b'ANIMATED TEST BYTES'

        class Pictures:
            async def load(self, refs, connection):
                return ['data:image/gif;base64,' + base64.b64encode(gif).decode()]

        await self.media.collect(100, '笑哭了', [{}], Pictures(), None)
        catalog = self.media.catalogue(100)
        self.assertEqual(catalog[0]['label'], '笑哭了')
        self.assertEqual(self.media.catalogue(200), [])
        self.assertNotIn('path', catalog[0])
        segments = self.media.segments(100, Bubble(image=catalog[0]['id']))
        self.assertEqual(base64.b64decode(segments[0]['data']['file'].removeprefix('base64://')), gif)
        self.assertEqual(segments[0]['data']['sub_type'], 1)
        await self.media.collect(100, '笑哭新版', [{}], Pictures(), None)
        self.assertEqual(len(self.media.catalogue(100)), 1)
        self.media.delete(100, '笑哭新版')
        self.assertEqual(self.media.catalogue(100), [])

    async def test_common_images_hotload_and_invalid_paths_cannot_be_sent(self):
        path = self.media.common / '无语.png'
        path.write_bytes(PNG)
        asset = self.media.catalogue(200)[0]
        parts = self.media.segments(100, Bubble(text='这个表情就很灵性', image=asset['id'], face='呲牙'))
        self.assertEqual([x['type'] for x in parts], ['text', 'face', 'image'])
        self.assertEqual(parts[1]['data']['id'], '13')
        for value in ['https://evil.example/x.png', 'file:///C:/private.txt', '../secret', 'saved_unknown']:
            with self.assertRaises(ValueError):
                reply_plan({'messages': [{'text': '', 'image': value, 'face': ''}]}, self.c, [asset])
        outside = Path(self.temp.name) / 'private.png'
        outside.write_bytes(PNG)
        self.media.write_index(100, [{'id': 'saved_'+'a'*20, 'file': '../private.png', 'label': 'bad'}])
        self.assertEqual(len(self.media.items(100)), 1)  # Only the safe common image.
        with self.assertRaises(ModelError):
            self.media.safe(outside)

    async def test_collect_command_and_quoted_image_without_model_calls(self):
        model, out = SmartModel(), RichReplies()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PNG))) as client:
            bot = Bot(self.c, model, Images(self.c, client), media=self.media)
            async def original(action, params, timeout):
                return photo('原图', mid=99)
            out.call = original
            e = event('/ai 收藏表情 绷不住了', mid=1)
            e['message'].insert(0, {'type': 'reply', 'data': {'id': '99'}})
            await bot.handle(e, out)
            self.assertIn('收好了', out.items[-1][2])
            await bot.handle(event('/ai 发图 绷不住了', mid=2), out)
            self.assertEqual(out.items[-1][2][0]['type'], 'image')
            await bot.handle(event('/ai 发表情 呲牙', mid=3), out)
            self.assertEqual(out.items[-1][2], [{'type': 'face', 'data': {'id': '13'}}])
            self.assertEqual(model.calls, [])
            self.assertEqual(model.decisions, [])

    async def test_multi_message_budget_ack_and_history_use_only_delivered_parts(self):
        c = replace(self.c, auto_replies_per_hour=2)
        model, out = SmartModel(), RichReplies()
        model.answer = Reply([Bubble('第一句'), Bubble(face='呲牙'), Bubble('这句超过配额')])
        bot = Bot(c, model, media=self.media)
        await bot.handle(event('有人知道怎么回事吗'), out)
        self.assertEqual(len(out.items), 2)
        self.assertEqual(len(bot.groups[100].replies), 2)
        history = bot.sessions[(100, 10)].history[-1]['content']
        self.assertIn('第一句', history)
        self.assertIn('QQ表情:呲牙', history)
        self.assertNotIn('超过配额', history)
        self.assertNotIn('base64', history)

    async def test_failed_second_bubble_is_counted_and_not_retried(self):
        model = SmartModel()
        model.answer = Reply([Bubble('第一句'), Bubble('第二句'), Bubble('第三句')])
        calls = []

        class Broken(RichReplies):
            async def reply(self, group, user, text):
                calls.append(text)
                if len(calls) == 2:
                    raise TimeoutError('ack lost')
                return {'message_id': 1}

        bot = Bot(self.c, model, media=self.media)
        await bot.handle(event('这个怎么解决呢'), Broken())
        self.assertEqual(calls, ['第一句', '第二句'])
        self.assertEqual(len(bot.groups[100].replies), 2)
        self.assertEqual(bot.sessions[(100, 10)].history, [])

    async def test_reply_guard_stops_later_bubbles(self):
        bot, out = Bot(self.c, SmartModel(), media=self.media), RichReplies()
        count = 0
        def guard():
            nonlocal count
            count += 1
            return count == 1
        result = await bot.send(out, Group(), 100, 10, 999,
                                Reply([Bubble('第一句'), Bubble('应该取消')]), guard=guard)
        self.assertEqual(str(result), '第一句')
        self.assertEqual(len(out.items), 1)

    async def test_rich_explicit_reply_can_search_then_return_two_bubbles(self):
        requests = []
        c = replace(self.c, search_enabled=True, search_max_tool_calls=1)
        def provider(request):
            if request.url.path.endswith('/tools/search'):
                return httpx.Response(200, json={'data': [{'title': '官方说明', 'url': 'https://example.com', 'snippet': '事实'}]})
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                self.assertEqual(body['tool_choice'], 'required')
                self.assertIn('messages', body['tools'][-1]['function']['parameters']['properties'])
                return httpx.Response(200, json={'choices': [{'message': {'content': None, 'tool_calls': [
                    {'id': 'web1', 'type': 'function', 'function': {'name': 'web_search', 'arguments': '{"query":"测试"}'}}]}}]})
            self.assertEqual(body['tool_choice'], {'type': 'function', 'function': {'name': DECISION_NAME}})
            return decision_response(json.dumps({'reply': True, 'content': '查到了', 'messages': [
                {'text': '查到了，给你看看', 'image': '', 'face': ''},
                {'text': '来源：https://example.com', 'image': '', 'face': '呲牙'}]}, ensure_ascii=False))
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            reply = await LLM(c, client).ask([], '搜一下测试', context={'recent': [], 'speaker': '测试群友', 'media': []})
        self.assertIsInstance(reply, Reply)
        self.assertEqual(len(reply.bubbles), 2)
        self.assertEqual(reply.bubbles[1].face, '呲牙')

    async def test_extra_web_call_after_budget_is_rejected_and_final_reply_is_recovered(self):
        c = replace(self.c, search_enabled=True, search_max_tool_calls=2)
        count = {'model': 0, 'web': 0}

        def provider(request):
            if request.url.path.endswith('/tools/search'):
                count['web'] += 1
                return httpx.Response(200, json={'results': [{'title': '已有结果', 'url': 'https://example.com', 'summary': '信息'}]})
            count['model'] += 1
            body = json.loads(request.content)
            if count['model'] <= 3:
                if count['model'] == 3:
                    self.assertEqual([x['function']['name'] for x in body['tools']], [DECISION_NAME])
                    self.assertEqual(body['tool_choice']['function']['name'], DECISION_NAME)
                return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {
                    'content': None, 'tool_calls': [{'id': f'call{count["model"]}', 'type': 'function',
                        'function': {'name': 'web_search', 'arguments': '{"query":"测试资料"}'}}]}}]})
            self.assertEqual(body['messages'][-1]['role'], 'tool')
            self.assertIn('未执行', body['messages'][-1]['content'])
            return decision_response('{"reply":true,"content":"根据已有结果给出答复","messages":[{"text":"根据已有结果给出答复","image":"","face":""}]}')

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            answer = await LLM(c, client).ask([], '搜一下测试资料', context={'recent': [], 'speaker': '测试群友', 'media': []})
        self.assertIn('已有结果', answer)
        self.assertEqual(count, {'model': 4, 'web': 2})

    async def test_repeated_tool_calls_after_budget_stop_without_exceeding_search_limit(self):
        c = replace(self.c, search_enabled=True, search_max_tool_calls=1)
        count = {'model': 0, 'web': 0}

        def provider(request):
            if request.url.path.endswith('/tools/search'):
                count['web'] += 1
                return httpx.Response(200, json={'results': []})
            count['model'] += 1
            return httpx.Response(200, json={'choices': [{'message': {'content': None, 'tool_calls': [
                {'id': f'call{count["model"]}', 'type': 'function', 'function': {'name': 'web_search', 'arguments': '{"query":"测试"}'}}]}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with self.assertRaisesRegex(ModelError, '联网已结束'):
                await LLM(c, client).ask([], '测试', context={'recent': [], 'speaker': '测试群友', 'media': []})
        self.assertEqual(count, {'model': 3, 'web': 1})

    async def test_websocket_delivers_three_native_messages_without_mentions(self):
        (self.media.common / '测试图.png').write_bytes(PNG)
        asset = self.media.catalogue(100)[0]
        ready = asyncio.Event()
        sent = []

        def provider(request):
            return decision_response(json.dumps({'reply': True, 'content': '测试完整图文回复', 'messages': [
                {'text': '第一条 [CQ:at,qq=all]', 'image': '', 'face': ''},
                {'text': '', 'image': '', 'face': '呲牙'},
                {'text': '', 'image': asset['id'], 'face': ''}]}))

        async def napcat(ws):
            await ws.send(json.dumps(event('/ai 分三条发给我看看')))
            async for raw in ws:
                call = json.loads(raw)
                self.assertEqual(call['action'], 'send_group_msg')
                sent.append(call['params']['message'])
                await ws.send(json.dumps({'status': 'ok', 'retcode': 0, 'data': {'message_id': len(sent)}, 'echo': call['echo']}))
                if len(sent) == 3:
                    ready.set()

        async with serve(napcat, '127.0.0.1', 0) as server:
            c = replace(self.c, token='test', ws_url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}')
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client, OneBot(c) as conn:
                bot = Bot(c, LLM(c, client), media=self.media)
                task = asyncio.create_task(bot.serve(conn))
                await asyncio.wait_for(ready.wait(), 3)
                await conn.ws.close()
                await asyncio.wait_for(task, 3)
        self.assertEqual([[part['type'] for part in message] for message in sent], [['text'], ['face'], ['image']])
        self.assertIn('[CQ:at,qq=all]', sent[0][0]['data']['text'])
        self.assertEqual(base64.b64decode(sent[2][0]['data']['file'].removeprefix('base64://')), PNG)

    def test_message_count_and_length_are_bounded(self):
        plan = reply_plan({'messages': [{'text': str(i)*10, 'image': '', 'face': ''} for i in range(8)]}, self.c)
        self.assertEqual(len(plan.bubbles), 3)
        self.assertLessEqual(sum(len(x.text) for x in plan.limited(12).bubbles), 12)
        with self.assertRaises(ValueError):
            reply_plan({'messages': [{'face': 'unknown'}]}, self.c)


if __name__ == '__main__':
    unittest.main()
