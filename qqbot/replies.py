"""Bounded reply plans. Images are catalogue IDs, never model-provided URLs."""
from dataclasses import dataclass, replace


FACES = {'微笑': '14', '呲牙': '13', '流泪': '5', '大哭': '9', '发怒': '11',
         '惊讶': '0', '尴尬': '10', '睡觉': '8'}


@dataclass(frozen=True)
class Bubble:
    text: str = ''
    image: str = ''
    face: str = ''
    image_label: str = ''

    def summary(self):
        return self.text + (f'[图片:{self.image_label or self.image}]' if self.image else '') + (f'[QQ表情:{self.face}]' if self.face else '')


class Reply(str):
    """Also a string so existing text-only history storage stays compatible."""
    def __new__(cls, bubbles):
        bubbles = tuple(bubbles)
        value = super().__new__(cls, '\n'.join(b.summary() for b in bubbles))
        value.bubbles = bubbles
        return value

    def limited(self, maximum):
        left, result = maximum, []
        for bubble in self.bubbles:
            text = bubble.text[:max(0, left)]
            left -= len(text)
            if text or bubble.image or bubble.face:
                result.append(replace(bubble, text=text))
        return Reply(result)


def reply_plan(decision, config, catalogue=()):
    messages = decision.get('messages')
    if not config.rich_reply_enabled or not messages:
        content = decision.get('content')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('missing reply text')
        return content.strip()
    if not isinstance(messages, list):
        raise ValueError('invalid messages')
    allowed = {item['id']: item['label'] for item in catalogue}
    result = []
    for item in messages[:config.reply_max_messages]:
        if not isinstance(item, dict):
            raise ValueError('invalid message')
        text, image, face = (item.get(k, '') for k in ('text', 'image', 'face'))
        if not all(isinstance(x, str) for x in (text, image, face)):
            raise ValueError('invalid message values')
        if image and image not in allowed:
            raise ValueError('unknown image ID')
        if face and face not in FACES:
            raise ValueError('unknown QQ face')
        if text.strip() or image or face:
            result.append(Bubble(text.strip(), image, face, allowed.get(image, '')))
    if not result:
        raise ValueError('empty reply')
    return Reply(result).limited(config.max_reply)
