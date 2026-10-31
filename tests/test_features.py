import asyncio
import base64
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from websockets.asyncio.server import serve

from qqbot.bot import Bot, calls_by_name
from qqbot.config import Config, WakeWords
from qqbot.images import Images, allowed_url
from qqbot.llm import DECISION_NAME, LLM, ModelError
from qqbot.messages import parse
from qqbot.onebot import OneBot
from test_bot import Model, Replies, event

# A small PNG fixture; no real QQ photos or paid APIs are used.
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3foAAAAASUVORK5CYII=')
IMAGE_URL = 'https://gchat.qpic.cn/test-image'


def decision_response(arguments):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {
        'content': None, 'tool_calls': [{'id': 'decision1', 'type': 'function',
            'function': {'name': DECISION_NAME, 'arguments': arguments}}]}}]})


def photo(text='/ai 这是什么', mid=1):
    e = event(text, mid=mid)
    e['message'].append({'type': 'image', 'data': {'file': 'a.image', 'url': IMAGE_URL}})
    return e


class SmartModel(Model):
    def __init__(self, answer=None):
        super().__init__()
        self.answer = answer
        self.decisions = []
        self.pictures = []

    async def ask(self, history, text, images=(), *, context=None):
        self.pictures.append(list(images))
        return await super().ask(history, text, images=images, context=context)

    async def consider(self, history, text, context, images=()):
        self.decisions.append((history, text, context, list(images)))
        return self.answer


class AutomaticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.c = Config(groups=frozenset({100, 200}), cooldown=0, auto_mode='smart')
        self.model, self.out = SmartModel(), Replies()
        self.bot = Bot(self.c, self.model)

    async def test_silent_decision_and_unaddressed_commands(self):
        await self.bot.handle(event('/ai 保留这一轮', mid=1), self.out)
        self.bot.groups[100].last_reply -= 120
        await self.bot.handle(event('reset', mid=2), self.out)
        self.assertEqual(len(self.model.decisions), 1)
        self.assertEqual(len(self.out.items), 1)
        self.assertEqual(len(self.bot.sessions[(100, 10)].history), 2)
        self.assertFalse(self.bot.groups[100].busy)

    async def test_check_interval_reply_cooldown_budget_and_explicit_bypass(self):
        self.bot.c = replace(self.c, auto_replies_per_hour=1)
        await self.bot.handle(event('有人知道怎么解决吗', mid=1), self.out)
        await self.bot.handle(event('有人知道怎么解决吗', mid=2), self.out)
        self.assertEqual(len(self.model.decisions), 1)
        self.bot.groups[100].last_check -= 30
        self.model.answer = '可以，先看报错。'
        await self.bot.handle(event('继续说这个问题', mid=3), self.out)
        self.assertEqual(len(self.out.items), 1)
        self.bot.groups[100].last_check -= 30
        await self.bot.handle(event('还想问个问题', mid=4), self.out)
        self.assertEqual(len(self.model.decisions), 2)
        self.bot.groups[100].last_reply -= 120
        await self.bot.handle(event('再来个问题', mid=5), self.out)
        self.assertEqual(len(self.model.decisions), 2)  # Hourly reply cap.
        await self.bot.handle(event('/ai 明确提问', mid=6), self.out)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.out.items), 2)

    async def test_hourly_check_budget_expires(self):
        self.bot.c = replace(self.c, auto_checks_per_hour=1)
        await self.bot.handle(event('这个怎么回事', mid=1), self.out)
        state = self.bot.groups[100]
        state.last_check -= 60
        await self.bot.handle(event('这又怎么回事', mid=2), self.out)
        self.assertEqual(len(self.model.decisions), 1)
        state.checks[0] -= 3601
        await self.bot.handle(event('这次呢怎么回事', mid=3), self.out)
        self.assertEqual(len(self.model.decisions), 2)

    async def test_uncertain_automatic_send_still_consumes_reply_budget(self):
        self.bot.c = replace(self.c, auto_replies_per_hour=1)
        self.model.answer = '试着回复'
        attempts = []
        async def uncertain(*args):
            attempts.append(args)
            raise TimeoutError('ack lost')
        self.out.reply = uncertain
        await self.bot.handle(event('这怎么解决', mid=1), self.out)
        state = self.bot.groups[100]
        state.last_check -= 120
        state.last_reply -= 120
        await self.bot.handle(event('还有个新问题', mid=2), self.out)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(state.replies), 1)
        self.assertEqual(self.bot.sessions[(100, 10)].history, [])

    async def test_other_mentions_replies_and_spam_are_skipped(self):
        e = event('你怎么看', mid=1)
        e['message'].insert(0, {'type': 'at', 'data': {'qq': '111'}})
        other_reply = event('回答一下', mid=2)
        other_reply['message'].insert(0, {'type': 'reply', 'data': {'id': '9999'}})
        for item in [e, other_reply, event('哈哈哈哈', mid=3), event('？！？', mid=4),
                     event('无关群提问', group=999, mid=5), event('自己说话', user=999, mid=6)]:
            await self.bot.handle(item, self.out)
        self.assertEqual(self.model.decisions, [])
        self.bot.groups[100].bot_message_ids.append('123')
        own_reply = event('还有详细说明吗', mid=7)
        own_reply['message'].insert(0, {'type': 'reply', 'data': {'id': '123'}})
        async def get_original(*args, **kwargs):
            return {**event('先前的回答'), 'user_id': 999}
        self.out.call = get_original
        await self.bot.handle(own_reply, self.out)
        self.assertEqual(len(self.model.decisions), 1)

    async def test_context_group_isolation_ttl_bound_and_names(self):
        await self.bot.handle(event('第一个群的话题', user=11, group=200, mid=1), self.out)
        await self.bot.handle(event('当前群前文', user=11, mid=2), self.out)
        state = self.bot.groups[100]
        state.last_check -= 30
        e = event('当前群的问题', mid=3)
        e['sender'] = {'card': '小明', 'nickname': '别名'}
        await self.bot.handle(e, self.out)
        context = self.model.decisions[-1][2]
        self.assertEqual(context['speaker'], '小明(QQ:10)')
        self.assertEqual([x['text'] for x in context['recent']], ['当前群前文'])
        self.bot.c = replace(self.c, group_context_messages=2)
        state.lines[0]['time'] = time.monotonic() - 600
        state.last_check -= 30
        await self.bot.handle(event('新一句话', mid=4), self.out)
        self.assertEqual(len(state.lines), 2)
        self.assertNotIn('当前群前文', str(self.model.decisions[-1][2]))

    async def test_reset_removes_user_from_shared_context(self):
        await self.bot.handle(event('别人的话', user=11, mid=1), self.out)
        await self.bot.handle(event('/ai 我的对话', mid=2), self.out)
        await self.bot.handle(event('/ai reset', mid=3), self.out)
        lines = self.bot.groups[100].lines
        self.assertFalse(any('我的对话' in x['text'] for x in lines))
        self.assertTrue(any('别人的话' in x['text'] for x in lines))

    async def test_only_one_auto_decision_per_group_in_flight(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return None
        self.model.consider = slow
        task = asyncio.create_task(self.bot.handle(event('谁来帮忙', mid=1), self.out))
        await entered.wait()
        await self.bot.handle(event('另一个问题', user=11, mid=2), self.out)
        self.assertEqual(len(self.bot.active), 1)
        release.set()
        await task
        self.assertFalse(self.bot.groups[100].busy)

    async def test_keywords_mode_does_not_judge_unrelated_chat(self):
        self.bot.c = replace(self.c, auto_mode='keywords', bot_names=('阿暴',), auto_keywords=())
        await self.bot.handle(event('今天天气真好', mid=1), self.out)
        await self.bot.handle(event('阿暴介绍一下自己', mid=2), self.out)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.decisions, [])

    async def test_direct_name_call_bypasses_auto_cooldown_and_budgets(self):
        self.bot.c = replace(self.c, auto_checks_per_hour=1, auto_replies_per_hour=1)
        self.model.answer = '前一次自动回复'
        await self.bot.handle(event('一个普通问题', user=11, mid=1), self.out)
        await self.bot.handle(event('机器人出来', mid=2), self.out)
        self.assertEqual(len(self.model.decisions), 1)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.calls[0][1], '机器人出来')
        self.assertEqual(len(self.out.items), 2)
        self.assertEqual(len(self.bot.groups[100].checks), 1)
        self.assertEqual(len(self.bot.groups[100].replies), 1)

    async def test_ascii_name_with_chinese_question_bypasses_auto_decision(self):
        self.bot.c = replace(self.c, bot_names=('大肥鱼', 'ds', 'deepseek'))
        await self.bot.handle(event('一个普通问题', user=11, mid=1), self.out)
        await self.bot.handle(event('ds认识我吗', mid=2), self.out)
        self.assertEqual(len(self.model.decisions), 1)
        self.assertEqual([call[1] for call in self.model.calls], ['ds认识我吗'])
        self.assertEqual(len(self.out.items), 1)

    async def test_wake_words_reload_on_messages_without_reloading_credentials(self):
        c = replace(self.c, api_key='keep-original-key')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('BOT_NAMES=大肥鱼,ds，deepseek\nAUTO_REPLY_KEYWORDS=求助；看图\nLLM_API_KEY=do-not-load\n', encoding='utf-8-sig')
            bot = Bot(c, self.model, wake_words=WakeWords(c, path))
            await bot.handle(event('ds认识我吗', mid=1), self.out)
            self.assertEqual(c.bot_names, ('大肥鱼', 'ds', 'deepseek'))
            self.assertEqual(c.auto_keywords, ('求助', '看图'))
            self.assertEqual(c.api_key, 'keep-original-key')
            self.assertFalse(bot.wake_words.refresh())
            path.write_text('BOT_NAMES=newbot\nAUTO_REPLY_KEYWORDS=求助\n', encoding='utf-8')
            await bot.handle(event('ds认识我吗', mid=2), self.out)
            self.assertEqual(len(self.model.calls), 1)  # Removed alias; auto cooldown applies.
            await bot.handle(event('newbot认识我吗', mid=3), self.out)
            self.assertEqual(len(self.model.calls), 2)
            self.assertEqual(c.bot_names, ('newbot',))
            path.unlink()
            await bot.handle(event('newbot出来', mid=4), self.out)
            self.assertEqual(len(self.model.calls), 3)  # Save/rename cannot erase working names.

    async def test_name_call_switch_and_off_mode(self):
        self.bot.c = replace(self.c, reply_on_name=False)
        await self.bot.handle(event('机器人出来', mid=1), self.out)
        self.assertEqual(len(self.model.decisions), 1)
        self.assertEqual(len(self.model.calls), 0)
        bot = Bot(replace(self.c, auto_mode='off'), self.model)
        await bot.handle(event('机器人出来', mid=2), self.out)
        self.assertEqual(len(self.model.calls), 0)
        self.assertEqual(len(self.model.decisions), 1)

    async def test_name_call_still_respects_whitelist_self_and_user_cooldown(self):
        self.bot.c = replace(self.c, cooldown=3)
        await self.bot.handle(event('机器人出来', group=999, mid=1), self.out)
        await self.bot.handle(event('机器人出来', user=999, mid=2), self.out)
        await self.bot.handle(event('机器人出来', mid=3), self.out)
        await self.bot.handle(event('机器人出来', mid=4), self.out)
        self.assertEqual(len(self.model.calls), 1)


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_only_explicit_message_uses_model_not_help(self):
        c = Config(groups=frozenset({100}), cooldown=0)
        model, out = SmartModel(), Replies()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PNG))) as client:
            bot = Bot(c, model, Images(c, client))
            e = photo('')
            e['message'].insert(0, {'type': 'at', 'data': {'qq': '999'}})
            await bot.handle(e, out)
            self.assertTrue(model.pictures[0][0].startswith('data:image/png;base64,'))
            saved = str(bot.sessions[(100, 10)].history)
            self.assertNotIn('base64', saved)
            self.assertIn('原图仅在本轮可见', saved)

    async def test_smart_picture_and_failure_are_silent(self):
        c = Config(groups=frozenset({100}), cooldown=0, auto_mode='smart')
        model, out = SmartModel(), Replies()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PNG))) as client:
            bot = Bot(c, model, Images(c, client))
            await bot.handle(photo('这张图什么意思'), out)
            self.assertEqual(len(model.decisions[0][3]), 1)
            self.assertEqual(out.items, [])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            bot = Bot(c, model, Images(c, client))
            await bot.handle(photo('图片里的问题'), out)
            self.assertEqual(out.items, [])
            self.assertFalse(bot.groups[100].busy)

    async def test_size_format_disabled_and_count(self):
        c = Config(max_image_bytes=10)
        for response in [httpx.Response(200, content=PNG), httpx.Response(200, content=b'<html>not an image</html>')]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
                with self.assertRaises(ModelError):
                    await Images(c, client).load([{'url': IMAGE_URL}], None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PNG))) as client:
            for config, refs in [(Config(vision_enabled=False), [{'url': IMAGE_URL}]),
                                 (Config(max_images=1), [{'url': IMAGE_URL}]*2)]:
                with self.assertRaises(ModelError):
                    await Images(config, client).load(refs, None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'bad'))) as client:
            with self.assertRaisesRegex(ModelError, '格式'):
                await Images(Config(), client).load([{'url': IMAGE_URL}], None)

    async def test_download_headers_do_not_leak_api_key_and_redirects_are_checked(self):
        calls = []
        def server(request):
            calls.append(str(request.url))
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            with self.assertRaises(ModelError):
                await Images(Config(), client).load([{'url': IMAGE_URL}], None)
        self.assertEqual(len(calls), 1)
        for url in ['file:///C:/secret.txt', 'http://127.0.0.1/a', 'https://qq.com.evil.example/a',
                    'https://user:password@gchat.qpic.cn/a', 'https://gchat.qpic.cn:8000/a']:
            self.assertFalse(allowed_url(url))
        self.assertTrue(allowed_url('https://multimedia.nt.qq.com.cn/download?appid=1'))

    async def test_missing_url_uses_get_image_without_opening_files(self):
        calls = []
        class Connection:
            async def call(self, action, params):
                calls.append((action, params))
                return {'url': IMAGE_URL, 'file': 'C:/private/never-opened.png'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PNG))) as client:
            result = await Images(Config(), client).load([{'file': 'hash.image'}], Connection())
            self.assertTrue(result[0].startswith('data:image/png'))
            self.assertEqual(calls[0][0], 'get_image')
            with self.assertRaises(ModelError):
                await Images(Config(), client).load([{'file': 'C:/secret.png'}], Connection())


class MentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_member_nickname_and_id_reach_the_model(self):
        c = Config(groups=frozenset({100}), cooldown=0)
        model = SmartModel()
        class Connection(Replies):
            async def call(self, action, params, timeout):
                self.action = action
                return {'nickname': '昵称', 'card': '张三'}
        connection = Connection()
        bot = Bot(c, model)
        e = event('/ai 评价一下 ')
        e['message'].append({'type': 'at', 'data': {'qq': '12345'}})
        await bot.handle(e, connection)
        self.assertIn('@张三(QQ:12345)', model.calls[0][1])
        self.assertEqual(connection.action, 'get_group_member_info')

    async def test_cq_array_images_and_literal_cq_are_consistent(self):
        e = event()
        e['message'] = '[CQ:at,qq=999]看[CQ:image,file=a.image,url=https://gchat.qpic.cn/a?x=1&amp;y=2][CQ:at,qq=123,name=张三]'
        parsed = parse(e)
        self.assertTrue(parsed.mentioned)
        self.assertEqual(parsed.images[0]['url'], 'https://gchat.qpic.cn/a?x=1&y=2')
        self.assertIn('@张三(QQ:123)', parsed.render())
        e['message'] = '&#91;CQ:at,qq=999&#93;'
        self.assertFalse(parse(e).mentioned)


class FeatureProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_ascii_alias_word_boundaries_and_chinese_followups(self):
        names = ('ds', 'deepseek')
        for text in ['ds', 'DS', 'ds认识我吗', 'ds今天有什么新闻', 'deepseek认识我吗', 'ds，出来', '在吗，DS']:
            self.assertTrue(calls_by_name(text, names), text)
        for text in ['DSLR相机', 'ds2模型', 'ds_model', 'deepseeker', '我在研究ds模型']:
            self.assertFalse(calls_by_name(text, names), text)

    def test_name_call_matching_avoids_casual_mentions(self):
        names = ('机器人', '老哥', 'Bob')
        for text in ['机器人出来', '老哥在吗', '机器人，帮我一下', '在吗，老哥', 'Bob hello', '机器人']:
            self.assertTrue(calls_by_name(text, names), text)
        for text in ['我写了一个机器人', '机器人领域发展很快', 'Bobby is here', '这台机器人坏了']:
            self.assertFalse(calls_by_name(text, names), text)

    async def test_multimodal_json_decisions_strict_schema_and_no_retry(self):
        for answer, expected in [(' {"reply":false} ', None), ('{"reply":true,"content":"有意思"}', '有意思'),
                                 ('```json\n{"reply":false}\n```', None),
                                 ('```json\n{"reply":true,"content":"接上话了"}\n```', '接上话了'),
                                 ('{"reply":"false"}', 'ERROR'), ('plain text', 'ERROR'),
                                 ('先解释一下\n{"reply":true,"content":"不应发出去"}', 'ERROR'), ('', 'ERROR')]:
            requests = []
            def provider(request):
                body = json.loads(request.content)
                requests.append(body)
                self.assertEqual(body['messages'][-1]['content'][1]['type'], 'image_url')
                self.assertEqual(body['thinking']['type'], 'disabled')
                self.assertNotIn('response_format', body)  # Avoid DeepSeek's empty JSON-mode responses.
                self.assertEqual(body['tool_choice'], {'type': 'function', 'function': {'name': DECISION_NAME}})
                self.assertEqual([tool['function']['name'] for tool in body['tools']], [DECISION_NAME])
                return decision_response(answer)
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                llm = LLM(Config(), client)
                call = llm.consider([], '看这张图片', {'speaker': '小明', 'recent': []}, images=['data:image/png;base64,test'])
                if expected == 'ERROR':
                    with self.assertRaises(ModelError):
                        await call
                else:
                    self.assertEqual(await call, expected)
            self.assertEqual(len(requests), 1)

    async def test_other_provider_keeps_json_mode_for_auto_decisions(self):
        def provider(request):
            body = json.loads(request.content)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"reply":false}'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            answer = await LLM(Config(base_url='https://other.example/v1'), client).consider(
                [], '普通闲聊', {'speaker': '测试群友', 'recent': []})
        self.assertIsNone(answer)

    async def test_full_automatic_picture_pipeline_with_onebot_ack(self):
        sent, api = [], []
        ready = asyncio.Event()
        def provider(request):
            if request.method == 'GET':
                self.assertEqual(str(request.url), IMAGE_URL)
                return httpx.Response(200, content=PNG)
            body = json.loads(request.content)
            self.assertEqual(body['messages'][-1]['content'][1]['image_url']['url'],
                             'data:image/png;base64,' + base64.b64encode(PNG).decode('ascii'))
            api.append(body)
            return decision_response('{"reply":true,"content":"图里有个白点。"}')

        async def napcat(ws):
            await ws.send(json.dumps(photo('这张图大家能看懂吗')))
            async for raw in ws:
                call = json.loads(raw)
                self.assertEqual(call['action'], 'send_group_msg')
                sent.append(call['params'])
                await ws.send(json.dumps({'status': 'ok', 'retcode': 0, 'data': {'message_id': 321}, 'echo': call['echo']}))
                ready.set()

        async with serve(napcat, '127.0.0.1', 0) as server:
            c = Config(ws_url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}',
                       groups=frozenset({100}), auto_mode='smart', token='test')
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                async with OneBot(c) as connection:
                    bot = Bot(c, LLM(c, client), Images(c, client))
                    task = asyncio.create_task(bot.serve(connection))
                    await asyncio.wait_for(ready.wait(), 3)
                    for _ in range(100):
                        if not bot.active:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(len(bot.groups[100].replies), 1)
                    self.assertIn('321', bot.groups[100].bot_message_ids)
                    self.assertNotIn('base64', str(bot.sessions[(100, 10)].history))
                    await connection.ws.close()
                    await asyncio.wait_for(task, 3)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(api), 1)
        self.assertEqual([part['type'] for part in sent[0]['message']], ['text'])
        self.assertIn('图里有个白点', sent[0]['message'][0]['data']['text'])

    def test_new_config_validation(self):
        env = {'ONEBOT_TOKEN': 'test', 'ALLOWED_GROUPS': '100', 'LLM_API_KEY': 'test'}
        for values in [{'AUTO_REPLY_MODE': 'all'}, {'VISION_ENABLED': 'maybe'}, {'MAX_IMAGES': '0'},
                       {'AUTO_CHECK_INTERVAL_SECONDS': 'nan'}, {'AUTO_MAX_REPLIES_PER_HOUR': '-1'}]:
            with patch('qqbot.config.load_dotenv'), patch.dict('os.environ', {**env, **values}, clear=True):
                with self.assertRaises(ValueError):
                    Config.load()


if __name__ == '__main__':
    unittest.main()
