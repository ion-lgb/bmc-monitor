"""bmc-manager 核心逻辑测试:IP 校验、序列化往返、双文件一致性。"""

import json

import pytest
import yaml

import bmc_manager.app as manager


def test_parse_ip_valid():
    assert manager.parse_ip(" 192.168.0.79 ") == "192.168.0.79"
    assert manager.parse_ip("::1") == "::1"


def test_parse_ip_invalid():
    assert manager.parse_ip("not-an-ip") is None
    assert manager.parse_ip("1.2.3.4.5") is None
    assert manager.parse_ip("") is None
    assert manager.parse_ip('192.168.0.79"; rm -rf /') is None


def test_module_name_roundtrip():
    s = manager.Server(ip="192.168.0.79", user="admin", password="secret")
    assert s.module_name == "ip_192_168_0_79"
    # ipmi.yml module -> Server -> targets.json 条目,再反解,必须无损
    restored = manager.Server.from_ipmi_module("192.168.0.79", s.to_ipmi_module())
    assert restored.ip == s.ip
    assert restored.user == s.user
    assert restored.password == s.password
    assert restored.driver == s.driver
    assert restored.collectors == s.collectors
    assert restored.authcap == s.authcap


def test_authcap_flag_roundtrip():
    s = manager.Server(ip="10.0.0.1", user="u", password="p", authcap=False)
    m = s.to_ipmi_module()
    assert "workaround_flags" not in m
    assert manager.Server.from_ipmi_module("10.0.0.1", m).authcap is False

    s2 = manager.Server(ip="10.0.0.1", user="u", password="p", authcap=True)
    assert manager.Server.from_ipmi_module("10.0.0.1", s2.to_ipmi_module()).authcap is True


def test_save_servers_keeps_both_files_consistent(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "IPMI_CONFIG_PATH", str(tmp_path / "ipmi.yml"))
    monkeypatch.setattr(manager, "TARGETS_PATH", str(tmp_path / "targets.json"))
    # 非 root 场景走 0644 分支
    monkeypatch.setattr(manager.os, "geteuid", lambda: 1000)

    servers = [
        manager.Server(ip="192.168.0.79", user="admin", password="s3cret"),
        manager.Server(ip="10.1.2.3", user="root", password="pw", authcap=False, collectors=["ipmi"]),
    ]
    manager.save_servers(servers)

    ipmi = yaml.safe_load((tmp_path / "ipmi.yml").read_text())
    targets = json.loads((tmp_path / "targets.json").read_text())
    assert set(ipmi["modules"]) == {"ip_192_168_0_79", "ip_10_1_2_3"}

    for t in targets:
        module = ipmi["modules"][t["labels"]["bmc_module"]]
        server = manager.Server.from_ipmi_module(t["targets"][0], module)
        assert server in servers

    # 加载回来也必须和写入前一致(load_servers 按 IP 排序,这里同样排序比较)
    assert manager.load_servers() == sorted(servers, key=lambda s: s.ip)
