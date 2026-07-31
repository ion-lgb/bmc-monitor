"""Alertmanager -> 钉钉/企业微信群机器人 webhook 转发服务。

Alertmanager 把告警 POST 到这个服务,这里再按 .env 里配置的
DINGTALK_WEBHOOK / WECHAT_WEBHOOK 转发。哪个配置了就发哪个,都没配就跳过。

安全说明:
  * 钉钉机器人推荐用「加签」方式创建,把 SEC 填到 DINGTALK_SECRET;
  * 企业微信机器人推荐勾选「自定义关键词」,把关键词设为「告警」。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse

import requests
from flask import Flask, jsonify, request

log = logging.getLogger("alert-webhook")

app = Flask(__name__)

DINGTALK_URL = os.environ.get("DINGTALK_WEBHOOK", "").strip()
DINGTALK_SECRET = os.environ.get("DINGTALK_SECRET", "").strip()
WECHAT_URL = os.environ.get("WECHAT_WEBHOOK", "").strip()
MAX_BYTES = 4000


def _dingtalk_sign(url: str) -> str:
    """钉钉加签:timestamp + nonce + HMAC-SHA256(secret),拼进 webhook URL。"""
    timestamp = str(round(time.time() * 1000))
    nonce = str(int.from_bytes(os.urandom(4), "big"))
    string_to_sign = f"{timestamp}\n{nonce}"
    digest = hmac.new(
        DINGTALK_SECRET.encode(), string_to_sign.encode(), hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={timestamp}&nonce={nonce}&sign={sign}"


def _notify(url: str, payload: dict, label: str) -> None:
    last_err = None
    for attempt in range(1, 4):  # 3 次重试,1s/2s 退避
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                body = r.json()
                errcode = body.get("errcode", body.get("code", 0))
                if errcode in (0, None) or errcode == 0:
                    log.info("%s 发送成功: %s", label, body)
                    return
                last_err = f"对方返回错误 {errcode}: {body}"
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(attempt)
    log.error("%s 发送失败(已重试 3 次): %s", label, last_err)


def _escape_markdown(text: str) -> str:
    # 钉钉 markdown 对 \n 不敏感,统一转成换行段
    return text.replace("\n", "\n\n")


def _build_message(alert) -> str:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    name = labels.get("alertname", "?")
    status = alert.get("status", "firing")
    if status == "resolved":
        head = "✅ 告警已恢复"
    else:
        head = "🚨 告警触发"
    lines = [
        f"### {head}: {name}",
        f"> 状态: {status}",
    ]
    if labels.get("instance"):
        lines.append(f"> 机器: {labels['instance']}")
    if labels.get("severity"):
        lines.append(f"> 级别: {labels['severity']}")
    if annotations.get("summary"):
        lines.append(f"> 摘要: {annotations['summary']}")
    if annotations.get("description"):
        lines.append(f"> 详情: {annotations['description']}")
    return "\n".join(lines)


@app.route("/hook", methods=["POST"])
def hook():
    data = request.get_json(silent=True) or {}
    alerts = data.get("alerts") or []
    if not alerts:
        return jsonify({"ok": True, "sent": 0})

    text = "\n\n".join(_build_message(a) for a in alerts)
    text = text[:MAX_BYTES]

    sent = 0
    if DINGTALK_URL:
        url = _dingtalk_sign(DINGTALK_URL) if DINGTALK_SECRET else DINGTALK_URL
        _notify(url, {"msgtype": "markdown", "markdown": {"title": "监控告警", "text": text}}, "钉钉")
        sent += 1
    if WECHAT_URL:
        _notify(WECHAT_URL, {"msgtype": "markdown", "markdown": {"content": text}}, "企业微信")
        sent += 1
    log.info("收到 %d 条告警,配置的渠道数: %d", len(alerts), sent)
    return jsonify({"ok": True, "alerts": len(alerts), "channels": sent})


@app.route("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not DINGTALK_URL and not WECHAT_URL:
        log.warning("DINGTALK_WEBHOOK 和 WECHAT_WEBHOOK 都为空:告警只会进入 Alertmanager,不会推送到群里")
    from waitress import serve

    serve(app, host="0.0.0.0", port=5001, threads=4)
