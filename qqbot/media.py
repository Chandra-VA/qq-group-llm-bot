"""Local shared pictures and explicitly collected, group-scoped stickers."""
import base64
import hashlib
import json
import re
from pathlib import Path

from .config import ROOT
from .images import image_mime
from .llm import ModelError
from .replies import FACES


EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
MIME_EXT = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp'}


class Media:
    def __init__(self, config, root=None):
        self.c = config
        self.root = Path(root or ROOT / 'assets').resolve()
        self.common = self.root / 'common'
        self.common.mkdir(parents=True, exist_ok=True)

    def safe(self, path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ModelError('素材路径无效。')
        return resolved

    def group_dir(self, group):
        return self.safe(self.root / 'groups' / str(int(group)))

    def index(self, group):
        path = self.group_dir(group) / 'index.json'
        if not path.exists():
            return []
        try:
            data = json.loads(self.safe(path).read_text(encoding='utf-8'))
            if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
                raise ValueError
            return data
        except (OSError, ValueError):
            raise ModelError('本群素材索引读取失败，请检查 assets/groups 下的 index.json。') from None

    def write_index(self, group, items):
        folder = self.group_dir(group)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = self.safe(folder / 'index.json.tmp')
        temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.safe(folder / 'index.json'))

    def items(self, group):
        result = []
        for path in sorted(self.common.iterdir()):
            if path.suffix.lower() not in EXTENSIONS or not path.is_file():
                continue
            try:
                resolved = self.safe(path)
                if resolved.stat().st_size > self.c.max_image_bytes:
                    continue
            except (ModelError, OSError):
                continue
            identity = 'local_' + hashlib.sha256(path.name.encode()).hexdigest()[:16]
            result.append({'id': identity, 'label': path.stem[:60], 'path': resolved, 'kind': '图片'})
            if len(result) >= self.c.media_max_assets:
                break
        for item in self.index(group):
            file, identity, label = item.get('file', ''), item.get('id', ''), item.get('label', '')
            if (not isinstance(file, str) or Path(file).name != file or Path(file).suffix.lower() not in EXTENSIONS
                    or not isinstance(identity, str) or not re.fullmatch(r'saved_[a-f0-9]{20}', identity)
                    or not isinstance(label, str)):
                continue
            try:
                path = self.safe(self.group_dir(group) / file)
                if path.is_file() and path.stat().st_size <= self.c.max_image_bytes:
                    result.append({'id': identity, 'label': label[:60], 'path': path, 'kind': '表情包'})
            except (ModelError, OSError):
                continue
        return result

    def catalogue(self, group, query=''):
        items = self.items(group)
        words = set(re.findall(r'[a-z0-9_]{2,}|[\u4e00-\u9fff]{2}', query.lower()))
        items.sort(key=lambda x: sum(w in x['label'].lower() for w in words), reverse=True)
        return [{k: item[k] for k in ('id', 'label', 'kind')} for item in items[:self.c.media_catalogue_limit]]

    def segments(self, group, bubble):
        parts = []
        if bubble.text:
            parts.append({'type': 'text', 'data': {'text': bubble.text}})
        if bubble.face:
            if bubble.face not in FACES:
                raise ModelError('没有这个 QQ 表情。')
            parts.append({'type': 'face', 'data': {'id': FACES[bubble.face]}})
        if bubble.image:
            item = next((x for x in self.items(group) if x['id'] == bubble.image), None)
            if item is None:
                raise ModelError('这张素材已不存在，请重新收藏。')
            path = self.safe(item['path'])
            if path.stat().st_size > self.c.max_image_bytes:
                raise ModelError('素材图片太大，请压缩后再放入素材库。')
            with path.open('rb') as source:
                data = source.read(self.c.max_image_bytes + 1)
            if len(data) > self.c.max_image_bytes:
                raise ModelError('素材图片太大，请压缩后再放入素材库。')
            image_mime(data)
            parts.append({'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(data).decode('ascii'),
                         'summary': '[' + item['label'] + ']', 'sub_type': 1 if item['kind'] == '表情包' else 0}})
        return parts

    async def collect(self, group, label, refs, images, connection):
        label = re.sub(r'[\x00-\x1f\x7f]', '', label).strip()[:60]
        if not label:
            raise ModelError('给这张图起个名字，例如：/ai 收藏表情 绷不住了，并附上图片。')
        if len(refs) != 1:
            raise ModelError('每次收藏一张图，附图或引用同群的一条图片消息都可以。')
        data_url = (await images.load(refs, connection))[0]
        data = base64.b64decode(data_url.split(',', 1)[1], validate=True)
        mime = image_mime(data)
        digest = hashlib.sha256(data).hexdigest()[:20]
        identity = 'saved_' + digest
        items = self.index(group)
        existing = next((x for x in items if x.get('id') == identity), None)
        if existing is None and len(items) >= self.c.media_max_assets:
            raise ModelError('本群素材库已满，请先用 /ai 删除表情 ID 清理一些。')
        folder = self.group_dir(group)
        folder.mkdir(parents=True, exist_ok=True)
        file = digest + MIME_EXT[mime]
        self.safe(folder / file).write_bytes(data)
        if existing is not None:
            existing['label'] = label
        else:
            items.append({'id': identity, 'label': label, 'file': file})
        self.write_index(group, items)
        return f'收好了，叫“{label}”，以后在这个群就能用了。'

    def describe(self, group):
        items = self.catalogue(group)
        if not items:
            return '素材库还是空的。发一张图并说 /ai 收藏表情 名字，或者把图片放进 assets/common。'
        return '当前可用素材：\n' + '\n'.join(f"{x['id']}  {x['label']}" for x in items)

    def find(self, group, name):
        matches = [x for x in self.items(group) if name in {x['id'], x['label']}]
        if len(matches) != 1:
            raise ModelError('没找到唯一的素材，请用 /ai 素材库 查看 ID。')
        return matches[0]

    def delete(self, group, name):
        item = self.find(group, name)
        if not item['id'].startswith('saved_'):
            raise ModelError('公共图片请在本机 assets/common 文件夹里删除。')
        items = [x for x in self.index(group) if x.get('id') != item['id']]
        self.write_index(group, items)
        self.safe(item['path']).unlink(missing_ok=True)
        return f"已删除“{item['label']}”。"
