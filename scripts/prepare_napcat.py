"""Prepare a local-only NapCat instance without displaying credentials."""
import json
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

root = Path(__file__).resolve().parent.parent
config = root / ".runtime" / "napcat" / "config"
config.mkdir(parents=True, exist_ok=True)
env = root / ".env"
values = dotenv_values(env)
token = values.get("ONEBOT_TOKEN") or secrets.token_urlsafe(32)
set_key(str(env), "ONEBOT_TOKEN", token)
set_key(str(env), "ONEBOT_WS_URL", "ws://127.0.0.1:3001")
onebot = config / "onebot11.json"
if not onebot.exists():
    onebot.write_text(json.dumps({
        "network": {
            "httpServers": [], "httpClients": [], "websocketClients": [],
            "websocketServers": [{
                "name": "qq-group-llm", "enable": True, "host": "127.0.0.1", "port": 3001,
                "messagePostFormat": "array", "reportSelfMessage": False, "token": token,
                "enableForcePushEvent": True, "debug": False, "heartInterval": 30000,
            }],
        },
        "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
webui = config / "webui.json"
if not webui.exists():
    webui.write_text(json.dumps({"host": "127.0.0.1", "port": 6099,
                                 "token": secrets.token_urlsafe(24), "loginRate": 10,
                                 "autoLoginAccount": "", "disableWebUI": False}, indent=2), encoding="utf-8")
print("NapCat local WebSocket and bot token prepared. No credentials printed.")
