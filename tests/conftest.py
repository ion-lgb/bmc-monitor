"""pytest 公共配置:按文件路径加载业务模块。

bmc-manager / alert-webhook 目录名不是合法的 Python 模块名(带连字符),
所以这里用 importlib 直接从文件加载,并注册成 `bmc_manager.app` /
`alert_webhook.app`,测试里照常 import。
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, rel_path: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load_module("bmc_manager", "bmc-manager/__init__.py")
_load_module("alert_webhook", "alert-webhook/__init__.py")
_load_module("bmc_manager.app", "bmc-manager/app.py")
_load_module("alert_webhook.app", "alert-webhook/app.py")
