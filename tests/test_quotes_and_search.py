import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from qqbot.bot import Bot
from qqbot.config import Config
from qqbot.images import Images
from qqbot.llm import LLM, ModelError
from qqbot.search import Web, public_url
from test_bot import Replies, event
from test_features import SmartModel, photo, PNG, decision_response


class QuotedImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_call_with_quoted_picture_sends_image_to_model(self):
        config = Config(groups=frozenset({100}), bot_names=('大肥鱼',), auto_mode='smart', cooldown=0)
        model = SmartModel()
        class Connection(Replies):
            async def call(self, action, params, timeout):
                self.request = (action, params)
                return photo('图片附带原文')
        connection = Connection()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PNG))) as client:
            bot = Bot(config, model, Images(config, client))
            message = event('大肥鱼给我解释一下')
            message['message'].insert(0, {'type': 'reply', 'data': {'id': '406093598'}})
            await bot.handle(message, connection)
        self.assertEqual(connection.request, ('get_msg', {'message_id': 406093598}))
        self.assertEqual(len(model.pictures[0]), 1)
        self.assertIn('图片附带原文', model.calls[0][1])
        self.assertIn('本轮含1张图片', str(bot.sessions[(100, 10)].history))

    async def test_empty_at_with_quoted_picture_does_not_show_help(self):
        config = Config(groups=frozenset({100}), cooldown=0)
        model = SmartModel()
        class Connection(Replies):
            async def call(self, *args, **kwargs):
                return photo('')
        connection = Connection()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PNG))) as client:
            bot = Bot(config, model, Images(config, client))
            message = event('')
            message['message'].extend([{'type': 'reply', 'data': {'id': '123'}}, {'type': 'at', 'data': {'qq': '999'}}])
            await bot.handle(message, connection)
        self.assertEqual(len(model.pictures), 1)
        self.assertNotIn('用法：', connection.items[-1][2])

    async def test_cross_group_quotes_are_not_loaded(self):
        class Connection(Replies):
            async def call(self, *args, **kwargs):
                return {**photo(), 'group_id': 888}
        connection, model = Connection(), SmartModel()
        bot = Bot(Config(groups=frozenset({100}), cooldown=0), model)
        message = event('/ai 解释图片')
        message['message'].insert(0, {'type': 'reply', 'data': {'id': '123'}})
        await bot.handle(message, connection)
        self.assertEqual(model.calls, [])
        self.assertIn('无法读取被引用', connection.items[-1][2])


def completion(content=None, tools=None, finish='stop'):
    message = {'role': 'assistant', 'content': content}
    if tools:
        message['tool_calls'] = tools
    return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': finish}]})


def tool(name='web_search', arguments='{"query":"DeepSeek 官方识图"}', id='call1'):
    return {'id': id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_tool_loop_returns_real_result_to_model(self):
        requests, completions = [], []
        def provider(request):
            requests.append(request.url.path)
            body = json.loads(request.content)
            if request.url.path.endswith('/tools/search'):
                self.assertEqual(body['query'], 'DeepSeek 官方识图')
                return httpx.Response(200, json={'results': [{'title': 'Vision', 'url': 'https://api-docs.deepseek.com/guides/vision/', 'summary': 'Flash supports images.'}]})
            completions.append(body)
            self.assertEqual(body['thinking']['type'], 'disabled')
            if len(completions) == 1:
                self.assertTrue(body['tools'])
                return completion(tools=[tool()], finish='tool_calls')
            self.assertEqual(body['messages'][-1]['role'], 'tool')
            self.assertIn('Flash supports images', body['messages'][-1]['content'])
            return completion('支持，来源 https://api-docs.deepseek.com/guides/vision/')
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            answer = await LLM(Config(search_enabled=True), client).ask([], '搜一下DeepSeek的识图说明')
        self.assertIn('https://', answer)
        self.assertEqual(requests, ['/chat/completions', '/v1/tools/search', '/chat/completions'])

    async def test_auto_judgment_does_not_get_search_tools(self):
        def provider(request):
            body = json.loads(request.content)
            self.assertEqual([tool['function']['name'] for tool in body['tools']], ['group_reply_decision'])
            return decision_response('{"reply":false,"content":""}')
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            result = await LLM(Config(search_enabled=True), client).consider([], '闲聊', {'speaker': 'A', 'recent': []})
        self.assertIsNone(result)

    async def test_tool_budget_and_failure_do_not_loop_or_leak(self):
        count = {'model': 0, 'web': 0}
        def provider(request):
            if request.url.path.endswith('/tools/search'):
                count['web'] += 1
                return httpx.Response(402, text='SECRET PROVIDER ERROR BODY')
            count['model'] += 1
            body = json.loads(request.content)
            if count['model'] == 1:
                return completion(tools=[tool(), tool(id='call2')], finish='tool_calls')
            self.assertEqual(body['tool_choice'], 'none')
            for message in body['messages']:
                self.assertNotIn('SECRET', str(message))
            return completion('这次没查到，联网额度不足。')
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            answer = await LLM(Config(search_enabled=True, search_max_tool_calls=1), client).ask([], '查一下')
        self.assertEqual(count, {'model': 2, 'web': 1})
        self.assertIn('没查到', answer)

    async def test_fetch_limits_hourly_quota_and_private_urls(self):
        requests = []
        def provider(request):
            requests.append(str(request.url))
            return httpx.Response(200, json={'title': 'Article', 'content': 'a'*12000})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            web = Web(Config(search_enabled=True, search_max_calls_per_hour=1), client)
            bad = await web.call('fetch_page', {'url': 'http://127.0.0.1/private'})
            self.assertIn('error', bad)
            good = await web.call('fetch_page', {'url': 'https://example.com/article'})
            self.assertEqual(len(good['content']), 8000)
            exhausted = await web.call('web_search', {'query': 'next'})
            self.assertIn('额度', exhausted['error'])
        self.assertEqual(len(requests), 1)
        for url in ['file:///C:/private', 'http://localhost/x', 'https://user:key@example.com/x', 'http://192.168.1.1/x']:
            self.assertFalse(public_url(url))

    async def test_unknown_tool_and_malformed_arguments_are_not_executed(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: self.fail('must not request'))) as client:
            web = Web(Config(search_enabled=True), client)
            for name, arguments in [('run_shell', {'command': 'anything'}), ('web_search', {'query': 'x'*129}), ('fetch_page', [])]:
                self.assertIn('error', await web.call(name, arguments))

    async def test_empty_completion_reports_token_limit(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: completion('', finish='length'))) as client:
            with self.assertRaisesRegex(ModelError, 'MAX_TOKENS'):
                await LLM(Config(), client).ask([], 'test')

    def test_search_provider_and_limits_are_validated(self):
        env = {'ONEBOT_TOKEN': 'test', 'ALLOWED_GROUPS': '100', 'LLM_API_KEY': 'test', 'SEARCH_ENABLED': 'true'}
        for values in [{'LLM_BASE_URL': 'https://other.example/v1'}, {'SEARCH_MAX_TOOL_CALLS': '5'}, {'SEARCH_MAX_CALLS_PER_HOUR': '0'}]:
            with patch('qqbot.config.load_dotenv'), patch.dict('os.environ', {**env, **values}, clear=True):
                with self.assertRaises(ValueError): Config.load()


if __name__ == '__main__':
    unittest.main()
