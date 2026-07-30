"""在 ESXi 上启用并配置 SNMP v3(供 Telegraf 采集用)。

只做加法:创建一个专用的只读 SNMP v3 用户并打开 161 端口。
回滚:esxcli system snmp set --enable false
"""

import os
import sys

import paramiko

HOST = os.environ["ESXI_HOST"]
ESXI_PW = os.environ["ESXI_PW"]
USER = os.environ["SNMP_USER"]
AUTH = os.environ["SNMP_AUTH_PASS"]
PRIV = os.environ["SNMP_PRIV_PASS"]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username="root", password=ESXI_PW, timeout=15,
          look_for_keys=False, allow_agent=False)


def run(cmd, show=None):
    _, o, e = c.exec_command(cmd, timeout=30)
    out = (o.read().decode() + e.read().decode()).strip()
    print("  $ " + (show or cmd))
    for line in (out or "(ok)").splitlines():
        print("      " + line)
    return out


# 注意顺序:必须先设定认证/加密算法,hash 命令才知道该用哪种算法,
# 否则会报 "Must specify set authentication protocol via set --authentication"。
print("=== 1. 设置认证 / 加密算法 ===")
run("esxcli system snmp set --authentication SHA1")
run("esxcli system snmp set --privacy AES128")

print("=== 2. 生成密码哈希 ===")
h = run(
    "esxcli system snmp hash --auth-hash '%s' --priv-hash '%s' --raw-secret" % (AUTH, PRIV),
    show="esxcli system snmp hash --auth-hash *** --priv-hash *** --raw-secret",
)
auth_hash = priv_hash = None
for line in h.splitlines():
    if "Authhash" in line:
        auth_hash = line.split(":", 1)[1].strip()
    elif "Privhash" in line:
        priv_hash = line.split(":", 1)[1].strip()

if not (auth_hash and priv_hash):
    print("  !! 未能解析出哈希,已中止,ESXi 未做任何改动")
    c.close()
    sys.exit(1)

print("=== 3. 创建 v3 用户 ===")
run("esxcli system snmp set --users %s/%s/%s/priv" % (USER, auth_hash, priv_hash),
    show="esxcli system snmp set --users %s/<authhash>/<privhash>/priv" % USER)

print("=== 4. 启用 SNMP ===")
run("esxcli system snmp set --enable true")

print("=== 5. 放行防火墙 ===")
run("esxcli network firewall ruleset set --ruleset-id snmp --enabled true")

print("=== 6. 最终状态 ===")
run("esxcli system snmp get")

c.close()
