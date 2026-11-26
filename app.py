#!/usr/bin/env python3
"""
Altare sing-box 节点 - app.py v1 (基于 Nodaroo DE3 v2.2)
部署: Altare Docker App, Stockholm, 免费档
镜像: python:3.12-slim + 本文件 (/app/app.py), 启动命令: python app.py
数据目录: /data (Altare data folder, 重部署保留); 本地无 /data 时回落到本文件所在目录

与 DE3 v2.2 相同的做法:
  1. 日志级别 warn (已有 config.json 只改 log level, UUID / 密钥 / Reality 不动)
  2. 守护循环: sing-box 退出后自动重起, 退避 5 / 15 / 30 / 60 秒
  3. 健康检查: 每 60 秒读 /proc/net/tcp、/proc/net/tcp6 查监听端口
  4. 路由首条加 IPv4-only 解析 (action=resolve, strategy=ipv4_only),
     容器没有 IPv6 路由; 已有 config.json 也会在启动时补上
  5. 指定 DNS (8.8.8.8 / 1.1.1.1, sing-box 1.14 新格式 type+server,
     dns.strategy=ipv4_only, route.default_domain_resolver=google)

Altare 与 Nodaroo 的不同 (重要, 先读完再部署):
  a. 监听端口来自环境变量, 优先级 PORT > SERVER_PORT > 8080。
     - Altare Docker 向导的 Port 字段就是应用监听的端口, 启动命令里的
       $PORT 会被填入; Altare 文档 "Connecting to servers" 则说应用应从
       SERVER_PORT 环境变量读主端口 ("Read the port from the SERVER_PORT
       environment variable rather than hard-coding it")。
       两个来源都支持, 以防向导与文档口径不一致。
     - 已有 config.json 时, 启动会把 inbound listen_port 改成当前环境端口
       (只改 listen_port / listen, UUID / 密钥 / Reality 不动), 并重建
       vless-link.txt; 端口变了节点链接也要跟着换。
  b. vless 链接里的 HOST:外部端口在容器内无法得知, 必须部署后去控制台补填:
     - Altare 给每个应用分配一个主端口 (primary port), 客户端连的是节点地址,
       形如 <node>.tryaltare.ai:<assigned-port> (loong 当时是
       sg-sgp12.tryaltare.ai:26567); 另外还有 go.altare.global 的 HTTPS 域名
       (如 sing-box.go.altare.global), 按 Altare 文档 "Connecting to servers",
       A 记录域名加端口 (name.go.altare.global:port) 对任意协议都可用,
       但 HTTPS 前端会终结 TLS, 不能直接走 VLESS Reality 的原始 TCP。
     - 所以 vless-link.txt 里写的 HOST 默认是 sing-box.go.altare.global
       (应用名就是 sing-box 时的域名猜测), 端口是容器内监听端口, 两者都只是
       占位: 部署后务必去控制台 Network 页抄下分配到的 <node host>:<port>,
       把链接里的 host:port 换成那个, 再导入客户端。可用环境变量 VLESS_HOST
       覆盖 HOST (例如设成分配到的节点 host)。
     - 本文件不保证 go.altare.global 域名 + 内部端口能直连, 这点部署后必须实测。
  c. 配置文件没有上传入口: Altare 新建向导 1-4 步没有任何文件/配置上传控件,
     所以配置必须由本文件在容器首次启动时生成 (UUID + Reality 密钥对),
     凭据只写在 /data 里的 config.json / vless-link.txt, 不打印到控制台,
     本文件里不含任何 UUID、密钥、vless 链接。
  d. 只有 TCP 流量能到应用, linux/amd64, 无宿主机 root 权限, 与 Nodaroo 一致
     的是 VLESS + Reality over TCP, 不用 UDP。

全新服务器首次运行还会:
  - 自动下载 sing-box 1.14.4 (linux amd64/arm64, 按机器架构选)
  - 用 sing-box 生成 UUID 和 Reality 密钥对, 生成 config.json 和 vless-link.txt
  - 另外写 reality-public.json (只含 public_key 和 short_id, 都是公开值,
    不含私钥、不含 UUID), 供端口变化时重建 vless 链接用
"""
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

# --- 按节点改这里 ---
# HOST 只是 vless-link.txt 里的占位域名, 见文件头说明 (b): 部署后必须按控制台
# Network 页分配到的 <node host>:<port> 手工替换链接里的 host:port。
# 可用环境变量 VLESS_HOST 覆盖 (例如设成分配到的节点 host)。
HOST = os.environ.get("VLESS_HOST", "sing-box.go.altare.global")
NODE_NAME = "Altare-STO"         # FlClash 里显示的节点名, 可改 (STO = Stockholm)
# --------------------------------------

SING_BOX_VERSION = "1.14.4"
SNI = "www.microsoft.com"

# 监听端口: PORT > SERVER_PORT > 8080 (见文件头说明 a)
def _resolve_port():
    for var in ("PORT", "SERVER_PORT"):
        raw = os.environ.get(var)
        if raw:
            try:
                port = int(raw)
            except ValueError:
                print(f"[node] WARNING: env {var}={raw!r} is not a number, ignoring", flush=True)
                continue
            if 1 <= port <= 65535:
                print(f"[node] listen port {port} from env {var}", flush=True)
                return port
            print(f"[node] WARNING: env {var}={port} out of range 1-65535, ignoring", flush=True)
    print("[node] no PORT/SERVER_PORT env, using fallback 8080", flush=True)
    return 8080

PORT = _resolve_port()

# 数据目录: /data (Altare data folder, 重部署保留); NODE_BASE 仅供本地测试覆盖;
# /data 不存在或不可写时回落到本文件所在目录。
if os.environ.get("NODE_BASE"):
    BASE = Path(os.environ["NODE_BASE"]).expanduser().resolve()
else:
    _data = Path("/data")
    if _data.is_dir() and os.access(_data, os.W_OK):
        BASE = _data
    else:
        BASE = Path(__file__).resolve().parent
BASE.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = BASE / "config.json"
SING_BOX_BIN = BASE / "sing-box"
MONITOR_LOG = BASE / "monitor.log"
VLESS_LINK_FILE = BASE / "vless-link.txt"
# 只含公开值 (public_key, short_id) 的 sidecar, 供端口变化时重建链接用;
# 不含私钥, 不含 UUID。
REALITY_PUBLIC_FILE = BASE / "reality-public.json"
BACKOFFS = [5, 15, 30, 60]    # 秒, 到 60 封顶
CHECK_INTERVAL = 60           # 秒
MAX_MISSES = 2


def now():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def monitor_log(msg):
    line = f"[{now()}] {msg}"
    try:
        with open(MONITOR_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def run_capture(args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def ensure_sing_box():
    if SING_BOX_BIN.exists() and os.access(SING_BOX_BIN, os.X_OK):
        return
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    url = (
        f"https://github.com/SagerNet/sing-box/releases/download/"
        f"v{SING_BOX_VERSION}/sing-box-{SING_BOX_VERSION}-linux-{arch}.tar.gz"
    )
    print(f"sing-box not found, downloading {url}", flush=True)
    tar_path = BASE / "sing-box.tar.gz"
    urllib.request.urlretrieve(url, tar_path)
    with tarfile.open(tar_path, "r:gz") as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith("/sing-box"))
        tf.extract(member, BASE, filter="data")
        extracted = BASE / member.name
        extracted.rename(SING_BOX_BIN)
    tar_path.unlink(missing_ok=True)
    SING_BOX_BIN.chmod(0o755)
    print(f"sing-box {SING_BOX_VERSION} downloaded", flush=True)


def build_vless_link(user_uuid, public_key, short_id):
    """按当前 HOST / PORT 生成 vless 链接。注意: 这里的 HOST:PORT 只是占位
    (见文件头说明 b), 部署后必须按控制台 Network 页分配的地址替换。"""
    params = {
        "encryption": "none",
        "flow": "xtls-rprx-vision",
        "security": "reality",
        "sni": SNI,
        "fp": "chrome",
        "pbk": public_key,
        "sid": short_id,
        "type": "tcp",
        "headerType": "none",
    }
    query = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params.items())
    return f"vless://{user_uuid}@{HOST}:{PORT}?{query}#{urllib.parse.quote(NODE_NAME)}"


def _read_reality_public():
    """从 reality-public.json 或已有 vless-link.txt 里读回 public_key / short_id。
    两者都是公开值; 读不到时返回 (None, None)。"""
    try:
        meta = json.loads(REALITY_PUBLIC_FILE.read_text(encoding="utf-8"))
        if meta.get("public_key") and meta.get("short_id"):
            return meta["public_key"], meta["short_id"]
    except (OSError, ValueError):
        pass
    try:
        link = VLESS_LINK_FILE.read_text(encoding="utf-8").strip()
        qs = urllib.parse.urlparse(link).query
        params = urllib.parse.parse_qs(qs)
        pbk = params.get("pbk", [None])[0]
        sid = params.get("sid", [None])[0]
        if pbk and sid:
            return pbk, sid
    except (OSError, ValueError):
        pass
    return None, None


def rebuild_vless_link(config):
    """已有 config: 用 config 里的 UUID + 公开的 pbk/sid, 按当前 HOST / PORT
    重建 vless-link.txt。UUID / 密钥不动。"""
    try:
        user_uuid = config["inbounds"][0]["users"][0]["uuid"]
    except (KeyError, IndexError, TypeError):
        print("[node] WARNING: cannot rebuild vless link, no uuid in config", flush=True)
        return
    public_key, short_id = _read_reality_public()
    if not public_key or not short_id:
        print("[node] WARNING: cannot rebuild vless link, no public_key/short_id "
              "(reality-public.json and vless-link.txt both missing them)", flush=True)
        return
    VLESS_LINK_FILE.write_text(build_vless_link(user_uuid, public_key, short_id) + "\n",
                               encoding="utf-8")
    print("rebuilt vless-link.txt for current HOST/PORT (placeholder, see header b)",
          flush=True)


def generate_config():
    """全新服务器: 生成 UUID + Reality 密钥对, 写 config.json 和 vless-link.txt.
    密钥只写文件, 不打印到控制台。"""
    user_uuid = str(uuid.uuid4())
    out = run_capture([str(SING_BOX_BIN), "generate", "reality-keypair"])
    private_key = re.search(r"PrivateKey:\s*(\S+)", out.stdout).group(1)
    public_key = re.search(r"PublicKey:\s*(\S+)", out.stdout).group(1)
    short_id = secrets.token_hex(4)  # 8 位 hex

    config = {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "vless",
                "tag": "vless-in",
                "listen": "0.0.0.0",
                "listen_port": PORT,
                "users": [
                    {"uuid": user_uuid, "flow": "xtls-rprx-vision"}
                ],
                "tls": {
                    "enabled": True,
                    "server_name": SNI,
                    "reality": {
                        "enabled": True,
                        "handshake": {"server": SNI, "server_port": 443},
                        "private_key": private_key,
                        "short_id": [short_id],
                    },
                },
            }
        ],
        "dns": {
            "servers": [
                {"type": "udp", "tag": "google", "server": "8.8.8.8"},
                {"type": "udp", "tag": "cloudflare", "server": "1.1.1.1"},
            ],
            "final": "google",
            "strategy": "ipv4_only",
        },
        "outbounds": [
            {"type": "direct", "tag": "direct"},
        ],
        # sing-box 1.13+ 移除了 inbound 里的 sniff / sniff_override_destination
        # 等旧字段, 嗅探改成 route 规则 action=sniff (见 sing-box migration 文档)
        "route": {
            "default_domain_resolver": "google",
            "rules": [
                {"action": "resolve", "strategy": "ipv4_only"},
                {"action": "sniff"},
            ],
            "final": "direct",
        },
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    REALITY_PUBLIC_FILE.write_text(
        json.dumps({"public_key": public_key, "short_id": short_id}, indent=2),
        encoding="utf-8")

    VLESS_LINK_FILE.write_text(build_vless_link(user_uuid, public_key, short_id) + "\n",
                               encoding="utf-8")
    print("generated new config.json and vless-link.txt", flush=True)


def patch_log_level():
    """已有 config.json: 只把 log level 改成 warn, 其余不动。"""
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    log = config.setdefault("log", {})
    if log.get("level") != "warn":
        log["level"] = "warn"
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
        print("patched existing config.json log level to warn", flush=True)
    else:
        print("config.json log level already warn", flush=True)


def patch_listen_port():
    """已有 config.json: Altare 分配的端口可能变化, 把 inbound listen_port
    (和 listen) 改成当前环境端口。只改这两项, UUID / 密钥 / Reality 不动;
    改了端口就重建 vless-link.txt (HOST:PORT 仍是占位, 见文件头说明 b)。"""
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        inbound = config["inbounds"][0]
    except (KeyError, IndexError, TypeError):
        return
    changed = False
    if inbound.get("listen") != "0.0.0.0":
        inbound["listen"] = "0.0.0.0"
        changed = True
    if inbound.get("listen_port") != PORT:
        print(f"patched existing config.json listen_port "
              f"{inbound.get('listen_port')} -> {PORT}", flush=True)
        inbound["listen_port"] = PORT
        changed = True
    if not changed:
        print(f"config.json listen_port already {PORT}", flush=True)
        return
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    rebuild_vless_link(config)


def patch_route_ipv4():
    """已有 config.json: 路由规则首条加上 IPv4-only 解析, 避免先拨 IPv6 失败。
    只改 route.rules, UUID / 密钥 / Reality 不动。"""
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    route = config.setdefault("route", {})
    rules = route.setdefault("rules", [])
    if any(isinstance(r, dict) and r.get("action") == "resolve"
           and r.get("strategy") == "ipv4_only" for r in rules):
        print("route already has ipv4_only resolve rule", flush=True)
        return
    rules.insert(0, {"action": "resolve", "strategy": "ipv4_only"})
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("patched existing config.json route with ipv4_only resolve rule", flush=True)


DNS_CONFIG = {
    "servers": [
        {"type": "udp", "tag": "google", "server": "8.8.8.8"},
        {"type": "udp", "tag": "cloudflare", "server": "1.1.1.1"},
    ],
    "final": "google",
    "strategy": "ipv4_only",
}


def patch_dns():
    """已有 config.json: 补上指定 DNS + route.default_domain_resolver。
    sing-box 1.14 已移除旧 address 格式, 必须用新格式 (type+server);
    没配 default_domain_resolver 时 1.14 会直接拒绝启动。
    只改 dns / route.default_domain_resolver, UUID / 密钥 / Reality 不动。"""
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    changed = False
    if config.get("dns") != DNS_CONFIG:
        config["dns"] = DNS_CONFIG
        changed = True
    route = config.setdefault("route", {})
    if route.get("default_domain_resolver") != "google":
        route["default_domain_resolver"] = "google"
        changed = True
    if changed:
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
        print("patched existing config.json dns to 8.8.8.8/1.1.1.1 + default_domain_resolver google", flush=True)
    else:
        print("config.json dns already set", flush=True)


def is_port_listening(port):
    """读 /proc/net/tcp、/proc/net/tcp6, 找 state 0A (LISTEN) 的端口。
    不建 TCP 连接, 不产生 sing-box 日志。"""
    want = f"{port:04X}"
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[1]
            state = parts[3]
            if ":" in local and local.rsplit(":", 1)[1].upper() == want and state == "0A":
                return True
    return False


def monitor_loop(state):
    monitor_log(f"[monitor] monitor started, checking port {PORT} every {CHECK_INTERVAL}s")
    misses = 0
    while True:
        time.sleep(CHECK_INTERVAL)
        proc = state.get("proc")
        if proc is None or proc.poll() is not None:
            misses = 0
            continue
        if is_port_listening(PORT):
            misses = 0
            continue
        misses += 1
        monitor_log(f"[monitor] port {PORT} not listening ({misses}/{MAX_MISSES})")
        if misses >= MAX_MISSES:
            monitor_log(f"[monitor] port {PORT} missing {MAX_MISSES}x, restarting sing-box")
            try:
                proc.kill()
            except OSError:
                pass
            misses = 0


def main():
    print("[node] starting", flush=True)
    print(f"[node] {NODE_NAME} 0.0.0.0:{PORT} sing-box {SING_BOX_VERSION} base={BASE}", flush=True)
    print("[node] WARNING: vless-link.txt uses placeholder HOST and the internal "
          "listen port; after deploy, replace its host:port with the assigned "
          "address from the Altare console Network page (see header b)", flush=True)
    ensure_sing_box()
    if CONFIG_PATH.exists():
        patch_log_level()
        patch_listen_port()
        patch_route_ipv4()
        patch_dns()
    else:
        generate_config()

    state = {"proc": None}
    threading.Thread(target=monitor_loop, args=(state,), daemon=True).start()

    restart_count = 0
    while True:
        print("sing-box starting", flush=True)
        proc = subprocess.Popen(
            [str(SING_BOX_BIN), "run", "-c", str(CONFIG_PATH)],
            cwd=str(BASE),
        )
        state["proc"] = proc
        rc = proc.wait()
        state["proc"] = None
        backoff = BACKOFFS[min(restart_count, len(BACKOFFS) - 1)]
        monitor_log(f"[node] sing-box exited rc={rc}, restarting in {backoff}s")
        restart_count += 1
        time.sleep(backoff)


if __name__ == "__main__":
    main()
