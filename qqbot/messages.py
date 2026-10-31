"""Keep text, member mentions and image references from OneBot messages."""
import re
from dataclasses import dataclass, field


def clean_name(value):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:60].strip()


def decode_cq(value):
    return value.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def segments(message):
    if isinstance(message, list):
        return [x for x in message if isinstance(x, dict) and isinstance(x.get("data"), dict)]
    if not isinstance(message, str):
        return []
    result, start = [], 0
    for match in re.finditer(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]", message):
        result.append({"type": "text", "data": {"text": decode_cq(message[start:match.start()])}})
        data = {}
        for param in match[2].lstrip(",").split(","):
            key, sep, value = param.partition("=")
            if sep:
                data[key] = decode_cq(value)
        result.append({"type": match[1], "data": data})
        start = match.end()
    result.append({"type": "text", "data": {"text": decode_cq(message[start:])}})
    return result


@dataclass
class Mention:
    qq: str
    name: str = ""


@dataclass
class Message:
    parts: list = field(default_factory=list)
    mentioned: bool = False
    images: list[dict] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    def render(self, names=None):
        names = names or {}
        text = []
        for part in self.parts:
            if isinstance(part, str):
                text.append(part)
            elif part.qq == "all":
                text.append(" @全体成员 ")
            else:
                name = clean_name(names.get(part.qq) or part.name) or "群友"
                text.append(f" @{name}(QQ:{part.qq}) ")
        return "".join(text).strip()


def parse(event):
    result = Message()
    bot = str(event.get("self_id", ""))
    for segment in segments(event.get("message", [])):
        data, kind = segment["data"], segment.get("type")
        if kind == "text" and isinstance(data.get("text"), str):
            result.parts.append(data["text"])
        elif kind == "at":
            qq = str(data.get("qq", ""))
            if qq == bot and bot:
                result.mentioned = True
            elif qq == "all" or re.fullmatch(r"[1-9][0-9]{0,19}", qq):
                mention = Mention(qq, clean_name(data.get("name")))
                result.mentions.append(mention)
                result.parts.append(mention)
        elif kind == "image":
            result.images.append(dict(data))
        elif kind == "reply" and str(data.get("id", "")).lstrip("-").isdigit():
            result.replies.append(str(data["id"]))
    return result
