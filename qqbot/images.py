"""Download QQ image attachments into bounded, short-lived data URLs."""
import asyncio
import base64
import re
from urllib.parse import urljoin, urlsplit

import httpx

from .llm import ModelError

QQ_IMAGE_DOMAINS = ("qpic.cn", "qq.com", "qq.com.cn", "gtimg.cn", "gtimg.com", "qlogo.cn")


def allowed_url(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
                and parsed.port in {None, 80, 443} and not parsed.fragment
                and any(host == d or host.endswith("." + d) for d in QQ_IMAGE_DOMAINS))
    except (ValueError, TypeError):
        return False


def image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if bytes(data[:6]) in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ModelError("图片格式不支持，请发送 JPEG、PNG、GIF 或 WebP 图片。")


class Images:
    def __init__(self, config, client):
        self.c, self.client = config, client

    async def download(self, url):
        try:
            async with asyncio.timeout(self.c.image_timeout):
                for _ in range(4):
                    if not allowed_url(url):
                        raise ModelError("无法读取该图片：只支持 QQ 图片附件的下载地址。")
                    async with self.client.stream("GET", url, follow_redirects=False, timeout=self.c.image_timeout) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = urljoin(url, response.headers.get("location", ""))
                            continue
                        response.raise_for_status()
                        length = response.headers.get("content-length", "")
                        if length.isdigit() and int(length) > self.c.max_image_bytes:
                            raise ModelError("图片太大，请压缩后再发（默认单张最多 6 MiB）。")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > self.c.max_image_bytes:
                                raise ModelError("图片太大，请压缩后再发（默认单张最多 6 MiB）。")
                            data.extend(chunk)
                        mime = image_mime(data)
                        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        except ModelError:
            raise
        except (TimeoutError, httpx.HTTPError):
            raise ModelError("图片下载失败或已过期，请重新发送图片。") from None
        raise ModelError("图片跳转次数过多，请重新发送。")

    async def load(self, refs, connection):
        if not self.c.vision_enabled:
            raise ModelError("当前已关闭识图，请将 VISION_ENABLED 设置为 true。")
        if len(refs) > self.c.max_images:
            raise ModelError(f"单条消息最多读取 {self.c.max_images} 张图片，请分开发送。")
        result = []
        for ref in refs:
            url = ref.get("url") or ""
            # get_image may refresh a missing URL. Never open returned local paths.
            if not url:
                file = ref.get("file", "")
                if not isinstance(file, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,256}", file):
                    raise ModelError("消息中缺少可读取的 QQ 图片地址，请重新发送图片。")
                try:
                    info = await connection.call("get_image", {"file": file})
                    url = info.get("url", "") if isinstance(info, dict) else ""
                except Exception:
                    raise ModelError("获取 QQ 图片地址失败，请重新发送图片。") from None
            result.append(await self.download(url))
        return result
