import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

import httpx

from qqbot.bot import Bot
from qqbot.config import Config
from qqbot.images import Images
from qqbot.llm import DECISION_NAME, LLM, ModelError
from qqbot.memory import Memory
from test_bot import Replies, event
from test_features import PNG, SmartModel, decision_response, photo


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unaddressed_message_with_memory_and_eight_turns_sends_natural_reply(self):
        c = Config(groups=frozenset({100}), cooldown=0, auto_mode='smart', history_turns=8,
                   search_enabled=True, extra={'thinking': {'type': 'enabled'}, 'tool_choice': 'none',
                                              'response_format': {'type': 'json_object'}})
        requests = []

        def provider(request):
            self.assertEqual(request.url.path, '/chat/completions')
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            self.assertNotIn('response_format', body)
            self.assertEqual(body['tool_choice'], {'type': 'function', 'function': {'name': DECISION_NAME}})
            self.assertEqual([tool['function']['name'] for tool in body['tools']], [DECISION_NAME])
            self.assertEqual(sum(m['role'] == 'assistant' for m in body['messages']), 8)
            self.assertTrue(any('记忆资料' in m['content'] for m in body['messages'] if m['role'] == 'system'))
            return decision_response('{"reply":true,"content":"先看看循环条件会不会改变，不行就用break退出"}')

        with tempfile.TemporaryDirectory() as folder:
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                llm = LLM(c, client)
                memory = Memory(c, llm, Path(folder) / 'memory.sqlite3')
                try:
                    memory.member(100, 10, '测试群友')
                    history = []
                    for i in range(8):
                        history.extend([{'role': 'user', 'content': f'前面的第{i}轮闲聊'},
                                        {'role': 'assistant', 'content': f'第{i}轮随口接话'}])
                    memory.save_history(100, 10, history)
                    bot, out = Bot(c, llm, memory=memory), Replies()
                    await bot.handle(event('这个Python循环怎么退出？'), out)
                    self.assertEqual(len(out.items), 1)
                    self.assertIn('break', out.items[0][2])
                    self.assertNotIn('reply', out.items[0][2])
                    self.assertEqual(len(requests), 1)
                finally:
                    await memory.close()

    async def test_invalid_or_foreign_decision_tools_never_execute_or_send_prose(self):
        good = {'id': 'call1', 'type': 'function', 'function': {
            'name': DECISION_NAME, 'arguments': '{"reply":false,"content":""}'}}
        foreign = {'id': 'call1', 'type': 'function', 'function': {
            'name': 'web_search', 'arguments': '{"query":"should not execute"}'}}
        for message in [
            {'content': '普通聊天内容，没有结构化决策'},
            {'content': None, 'tool_calls': [foreign]},
            {'content': None, 'tool_calls': [good, good]},
            {'content': None, 'tool_calls': [{'function': {'name': DECISION_NAME, 'arguments': {}}}]},
        ]:
            requests = []

            def provider(request):
                requests.append(request.url.path)
                return httpx.Response(200, json={'choices': [{'message': message}]})

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                with self.assertRaises(ModelError):
                    await LLM(Config(search_enabled=True), client).consider(
                        [], '普通问题', {'speaker': '测试群友', 'recent': []})
            self.assertEqual(requests, ['/chat/completions'])

    async def test_explicit_image_receives_prior_group_chat_in_every_mode(self):
        for mode in ('off', 'smart', 'keywords'):
            with self.subTest(mode=mode):
                c = Config(groups=frozenset({100, 200}), cooldown=0, auto_mode=mode)
                model, out = SmartModel(), Replies()
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PNG))) as client:
                    bot = Bot(c, model, Images(c, client))
                    await bot.handle(event('隔壁群在聊汽车', group=200, user=11, mid=1), out)
                    for mid, text in enumerate(('今晚点了个外卖', '我怕踩雷', '等下给你们看看'), start=2):
                        await bot.handle(event(text, user=11, mid=mid), out)
                    e = photo('', mid=5)
                    e['message'].insert(0, {'type': 'at', 'data': {'qq': '999'}})
                    await bot.handle(e, out)
                    context = model.contexts[-1]
                    self.assertEqual([line['text'] for line in context['recent']],
                                     ['今晚点了个外卖', '我怕踩雷', '等下给你们看看'])
                    self.assertEqual(context['speaker'], '群友(QQ:10)')
                    self.assertTrue(all('(QQ:11)' in line['speaker'] for line in context['recent']))
                    self.assertEqual(model.calls[-1][1], '')
                    self.assertEqual(len(model.pictures[-1]), 1)
                    self.assertNotIn('隔壁群', str(context))
                    self.assertNotIn('我怕踩雷', str(bot.sessions[(100, 10)].history))

    async def test_explicit_text_includes_bot_reply_target_and_excludes_expired_chat(self):
        c = Config(groups=frozenset({100}), cooldown=0, auto_mode='off')
        model, out = SmartModel(), Replies()
        bot = Bot(c, model)
        await bot.handle(event('这是过期话题', user=11, mid=1), out)
        bot.groups[100].lines[0]['time'] = time.monotonic() - c.group_context_ttl - 1
        await bot.handle(event('/ai 我今晚点外卖', user=11, mid=2), out)
        await bot.handle(event('/ai 那你觉得怎么样', mid=3), out)
        context = model.contexts[-1]
        self.assertNotIn('过期话题', str(context))
        self.assertNotIn('那你觉得怎么样', str(context['recent']))
        replies = [line for line in context['recent'] if line['is_bot']]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]['reply_to_qq'], 11)

    async def test_context_deleted_while_image_loads_is_not_sent(self):
        c = Config(groups=frozenset({100}), cooldown=0)
        model, out = SmartModel(), Replies()
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowImages:
            async def load(self, refs, connection):
                entered.set()
                await release.wait()
                return ['data:image/png;base64,test']

        bot = Bot(c, model, SlowImages())
        await bot.handle(event('我发过但等会要清空的内容', user=11, mid=1), out)
        e = photo('', mid=2)
        e['message'].insert(0, {'type': 'at', 'data': {'qq': '999'}})
        task = asyncio.create_task(bot.handle(e, out))
        await entered.wait()
        await bot.handle(event('/ai reset', user=11, mid=3), out)
        release.set()
        await task
        self.assertNotIn('等会要清空', str(model.contexts[-1]))

    async def test_busy_chat_does_not_replace_the_context_of_an_inflight_image(self):
        c = Config(groups=frozenset({100}), cooldown=0, group_context_messages=3)
        model, out = SmartModel(), Replies()
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowImages:
            async def load(self, refs, connection):
                entered.set()
                await release.wait()
                return ['data:image/png;base64,test']

        bot = Bot(c, model, SlowImages())
        for mid in range(1, 4):
            await bot.handle(event(f'发图之前的第{mid}句话', user=11, mid=mid), out)
        e = photo('', mid=4)
        e['message'].insert(0, {'type': 'at', 'data': {'qq': '999'}})
        task = asyncio.create_task(bot.handle(e, out))
        await entered.wait()
        for mid in range(5, 9):
            await bot.handle(event(f'发图之后的第{mid}句话', user=11, mid=mid), out)
        release.set()
        await task
        self.assertEqual([line['text'] for line in model.contexts[-1]['recent']],
                         [f'发图之前的第{mid}句话' for mid in range(1, 4)])

    async def test_explicit_image_payload_has_chat_context_without_forcing_description_or_json(self):
        context = {'speaker': '小明(QQ:10)', 'recent': [
            {'speaker': '小红(QQ:11)', 'text': '今晚这外卖不会又踩雷吧', 'is_bot': False, 'reply_to_qq': None},
            {'speaker': '小明(QQ:10)', 'text': '端上来了，给你看看', 'is_bot': False, 'reply_to_qq': None},
        ]}

        def provider(request):
            body = json.loads(request.content)
            self.assertIn('tools', body)  # Context must not turn an explicit call into auto mode.
            self.assertNotIn('response_format', body)
            content = body['messages'][-1]['content']
            current = json.loads(content[0]['text'])
            self.assertEqual(current['current_message']['text'], '')
            self.assertEqual(current['current_message']['image_count'], 1)
            self.assertEqual(current['recent_group_messages'], context['recent'])
            self.assertNotIn('请描述或分析这张图片', content[0]['text'])
            self.assertEqual(content[1]['type'], 'image_url')
            self.assertEqual(body['thinking']['type'], 'disabled')
            return httpx.Response(200, json={'choices': [{'message': {'content': '这卖相就有种开奖的感觉'}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            answer = await LLM(Config(search_enabled=True), client).ask([], '', images=['data:image/png;base64,test'], context=context)
        self.assertIn('开奖', answer)

    async def test_bare_image_without_context_does_not_invent_a_description_request(self):
        def provider(request):
            body = json.loads(request.content)
            text = body['messages'][-1]['content'][0]['text']
            self.assertIn('只发送了图片', text)
            self.assertNotIn('请描述', text)
            return httpx.Response(200, json={'choices': [{'message': {'content': '这是啥情况'}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            answer = await LLM(Config(), client).ask([], '', images=['data:image/png;base64,test'])
        self.assertEqual(answer, '这是啥情况')


if __name__ == '__main__':
    unittest.main()
