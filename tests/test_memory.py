import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from qqbot.bot import Bot
from qqbot.config import Config
from qqbot.llm import LLM
from qqbot.memory import Memory
from test_bot import Model, Replies, event


class MemoryModel(Model):
    def __init__(self):
        super().__init__()
        self.extractions = []
        self.facts = []

    async def extract_memory(self, text, known):
        self.extractions.append((text, known))
        return self.facts


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'memory.sqlite3'
        self.c = Config(groups=frozenset({100, 200}), cooldown=0, auto_mode='off', memory_enabled=True,
                        bot_names=('大肥鱼',), memory_max_facts=4, memory_inject_facts=2)
        self.model = MemoryModel()
        self.memory = Memory(self.c, self.model, self.path)
        self.addAsyncCleanup(self.memory.close)

    async def test_facts_and_recent_history_survive_restart_and_rename(self):
        m = self.memory
        m.member(100, 10, '旧昵称')
        m.put(100, 10, 'preference', '围棋', '我喜欢下围棋')
        m.save_history(100, 10, [{'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': '在呢'}])
        await m.close()
        restored = Memory(self.c, self.model, self.path)
        self.addAsyncCleanup(restored.close)
        restored.member(100, 10, '新昵称')
        context = restored.context(100, 10, '我喜欢什么')
        self.assertIn('旧昵称', context)
        self.assertIn('新昵称', context)
        self.assertIn('我喜欢下围棋', context)
        self.assertEqual(len(restored.history(100, 10)), 2)
        self.assertEqual(restored.records(100, 11), [])
        self.assertEqual(restored.records(200, 10), [])

    async def test_scope_topic_updates_and_capacity(self):
        m = self.memory
        m.put(100, 10, 'preference', '咖啡', '我喜欢喝咖啡')
        m.put(100, 10, 'preference', '咖啡', '我现在不喜欢咖啡')
        self.assertEqual(len(m.records(100, 10)), 1)
        self.assertEqual(m.records(100, 10)[0]['evidence'], '我现在不喜欢咖啡')
        m.put(100, 11, 'profile', '称呼', '我叫小红')
        m.put(200, 10, 'profile', '称呼', '我叫群二的人')
        self.assertNotIn('小红', m.context(100, 10, '我是谁'))
        self.assertNotIn('群二的人', m.context(100, 10, '我是谁'))
        for i in range(8):
            m.put(100, 10, 'event', str(i), f'我正在准备第{i}件事')
        self.assertEqual(len(m.records(100, 10)), 4)

    async def test_relevant_recall_and_expiration(self):
        m = self.memory
        m.put(100, 10, 'preference', '咖啡', '我喜欢喝咖啡')
        m.put(100, 10, 'ongoing', '学习', '我正在学习数学')
        m.put(100, 10, 'event', '比赛', '我下周参加比赛')
        context = m.context(100, 10, '你还记得我对咖啡什么态度吗')
        self.assertIn('我喜欢喝咖啡', context)
        with m.db:
            m.db.execute('UPDATE facts SET expires=?', (time.time()-1,))
        self.assertEqual(m.records(100, 10), [])

    async def test_extraction_requires_exact_first_person_evidence(self):
        self.model.facts = [
            {'kind': 'preference', 'topic': '围棋', 'evidence': '我喜欢下围棋'},
            {'kind': 'profile', 'topic': '职业', 'evidence': '我是医生'},
            {'kind': 'preference', 'topic': '别人的话', 'evidence': '张三喜欢足球'},
        ]
        self.memory.observe(100, 10, '小明', '我喜欢下围棋。张三喜欢足球。', 123)
        await self.memory.flush()
        rows = self.memory.records(100, 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['evidence'], '我喜欢下围棋')
        self.assertEqual(rows[0]['message_id'], '123')

    async def test_extract_rate_limits_and_questions_do_not_trigger(self):
        self.memory.c = replace(self.c, memory_extracts_per_hour=1)
        self.memory.observe(100, 10, 'A', '大肥鱼你还记得我是谁吗', 1)
        self.assertEqual(self.memory.tasks, {})
        self.memory.observe(100, 10, 'A', '我喜欢下围棋', 2)
        await self.memory.flush()
        self.memory.observe(100, 10, 'A', '我喜欢下象棋', 3)
        self.memory.observe(100, 11, 'B', '我喜欢看电影', 4)
        await self.memory.flush()
        self.assertEqual(len(self.model.extractions), 1)

    async def test_forget_wins_against_an_inflight_extraction(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(text, known):
            entered.set()
            await release.wait()
            return [{'kind': 'preference', 'topic': '围棋', 'evidence': '我喜欢下围棋'}]
        self.model.extract_memory = slow
        self.memory.observe(100, 10, 'A', '我喜欢下围棋', 1)
        await entered.wait()
        self.memory.forget(100, 10)
        release.set()
        await self.memory.flush()
        self.assertEqual(self.memory.records(100, 10), [])
        self.assertTrue(self.memory.member(100, 10)['opted_out'])
        self.assertFalse(self.memory.put(100, 10, 'note', '测试', '我喜欢下象棋'))
        self.memory.enable(100, 10)
        self.assertTrue(self.memory.put(100, 10, 'note', '测试', '我喜欢下象棋'))

    async def test_keyword_forget_is_owner_scoped(self):
        m = self.memory
        for group, user in [(100, 10), (100, 11), (200, 10)]:
            m.put(group, user, 'preference', '咖啡', '我喜欢咖啡')
        m.put(100, 10, 'preference', '围棋', '我喜欢下围棋')
        m.forget(100, 10, '咖啡')
        self.assertEqual([r['topic'] for r in m.records(100, 10)], ['围棋'])
        self.assertEqual(len(m.records(100, 11)), 1)
        self.assertEqual(len(m.records(200, 10)), 1)

    async def test_history_excludes_credentials_system_prompts_and_images(self):
        history = [{'role': 'system', 'content': 'system should not persist'},
                   {'role': 'user', 'content': 'sk-abcdefghijklmnopqrstuv'},
                   {'role': 'assistant', 'content': 'data:image/png;base64,SECRET'},
                   {'role': 'user', 'content': '正常聊天'}, {'role': 'assistant', 'content': '收到'}]
        self.memory.save_history(100, 10, history)
        saved = self.memory.history(100, 10)
        self.assertEqual(len(saved), 2)
        self.assertNotIn('SECRET', str(saved))
        self.assertFalse(self.memory.put(100, 10, 'profile', '秘密', '我的密码是abc'))

    async def test_memory_commands_and_recall_in_bot(self):
        self.c.auto_mode = 'smart'
        bot, out = Bot(self.c, self.model, memory=self.memory), Replies()
        await bot.handle(event('大肥鱼记住我喜欢下围棋', mid=1), out)
        self.assertIn('记住了', out.items[-1][2])
        await bot.handle(event('/ai memory 围棋', mid=2), out)
        self.assertIn('我喜欢下围棋', out.items[-1][2])
        await bot.handle(event('/ai 我喜欢什么', mid=3), out)
        self.assertIn('我喜欢下围棋', self.model.calls[-1][0][0]['content'])
        self.assertEqual(self.model.calls[-1][0][0]['role'], 'system')
        await bot.handle(event('/ai reset', mid=4), out)
        self.assertEqual(self.memory.history(100, 10), [])
        self.assertEqual(len(self.memory.records(100, 10)), 1)
        await bot.handle(event('/ai forgetme', mid=5), out)
        self.assertEqual(self.memory.records(100, 10), [])
        self.assertTrue(self.memory.member(100, 10)['opted_out'])
        self.assertEqual(bot.sessions[(100, 10)].history, [])

    async def test_passive_self_statement_is_learned_without_reply(self):
        self.model.facts = [{'kind': 'preference', 'topic': '围棋', 'evidence': '我喜欢下围棋'}]
        out = Replies()
        bot = Bot(self.c, self.model, memory=self.memory)
        await bot.handle(event('我喜欢下围棋'), out)
        await self.memory.flush()
        self.assertEqual(out.items, [])
        self.assertEqual(len(self.memory.records(100, 10)), 1)

    async def test_forget_during_reply_discards_old_answer_and_history(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return '旧回答不应再发送'
        self.model.ask = slow
        bot, out = Bot(self.c, self.model, memory=self.memory), Replies()
        task = asyncio.create_task(bot.handle(event('/ai 继续聊天', mid=1), out))
        await entered.wait()
        await bot.handle(event('/ai forgetme', mid=2), out)
        release.set()
        await task
        self.assertEqual(len(out.items), 1)
        self.assertIn('已忘记', out.items[0][2])
        self.assertEqual(bot.sessions[(100, 10)].history, [])
        self.assertEqual(self.memory.history(100, 10), [])
        self.assertEqual(bot.active, set())

    async def test_view_is_bounded_and_searchable(self):
        self.memory.c = replace(self.c, max_reply=150)
        for i in range(4):
            self.memory.put(100, 10, 'note', str(i), '我喜欢'+str(i)+'以及'+'很多东西'*15)
        self.assertLessEqual(len(self.memory.describe(100, 10)), 150)
        self.assertIn('我喜欢2', self.memory.describe(100, 10, '我喜欢2'))


class MemoryModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_extractor_is_json_only_and_separate_from_persona_and_tools(self):
        def provider(request):
            body = json.loads(request.content)
            self.assertEqual(body['response_format']['type'], 'json_object')
            self.assertNotIn('tools', body)
            self.assertEqual(body['thinking']['type'], 'disabled')
            self.assertNotIn('PERSONA_SENTINEL', body['messages'][0]['content'])
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'facts': [{'kind': 'preference', 'topic': '围棋', 'evidence': '我喜欢下围棋'}]}, ensure_ascii=False)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            result = await LLM(Config(prompt='PERSONA_SENTINEL', search_enabled=True), client).extract_memory('我喜欢下围棋', [])
            self.assertEqual(result[0]['evidence'], '我喜欢下围棋')


if __name__ == '__main__':
    unittest.main()
