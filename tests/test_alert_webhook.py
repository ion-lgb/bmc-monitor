"""alert-webhook 转发服务的核心逻辑测试。"""

import base64
import hashlib
import hmac
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import alert_webhook.app as webhook

# --- 消息格式化 ---


def test_build_message_firing():
    alert = {
        "status": "firing",
        "labels": {"alertname": "CpuTemperatureHigh", "instance": "192.168.0.79", "severity": "warning"},
        "annotations": {"summary": "CPU 温度过高: 192.168.0.79 92°C"},
    }
    text = webhook._build_message(alert)
    assert "告警触发" in text
    assert "CpuTemperatureHigh" in text
    assert "192.168.0.79" in text
    assert "warning" in text


def test_build_message_resolved():
    alert = {"status": "resolved", "labels": {"alertname": "X"}, "annotations": {}}
    assert "已恢复" in webhook._build_message(alert)


def test_markdown_escape():
    assert webhook._escape_markdown("a\nb") == "a\n\nb"


# --- 钉钉加签 ---


def test_dingtalk_sign(monkeypatch):
    monkeypatch.setattr(webhook.time, "time", lambda: 1700000000.0)
    monkeypatch.setattr(webhook.os, "urandom", lambda n: b"\x00" * n)
    webhook.DINGTALK_SECRET = "SEC-test"

    url = "https://oapi.dingtalk.com/robot/send?access_token=abc"
    signed = webhook._dingtalk_sign(url)
    assert "timestamp=1700000000000" in signed

    expected_string = "1700000000000\n0"
    digest = hmac.new(b"SEC-test", expected_string.encode(), hashlib.sha256).digest()
    expected_sign = __import__("urllib.parse", fromlist=["quote_plus"]).quote_plus(base64.b64encode(digest))
    assert f"sign={expected_sign}" in signed


def test_no_channels_returns_400():
    webhook.DINGTALK_URL = ""
    webhook.WECHAT_URL = ""
    with webhook.app.test_client() as c:
        r = c.post("/test")
        assert r.status_code == 400
        assert r.get_json()["ok"] is False


# --- 端到端:webhook -> 转发 -> mock 接收端 ---

class _MockHandler(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        type(self).received.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"errcode": 0}')

    def log_message(self, *args):
        pass


@pytest.fixture()
def mock_webhook_server():
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()
    thread.join(timeout=5)
    _MockHandler.received.clear()


def test_hook_forwards_to_wecom(monkeypatch, mock_webhook_server):
    webhook.DINGTALK_URL = ""
    webhook.WECHAT_URL = mock_webhook_server

    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "FanSensorCritical", "instance": "192.168.0.79"},
                "annotations": {"summary": "风扇传感器异常"},
            }
        ],
    }
    with webhook.app.test_client() as c:
        r = c.post("/hook", json=payload)
        assert r.status_code == 200
        assert r.get_json()["channels"] == 1

    assert len(_MockHandler.received) == 1
    import json

    sent = json.loads(_MockHandler.received[0])
    assert sent["msgtype"] == "markdown"
    assert "FanSensorCritical" in sent["markdown"]["content"]
