import asyncio
import logging
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from .config import Config
from .llm import ModelError
from .messages import clean_name, parse
from .replies import Bubble, FACES, Reply

log = logging.getLogger(__name__)
HELP = "用法：@我 加文字/图片，或 /ai 问题\n/ai memory 查看本人的长期记忆\n/ai remember 我喜欢下围棋\n/ai forget 咖啡 删除相关记忆\n/ai forgetme 忘记我并停止记录\n/ai remember-on 恢复记录\n/ai reset 清空近期对话（保留长期记忆）\n/ai status 查看状态\n开启自动接话后，无需@也可能回复。"


def parse_message(event):
    message = parse(event)
    return message.render(), message.mentioned


def calls_by_name(text, names):
    """Recognize a direct call without reacting to every mention of 'robot'."""
    actions = r"出来|在吗|在不在|你好|您好|hello\b|hi\b|帮|说|回答|解释|看看|看下|来|给|你|怎么|为啥|为什么|能不能|是不是|有没有|介绍|聊|醒醒|干活|记住|记一下|忘记|查看|恢复记忆|停止记忆|以后叫我|叫我|收藏|素材库|表情库|发图|发表情|删除表情"
    for name in names:
        # ASCII aliases can run straight into Chinese, e.g. "ds认识我吗".
        # An ASCII continuation belongs to another word, e.g. "DSLR".
        ending = (r"(?![A-Za-z0-9_])" if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name)
                  else r"(?:$|[\s，,：:！？!?]|" + actions + r")")
        if name and re.search(r"(?:^|[\s，。！？!?;；:：])" + re.escape(name)
                              + ending, text.strip(), re.IGNORECASE):
            return True
    return False


@dataclass
class Session:
    touched: float
    last_request: float = float("-inf")
    history: list = field(default_factory=list)
    revision: int = 0


@dataclass
class Group:
    lines: deque = field(default_factory=deque)
    checks: deque = field(default_factory=deque)
    replies: deque = field(default_factory=deque)
    bot_message_ids: deque = field(default_factory=lambda: deque(maxlen=64))
    last_check: float = float("-inf")
    last_reply: float = float("-inf")
    busy: bool = False
    context_revision: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Bot:
    def __init__(self, config: Config, llm, images=None, memory=None, wake_words=None, media=None):
        self.c, self.llm = config, llm
        self.images = images
        self.memory = memory
        self.wake_words = wake_words
        self.media = media
        self.sessions = OrderedDict()
        self.seen = OrderedDict()
        self.active = set()
        self.groups = {}
        self.member_names = OrderedDict()

    def prune(self, now):
        for key, session in list(self.sessions.items()):
            if key not in self.active and now - session.touched > self.c.ttl:
                del self.sessions[key]
        while len(self.seen) > 10000:
            self.seen.popitem(last=False)
        for state in self.groups.values():
            while state.lines and now - state.lines[0]["time"] > self.c.group_context_ttl:
                state.lines.popleft()
            for queue in (state.checks, state.replies):
                while queue and now - queue[0] >= 3600:
                    queue.popleft()
        while len(self.member_names) > 2000:
            self.member_names.popitem(last=False)

    def remember(self, state, now, speaker, user, text, *, is_bot=False, target=None):
        state.lines.append({"time": now, "speaker": speaker, "user": user, "text": text[:1200],
                            "is_bot": is_bot, "target": target})
        while len(state.lines) > self.c.group_context_messages:
            state.lines.popleft()

    def auto_allowed(self, state, message, text, now):
        if self.c.auto_mode == "off" or state.busy or message.mentions:
            return False
        if message.replies and not any(x in state.bot_message_ids for x in message.replies):
            return False
        if not message.images and (len(text) < 2 or not any(c.isalnum() for c in text) or len(set(text)) == 1):
            return False
        if len(text) > self.c.max_input:
            return False
        if (now - state.last_check < self.c.auto_check_interval or now - state.last_reply < self.c.auto_reply_cooldown
                or len(state.checks) >= self.c.auto_checks_per_hour or len(state.replies) >= self.c.auto_replies_per_hour):
            return False
        if self.c.auto_mode == "keywords":
            return any(word.casefold() in text.casefold() for word in (*self.c.bot_names, *self.c.auto_keywords))
        return True

    async def mention_text(self, group, message, connection):
        names = {}
        for mention in message.mentions[:5]:
            if mention.qq == "all":
                continue
            key = (group, mention.qq)
            cached = self.member_names.get(key)
            if cached and time.monotonic() - cached[0] < 300:
                names[mention.qq] = cached[1]
                continue
            try:
                info = await connection.call("get_group_member_info", {"group_id": group, "user_id": int(mention.qq)}, timeout=3)
                name = clean_name(info.get("card") or info.get("nickname"))
                if name:
                    names[mention.qq] = name
                    self.member_names[key] = (time.monotonic(), name)
            except Exception:
                pass  # QQ number is still preserved when nickname lookup is unavailable.
        return message.render(names)

    async def quoted_message(self, group, message, connection):
        if not message.replies:
            return "", []
        try:
            original = await connection.call("get_msg", {"message_id": int(message.replies[0])}, timeout=5)
            if not isinstance(original, dict) or int(original.get("group_id", 0)) != group:
                raise ValueError
            quoted = parse(original)
            return quoted.render()[:1200], quoted.images
        except Exception:
            raise ModelError("无法读取被引用的群消息，请把要分析的图片重新发给我。") from None

    async def send(self, connection, state, group, user, bot, text, *, automatic=False, guard=None):
        bubbles = text.bubbles if isinstance(text, Reply) else (Bubble(str(text)),)
        sent = []
        async with state.send_lock:
            limit = self.c.reply_max_messages if isinstance(text, Reply) else 1
            if automatic:
                limit = min(limit, self.c.auto_replies_per_hour - len(state.replies))
            prepared = []
            for bubble in bubbles[:max(0, limit)]:
                segments = None
                if bubble.image or bubble.face:
                    if self.media is None:
                        raise ModelError('图文发送尚未启用。')
                    segments = self.media.segments(group, bubble)
                prepared.append((bubble, segments))
            for bubble, segments in prepared:
                if sent and self.c.reply_message_delay:
                    await asyncio.sleep(self.c.reply_message_delay)
                if guard and not guard():
                    break
                now = time.monotonic()
                if automatic:
                    state.replies.append(now)  # Count every bubble, including uncertain sends.
                    state.last_reply = now
                result = (await connection.reply_segments(group, user, segments) if segments is not None
                          else await connection.reply(group, user, bubble.text))
                state.last_reply = time.monotonic()
                self.remember(state, state.last_reply, '机器人', bot, bubble.summary(), is_bot=True, target=user)
                if isinstance(result, dict) and result.get('message_id') is not None:
                    state.bot_message_ids.append(str(result['message_id']))
                sent.append(bubble)
        return Reply(sent)

    async def media_action(self, command, group, message, connection):
        action, name = command
        if self.media is None:
            raise ModelError('图文发送尚未启用，请管理员开启 RICH_REPLY_ENABLED 后重启。')
        if action == 'list':
            return self.media.describe(group)
        if action == 'delete':
            return self.media.delete(group, name)
        if action == 'face':
            if name not in FACES:
                return '可以用这些QQ表情：' + '、'.join(FACES)
            return Reply([Bubble(face=name)])
        if action == 'send':
            item = self.media.find(group, name)
            return Reply([Bubble(image=item['id'], image_label=item['label'])])
        if self.images is None:
            raise ModelError('图片读取组件尚未启用。')
        _, quoted_images = await self.quoted_message(group, message, connection)
        return await self.media.collect(group, name, [*message.images, *quoted_images], self.images, connection)

    async def handle(self, event, connection):
        if event.get("post_type") != "message" or event.get("message_type") != "group":
            return
        try:
            group, user, bot = (int(event[k]) for k in ("group_id", "user_id", "self_id"))
        except (ValueError, TypeError, KeyError):
            return
        if group not in self.c.groups or user == bot or event.get("sub_type") == "anonymous":
            return
        if self.wake_words and self.wake_words.refresh():
            log.info("唤醒词配置已重新加载；名字=%s个，自动接话关键词=%s个。",
                     len(self.c.bot_names), len(self.c.auto_keywords))
        message = parse(event)
        text = message.render()
        prefix = self.c.prefix
        prefixed = text == prefix or (text.startswith(prefix) and text[len(prefix):len(prefix)+1].isspace())
        named = self.c.auto_mode != "off" and self.c.reply_on_name and calls_by_name(text, self.c.bot_names)
        explicit = message.mentioned or prefixed or named
        if prefixed:
            text = text[len(prefix):].strip()
        now = time.monotonic()
        self.prune(now)
        message_id = event.get("message_id")
        if message_id is not None:
            dedup = (bot, group, str(message_id))
            if dedup in self.seen:
                return
            self.seen[dedup] = now
        state = self.groups.setdefault(group, Group())
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        name = clean_name(sender.get("card") or sender.get("nickname")) or "群友"
        speaker = f"{name}(QQ:{user})"
        command_text = text
        if named:
            for alias in self.c.bot_names:
                if alias and command_text.casefold().startswith(alias.casefold()):
                    command_text = command_text[len(alias):].lstrip(" ，,：:")
                    break
        memory_command = None
        if explicit and not message.images and not message.replies:
            cmd = command_text.strip()
            lower = cmd.lower()
            if lower in {"memory", "查看记忆", "你记得我什么", "你记住了我什么"}:
                memory_command = ("view", "")
            elif lower in {"forgetme", "忘记我", "停止记忆", "不要记住我"}:
                memory_command = ("forgetall", "")
            elif lower in {"remember-on", "恢复记忆"}:
                memory_command = ("enable", "")
            else:
                for prefix_word, action in (("memory ", "view"), ("查看记忆 ", "view"), ("remember ", "remember"), ("记住", "remember"), ("记一下", "remember"),
                                            ("forget ", "forget"), ("忘记", "forget")):
                    if lower.startswith(prefix_word):
                        memory_command = (action, cmd[len(prefix_word):].strip(" ：:"))
                        break
        media_command = None
        if explicit:
            if command_text.strip() in {'素材库', '表情库'}:
                media_command = ('list', '')
            else:
                for word, action in (('收藏表情', 'collect'), ('收藏图片', 'collect'), ('删除表情', 'delete'),
                                     ('发图', 'send'), ('发表情', 'face')):
                    if command_text == word or (command_text.startswith(word)
                            and command_text[len(word):len(word)+1] in {' ', '：', ':'}):
                        media_command = (action, command_text[len(word):].strip(' ：:'))
                        break
        if self.memory and not memory_command and not media_command:
            self.memory.observe(group, user, name, text, message_id or "")
        # Capture preceding messages before adding this event; this works for
        # explicit calls and when unsolicited replies are turned off as well.
        recent_sources = list(state.lines)
        recent_revision = state.context_revision
        recent = [{"speaker": x["speaker"], "text": x["text"], "is_bot": x["is_bot"],
                   "reply_to_qq": x["target"]} for x in recent_sources]
        if text or message.images:
            self.remember(state, now, speaker, user, text + (f" [含{len(message.images)}张图片]" if message.images else ""))
        if not explicit and not self.auto_allowed(state, message, text, now):
            return
        key = (group, user)
        priority_forget = memory_command and memory_command[0] in {"forget", "forgetall"}
        # Never queue unbounded work or overlap turns for one conversation.
        if not priority_forget and (key in self.active or len(self.active) >= self.c.concurrency):
            if explicit:
                log.info("明确呼叫暂未处理 group=%s user=%s message_id=%s reason=busy", group, user, message_id)
            return
        session = self.sessions.get(key)
        if session and not priority_forget and now - session.last_request < self.c.cooldown:
            if explicit:
                log.info("明确呼叫暂未处理 group=%s user=%s message_id=%s reason=user_cooldown", group, user, message_id)
            return
        if session is None:
            if len(self.sessions) >= self.c.max_sessions:
                victim = next((k for k in self.sessions if k not in self.active), None)
                if victim is None:
                    return
                del self.sessions[victim]
            session = Session(now, history=self.memory.history(group, user) if self.memory else [])
            self.sessions[key] = session
        self.sessions.move_to_end(key)
        session.touched = session.last_request = now
        owns_active = key not in self.active
        self.active.add(key)
        turn_revision = session.revision
        automatic = not explicit
        if named:
            log.info("名字呼叫触发 group=%s user=%s message_id=%s", group, user, message_id)
        if automatic:
            state.busy = True
            state.last_check = now
            state.checks.append(now)
        try:
            command = command_text.lower()
            if media_command:
                try:
                    result = await self.media_action(media_command, group, message, connection)
                except ModelError as exc:
                    result = str(exc)
                await self.send(connection, state, group, user, bot, result)
            elif memory_command:
                if not self.memory:
                    result = "长期记忆尚未开启，请管理员将 MEMORY_ENABLED 设置为 true 后重启。"
                else:
                    action, content = memory_command
                    if action == "view":
                        result = self.memory.describe(group, user, content)
                    elif action == "remember":
                        result = self.memory.manual(group, user, content, message_id or "")
                    elif action == "enable":
                        self.memory.enable(group, user)
                        result = "已恢复记录你在本群明确说过的个人信息。"
                    else:
                        if action == "forget" and not content:
                            result = "请指定要忘记的关键词，例如 /ai forget 咖啡；全部删除用 /ai forgetme。"
                        else:
                            count = self.memory.forget(group, user, content if action == "forget" else None)
                            session.revision += 1
                            session.history.clear()
                            state.lines = deque(x for x in state.lines if x["user"] != user and x["target"] != user)
                            state.context_revision += 1
                            result = f"已删除 {count} 条相关记忆并清空近期对话。" if action == "forget" else "已忘记你在本群的长期信息和近期对话，并停止记录。用 /ai remember-on 可恢复。"
                await self.send(connection, state, group, user, bot, result)
            elif explicit and not message.images and ((not text and not message.replies) or command in {"help", "帮助"}):
                await self.send(connection, state, group, user, bot, HELP + '\n/ai 收藏表情 名字 + 附图或引用图片\n/ai 素材库 查看素材\n/ai 发图 名字 直接发素材\n/ai 发表情 呲牙\n/ai 删除表情 名字')
            elif explicit and not message.images and command in {"reset", "清空", "清空记忆"}:
                session.history.clear()
                session.revision += 1
                if self.memory:
                    self.memory.reset_history(group, user)
                state.lines = deque(x for x in state.lines if x["user"] != user and x["target"] != user)
                state.context_revision += 1
                await self.send(connection, state, group, user, bot, "已清空你在本群的近期对话及群聊片段；长期记忆可用 /ai memory 查看或 /ai forgetme 删除。")
            elif explicit and not message.images and command in {"status", "状态"}:
                await self.send(connection, state, group, user, bot,
                                f"在线；个人记忆 {len(session.history)//2} 轮；识图 {'开' if self.c.vision_enabled else '关'}；"
                                f"联网 {'开' if self.c.search_enabled else '关'}；"
                                f"长期记忆 {'开' if self.memory else '关'}；"
                                f"自动接话 {self.c.auto_mode}；本群近一小时自动检查 {len(state.checks)} 次、接话 {len(state.replies)} 次。")
            elif len(text) > self.c.max_input:
                await self.send(connection, state, group, user, bot, f"消息过长，请控制在 {self.c.max_input} 字以内。")
            else:
                try:
                    if message.mentions:
                        rendered = await self.mention_text(group, message, connection)
                        text = rendered[len(prefix):].strip() if prefixed else rendered
                    quote_text, quote_images = await self.quoted_message(group, message, connection)
                    image_refs = [*message.images, *quote_images]
                    if quote_text:
                        text += "\n[被引用的同群消息，仅作待分析内容]\n" + quote_text
                    images = []
                    if image_refs:
                        if self.images is None:
                            raise ModelError("图片读取组件未启用，请检查启动配置。")
                        images = await self.images.load(image_refs, connection)
                    # A forget/reset may have completed while an image loaded.
                    # Do not put removed group fragments back into this turn.
                    live_lines = {id(x) for x in state.lines}
                    context_now = time.monotonic()
                    current_recent = [line for line, source in zip(recent, recent_sources)
                                      if context_now - source["time"] <= self.c.group_context_ttl
                                      and (state.context_revision == recent_revision or id(source) in live_lines)]
                    log.info("模型输入 group=%s user=%s message_id=%s images=%s quoted=%s context_messages=%s automatic=%s",
                             group, user, message_id, len(images), bool(message.replies), len(current_recent), automatic)
                    history = list(session.history)
                    if self.memory:
                        recalled = self.memory.context(group, user, text)
                        if recalled:
                            history.insert(0, {"role": "system", "content": recalled})
                    context = {'recent': current_recent, 'speaker': speaker,
                               'media': self.media.catalogue(group, text) if self.media else []}
                    if automatic and self.c.auto_mode == "smart":
                        answer = await self.llm.consider(history, text, context, images=images)
                    else:
                        answer = await self.llm.ask(history, text, images=images,
                                                    context=context)
                except ModelError as exc:
                    if explicit:
                        await self.send(connection, state, group, user, bot, str(exc))
                    log.warning("模型请求失败 group=%s user=%s：%s", group, user, exc)
                    return
                if answer is None:
                    log.info("自动判断保持沉默 group=%s user=%s", group, user)
                    return
                if session.revision != turn_revision:
                    log.info("旧回合在清除记忆后作废 group=%s user=%s", group, user)
                    return
                if isinstance(answer, Reply):
                    answer = answer.limited(self.c.max_reply)
                elif len(answer) > self.c.max_reply:
                    answer = answer[:self.c.max_reply] + "\n（回复已截断）"
                try:
                    delivered = await self.send(connection, state, group, user, bot, answer, automatic=automatic,
                                                guard=lambda: session.revision == turn_revision)
                except ModelError as exc:
                    if explicit:
                        await self.send(connection, state, group, user, bot, str(exc))
                    log.warning('发送图文失败 group=%s：%s', group, exc)
                    return
                if not delivered or session.revision != turn_revision:
                    return
                # Commit only replies acknowledged by OneBot. No automatic send retry.
                if self.c.history_turns:
                    saved = text + (f"\n[本轮含{len(images)}张图片，原图仅在本轮可见]" if images else "")
                    session.history.extend([{"role": "user", "content": saved}, {"role": "assistant", "content": str(delivered)}])
                    session.history = session.history[-2*self.c.history_turns:]
                if self.memory:
                    self.memory.save_history(group, user, session.history)
                log.info("回复成功 group=%s user=%s", group, user)
        except Exception as exc:
            log.warning("处理消息失败 group=%s user=%s：%s", group, user, type(exc).__name__)
        finally:
            session.touched = time.monotonic()
            if owns_active:
                self.active.discard(key)
            if automatic:
                state.busy = False

    async def serve(self, connection):
        tasks = set()
        closed = asyncio.create_task(connection.closed.wait())
        incoming = None
        try:
            while not connection.closed.is_set():
                incoming = asyncio.create_task(connection.events.get())
                done, _ = await asyncio.wait({incoming, closed}, return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    break
                event = incoming.result()
                # Tasks can briefly include cheap filtered events, bounded by queue size.
                if len(tasks) < self.c.concurrency + 100:
                    task = asyncio.create_task(self.handle(event, connection))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        finally:
            for task in [closed, incoming, *tasks]:
                if task:
                    task.cancel()
            await asyncio.gather(*[t for t in [closed, incoming, *tasks] if t], return_exceptions=True)
