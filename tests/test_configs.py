"""全仓库配置校验:YAML/JSON 可解析、Grafana 看板 uid 不重复、docker-compose 合法。"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# 运行时生成 / 含敏感信息的文件,不属于提交内容,跳过
GITIGNORED = {
    ".env",
    "ipmi_exporter/ipmi.yml",
    "prometheus/targets/bmc-targets.json",
    ".snmp-creds",
}


def _all_config_files():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if ".git" in rel.parts or "tests" in rel.parts or p.name in GITIGNORED:
            continue
        if p.suffix in (".yml", ".yaml", ".json"):
            yield rel, p


def test_yaml_and_json_parse():
    for rel, p in _all_config_files():
        with p.open(encoding="utf-8") as f:
            if p.suffix in (".yml", ".yaml"):
                yaml.safe_load(f)
            else:
                json.load(f)
        # 走到这里说明解析成功


def test_grafana_dashboard_uids_unique():
    uids = []
    for rel, p in _all_config_files():
        if "dashboards" in rel.parts and rel.suffix == ".json":
            doc = json.loads(p.read_text(encoding="utf-8"))
            assert "title" in doc and "uid" in doc, f"{rel} 不是合法的 Grafana 看板"
            uids.append((rel, doc["uid"]))
    seen = [uid for _, uid in uids]
    assert len(seen) == len(set(seen)), f"看板 uid 重复: {uids}"


def test_docker_compose_valid():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert "prometheus" in services
    assert "alertmanager" in services
    assert "alert-webhook" in services
    assert "bmc-manager" in services
