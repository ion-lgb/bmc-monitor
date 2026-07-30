"""bmc-manager —— 用网页管理 Prometheus + ipmi_exporter 要监控的 BMC 列表。

三条贯穿全文件的设计约束,改代码前先读:

1. **两个配置文件必须始终一致。** `ipmi.yml`(账号密码)和 `bmc-targets.json`
   (抓取目标)描述的是同一份"服务器清单",任何一处漏改都会让某台机器
   要么抓不到、要么留着没人用的凭据。所以本文件不提供"单独改某个文件"的
   入口——只有 `load_servers()` / `save_servers()` 这一对,后者总是把两个
   文件按同一份清单整体重写。

2. **所有写入都是原子的**(临时文件 + `os.replace`)。容器被 kill、磁盘写到
   一半断电,都不会留下半截配置文件——要么是旧的完整内容,要么是新的完整
   内容。这一点很重要:`ipmi.yml` 被写坏 = 所有服务器的监控凭据一起丢失。

3. **因为第 2 点,这两个文件必须以「目录」形式挂进容器,不能挂单个文件。**
   原子替换会产生新的 inode,而 Docker 的单文件 bind mount 绑的是 inode,
   替换之后容器里看到的永远是旧文件。docker-compose.yml 里挂的是
   `./ipmi_exporter:/config` 而不是 `./ipmi_exporter/ipmi.yml:/config/ipmi.yml`,
   就是为了这个。改挂载方式前请先想清楚这一条。
"""

import ipaddress
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field

import requests
import yaml
from flask import Flask, flash, redirect, render_template, request, url_for
from waitress import serve

log = logging.getLogger("bmc-manager")

app = Flask(__name__)
# 从环境变量取,这样容器重启后已登录页面的 flash 消息不会因为密钥变化而丢失。
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)

IPMI_CONFIG_PATH = os.environ.get("IPMI_CONFIG_PATH", "/config/ipmi.yml")
TARGETS_PATH = os.environ.get("TARGETS_PATH", "/targets/bmc-targets.json")
IPMI_EXPORTER_URL = os.environ.get("IPMI_EXPORTER_URL", "http://ipmi-exporter:9290")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
# ipmi_exporter 容器以 nobody(65534) 运行,凭据文件要给它读。
IPMI_EXPORTER_UID = int(os.environ.get("IPMI_EXPORTER_UID", "65534"))

PROMETHEUS_JOB = "ipmi"

ALL_COLLECTORS = ["ipmi", "dcmi", "bmc", "chassis", "sel"]
# 这组默认值是实测出来的:sel(事件日志)最重,BMC 的并发 session 槽位有限,
# 四个采集器已经够点亮所有面板,再加 sel 容易间歇性抓取失败。
DEFAULT_COLLECTORS = ["ipmi", "dcmi", "bmc", "chassis"]

CONFIG_HEADER = (
    "# 本文件由 bmc-manager 网页 (http://<host>:8080) 自动生成和维护,请不要手工编辑——\n"
    "# 下次在网页上增删服务器时,这里的手工修改会被程序整体重写覆盖掉。\n"
)

# 保护「读两个文件 → 改 → 写两个文件」这段临界区。waitress 是单进程多线程,
# 所以进程内的锁就够了;如果哪天换成多进程 WSGI(gunicorn -w N),这个锁会
# 失效,那时需要改成文件锁。
_write_lock = threading.Lock()


# --------------------------------------------------------------------------
# 领域模型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Server:
    """一台被监控的服务器。IP 是唯一标识(用户明确选择不额外起名字)。"""

    ip: str
    user: str
    password: str
    privilege: str = "administrator"
    driver: str = "LAN_2_0"
    collectors: list = field(default_factory=lambda: list(DEFAULT_COLLECTORS))
    authcap: bool = True

    @property
    def module_name(self) -> str:
        """ipmi.yml 里的 module 名。必须能从 IP 唯一推出,否则两个文件会对不上。"""
        return "ip_" + self.ip.replace(".", "_")

    def to_ipmi_module(self) -> dict:
        module = {
            "user": self.user,
            "pass": self.password,
            "privilege": self.privilege,
            "driver": self.driver,
            "collectors": list(self.collectors),
        }
        if self.authcap:
            # 部分厂商 BMC 上报的认证能力有 bug,freeipmi 会误判成
            # "username invalid",这个 workaround 跳过早期校验。
            module["workaround_flags"] = ["authcap"]
        return module

    def to_target_entry(self) -> dict:
        return {"targets": [self.ip], "labels": {"bmc_module": self.module_name}}

    @classmethod
    def from_ipmi_module(cls, ip: str, module: dict) -> "Server":
        return cls(
            ip=ip,
            user=module.get("user", ""),
            password=module.get("pass", ""),
            privilege=module.get("privilege", "administrator"),
            driver=module.get("driver", "LAN_2_0"),
            collectors=module.get("collectors") or [],
            authcap="authcap" in (module.get("workaround_flags") or []),
        )


def parse_ip(raw: str):
    """校验并规范化 IP。非法返回 None。

    每一个从外部拿到的 IP 都必须过这里——它同时是路径片段(module 名)、
    PromQL 字面量(purge_history)和抓取目标,任何一处没校验都是注入面。
    """
    ip = (raw or "").strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None
    return ip


# --------------------------------------------------------------------------
# 持久化:两个文件当成一份数据整体读写
# --------------------------------------------------------------------------


def _atomic_write(path: str, render, *, secret: bool = False) -> None:
    """把 render(f) 的输出原子地写到 path。

    先写同目录下的临时文件再 rename——rename 在同一文件系统内是原子的,
    读者要么看到完整的旧文件,要么看到完整的新文件,不存在中间态。
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w") as f:
            render(f)
            f.flush()
            os.fsync(f.fileno())

        if secret and os.geteuid() == 0:
            # 里面是 BMC 管理员密码(等于服务器的远程开关机+虚拟控制台权限)。
            # 交给 exporter 的 uid 独占,宿主机上其他用户读不到。
            os.chown(tmp, IPMI_EXPORTER_UID, IPMI_EXPORTER_UID)
            os.chmod(tmp, 0o600)
        else:
            # 非 root 跑(比如本机直接调试)时没法 chown,只能退回到
            # 全局可读,否则容器里的 nobody 读不到配置。
            if secret:
                log.warning("非 root 运行,%s 只能保持 0644,宿主机上任何用户都能读到密码", path)
            os.chmod(tmp, 0o644)

        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise

    # 让 rename 本身落盘,否则断电后目录项可能还是旧的。
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load_servers() -> list:
    """从两个文件还原服务器清单。

    以 targets.json 为准:只有出现在抓取目标里的机器才算数。ipmi.yml 里
    多出来的 module 视为孤儿(比如上一次写入中途失败留下的),读的时候就
    忽略,下一次 save 会把它们清掉——不让没人用的凭据留在磁盘上。
    """
    modules = {}
    if os.path.exists(IPMI_CONFIG_PATH):
        with open(IPMI_CONFIG_PATH) as f:
            modules = (yaml.safe_load(f) or {}).get("modules") or {}

    entries = []
    if os.path.exists(TARGETS_PATH):
        with open(TARGETS_PATH) as f:
            content = f.read().strip()
        entries = json.loads(content) if content else []

    servers = []
    for entry in entries:
        targets = entry.get("targets") or []
        if not targets:
            continue
        ip = targets[0]
        module_name = (entry.get("labels") or {}).get("bmc_module", "")
        module = modules.get(module_name)
        if module is None:
            # 目标在、凭据不在。保留条目让它显示在页面上(状态会是离线),
            # 用户重新填一次密码就能修好,比悄悄消失好排查。
            log.warning("目标 %s 在 targets.json 里,但 ipmi.yml 中没有对应的 %s", ip, module_name)
            module = {}
        servers.append(Server.from_ipmi_module(ip, module))

    servers.sort(key=lambda s: ipaddress.ip_address(s.ip))
    return servers


def save_servers(servers: list) -> None:
    """按同一份清单整体重写两个文件,保证它们永远一致。"""

    def render_ipmi(f):
        f.write(CONFIG_HEADER)
        yaml.safe_dump(
            {"modules": {s.module_name: s.to_ipmi_module() for s in servers}},
            f,
            allow_unicode=True,
            sort_keys=False,
        )

    def render_targets(f):
        json.dump([s.to_target_entry() for s in servers], f, indent=2, ensure_ascii=False)
        f.write("\n")

    # 先写凭据再写目标:万一第二步失败,结果是"有凭据没目标"(不抓,无害),
    # 而不是"有目标没凭据"(抓取全失败刷错误日志)。
    _atomic_write(IPMI_CONFIG_PATH, render_ipmi, secret=True)
    _atomic_write(TARGETS_PATH, render_targets)


# --------------------------------------------------------------------------
# 与 ipmi-exporter / Prometheus 交互
# --------------------------------------------------------------------------


def reload_ipmi_exporter():
    """热加载凭据。成功返回 True,否则返回给用户看的错误字符串。"""
    try:
        r = requests.post(f"{IPMI_EXPORTER_URL}/-/reload", timeout=5)
    except requests.RequestException as e:
        return f"连接 ipmi-exporter 失败: {e}"
    if r.status_code != 200:
        return f"ipmi-exporter 返回 HTTP {r.status_code}"
    return True


def test_scrape(server: Server) -> dict:
    """立刻抓一次,把结果直接告诉用户,而不是让他去 Prometheus 里等 60 秒。

    注意:这里拿不到 freeipmi 的原始 stderr(那需要把 Docker socket 挂进
    本容器,等于给这个网页近乎宿主机 root 的权限,不值得)。每个采集器的
    up/down 已经足够定位问题,所以失败时我们给的是「按可能性排序的排查方向」。
    """
    try:
        r = requests.get(
            f"{IPMI_EXPORTER_URL}/ipmi",
            params={"target": server.ip, "module": server.module_name},
            timeout=25,
        )
    except requests.RequestException as e:
        return {"ok": False, "message": f"无法连接 ipmi-exporter: {e}"}

    if r.status_code != 200:
        return {"ok": False, "message": f"ipmi-exporter 返回 HTTP {r.status_code}: {r.text[:200]}"}

    results = dict(re.findall(r'ipmi_up\{collector="([^"]+)"\}\s+(\S+)', r.text))
    if not results:
        return {"ok": False, "message": "没有采集到任何数据,BMC 可能不可达(网络不通 / IP 填错)"}

    up = sorted(k for k, v in results.items() if v == "1")
    down = sorted(k for k, v in results.items() if v != "1")

    if not up:
        return {
            "ok": False,
            "message": (
                f"所有采集器都失败了({', '.join(down)})。按可能性排序:"
                "1) 用户名/密码错;"
                "2) 权限级别不够,试试 administrator;"
                "3) driver 选错,试试切换 LAN_2_0 / LAN;"
                "4) 网络不通或 BMC 没开 IPMI-over-LAN。"
            ),
        }
    if down:
        return {
            "ok": True,
            "message": (
                f"部分采集器失败: {', '.join(down)}(正常: {', '.join(up)})。"
                "多半是这台 BMC 不支持,可以在高级选项里去掉它们。"
            ),
        }
    return {"ok": True, "message": f"采集正常,{len(up)} 个采集器全部在线: {', '.join(up)}"}


def fetch_all_status() -> dict:
    """一次查询拿到所有服务器的在线状态,返回 {ip: 'up'|'down'}。

    以前是每台机器发一次 HTTP,10 台就是 10 次串行请求(最坏 10×5s),
    页面打开时间随机器数线性增长。`max by (instance)` 让 Prometheus 在
    服务端聚合,不管多少台都只有一次往返。
    """
    try:
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": f'max by (instance) (ipmi_up{{job="{PROMETHEUS_JOB}"}})'},
            timeout=5,
        )
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("查询 Prometheus 状态失败: %s", e)
        return {}

    status = {}
    for item in payload.get("data", {}).get("result", []):
        instance = item.get("metric", {}).get("instance")
        value = item.get("value", [None, "0"])[1]
        if instance:
            status[instance] = "up" if value == "1" else "down"
    return status


def purge_history(ip: str):
    """立刻删掉这台机器的历史数据,而不只是停止采集。

    需要 Prometheus 带 --web.enable-admin-api 启动。delete_series 让查询
    立即查不到,clean_tombstones 再把数据从磁盘上压缩掉。

    注意 selector 里带了 job 限定:不加的话,将来如果给同一台机器接了别的
    exporter(node_exporter 等),这里会把那些数据一起误删。
    """
    if parse_ip(ip) is None:
        # 这个字符串会被拼进 PromQL,必须是校验过的 IP。
        return f"非法 IP: {ip!r}"

    selector = f'{{job="{PROMETHEUS_JOB}",instance="{ip}"}}'
    try:
        r = requests.post(
            f"{PROMETHEUS_URL}/api/v1/admin/tsdb/delete_series",
            params={"match[]": selector},
            timeout=10,
        )
        if r.status_code != 204:
            return f"delete_series 返回 HTTP {r.status_code}: {r.text[:200]}"
        r = requests.post(f"{PROMETHEUS_URL}/api/v1/admin/tsdb/clean_tombstones", timeout=30)
        if r.status_code != 204:
            return f"clean_tombstones 返回 HTTP {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return f"连接 Prometheus 失败: {e}"
    return True


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------


@app.route("/")
def index():
    servers = load_servers()
    status = fetch_all_status()

    editing = None
    edit_ip = parse_ip(request.args.get("edit", ""))
    if edit_ip:
        editing = next((s for s in servers if s.ip == edit_ip), None)

    rows = [
        {
            "ip": s.ip,
            "user": s.user or "?",
            "privilege": s.privilege,
            "driver": s.driver,
            "collectors": s.collectors,
            "status": status.get(s.ip, "unknown"),
        }
        for s in servers
    ]
    return render_template(
        "index.html",
        servers=rows,
        all_collectors=ALL_COLLECTORS,
        default_collectors=DEFAULT_COLLECTORS,
        editing=editing,
    )


@app.route("/servers", methods=["POST"])
def add_or_update_server():
    ip = parse_ip(request.form.get("ip", ""))
    if ip is None:
        flash(f"{request.form.get('ip', '')!r} 不是合法的 IP 地址", "error")
        return redirect(url_for("index"))

    user = request.form.get("user", "").strip()
    password = request.form.get("password", "")
    if not user:
        flash("用户名不能为空", "error")
        return redirect(url_for("index"))

    with _write_lock:
        servers = {s.ip: s for s in load_servers()}
        existing = servers.get(ip)

        # 密码留空 = 沿用原密码。否则每次只想改个采集器都得把密码重打一遍,
        # 而重打密码本身就是打错密码、把好端端的监控搞挂的主要来源。
        if not password:
            if existing is None or not existing.password:
                flash("新增服务器时必须填密码", "error")
                return redirect(url_for("index"))
            password = existing.password

        server = Server(
            ip=ip,
            user=user,
            password=password,
            privilege=request.form.get("privilege", "administrator"),
            driver=request.form.get("driver", "LAN_2_0"),
            collectors=request.form.getlist("collectors") or list(DEFAULT_COLLECTORS),
            authcap=request.form.get("authcap") == "on",
        )
        servers[ip] = server
        save_servers(list(servers.values()))

    reload_result = reload_ipmi_exporter()
    if reload_result is not True:
        flash(f"配置已保存,但重新加载 ipmi-exporter 失败: {reload_result}", "error")
        return redirect(url_for("index"))

    result = test_scrape(server)
    verb = "更新" if existing else "添加"
    flash(f"已{verb} {ip}。测试结果: {result['message']}", "ok" if result["ok"] else "error")
    return redirect(url_for("index"))


@app.route("/servers/<ip>/delete", methods=["POST"])
def delete_server(ip):
    # 这个 ip 来自 URL,后面要拼进 PromQL 的删除语句,必须先校验。
    ip = parse_ip(ip)
    if ip is None:
        flash("非法的 IP 地址", "error")
        return redirect(url_for("index"))

    with _write_lock:
        servers = [s for s in load_servers() if s.ip != ip]
        save_servers(servers)

    reload_result = reload_ipmi_exporter()
    if reload_result is not True:
        flash(f"{ip} 已删除,但重新加载 ipmi-exporter 失败: {reload_result}", "error")
        return redirect(url_for("index"))

    purge_result = purge_history(ip)
    if purge_result is not True:
        flash(f"{ip} 已停止采集,但清空历史数据失败: {purge_result}", "error")
    else:
        flash(f"已删除 {ip},历史数据也已清空", "ok")
    return redirect(url_for("index"))


@app.route("/healthz")
def healthz():
    """给 docker healthcheck 用:能读到配置就算健康。"""
    try:
        load_servers()
    except Exception as e:  # noqa: BLE001 - healthcheck 要把任何异常都算成不健康
        return {"status": "error", "detail": str(e)}, 500
    return {"status": "ok"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # 用 waitress 而不是 app.run():后者是开发服务器,不该长期跑在后台服务里。
    serve(app, host="0.0.0.0", port=8080, threads=8)
