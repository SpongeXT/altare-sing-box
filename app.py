#!/usr/bin/env python3
"""
Altare sing-box 节点 - app.py v2 (基于 v1, 入站改成 VLESS + WebSocket)
部署: Altare Docker App, Stockholm, 免费档
镜像: python:3.12-slim + 本文件 (/app/app.py), 启动命令: python app.py
数据目录: /data (Altare data folder, 重部署保留); 本地无 /data 时回落到本文件所在目录

为什么从 v1 的 VLESS + Reality 改成 VLESS + WS (本版本的核心改动):
  v1 的 Reality 走原始 TCP, 需要节点地址有公共地址才能从外网连入。
  但斯德哥尔摩节点 se-sto10 分配给应用的端口 (172.31.31.47:26941)
  在平台侧没有公共地址: A 记录域名 3 次提交都被拒
  ("This port has no public address, so a domain can't point at it"),
  刷新网络分配、新分配端口也都只拿到内网地址; 节点公开域名
  se-sto10.tryaltare.ai:26941 从用户手机网络也连不上。
  Altare 域名对话框提供的 HTTPS 类型是反向代理 + 证书、支持 WebSocket,
  由 Altare 自己终结 TLS, 不需要端口有公共地址。
  所以 v2 的入站改成 VLESS + WS (容器内不带 TLS, 由代理终结),
  监听 0.0.0.0:PORT, 路径 /vless; 客户端用 vless + ws + tls 连
  HTTPS 域名:443。Reality 入站不再提供, 旧的 Reality 链接在
  重新部署后失效, 改用新的 WS 链接。
  UUID 会保留: 已有 config.json 是 Reality 时, 启动会按旧 UUID
  重新生成 WS 配置 (Reality 密钥作废), 链接里的 UUID 不变。

与 v1 相同的做法:
  1. 日志级别 warn
  2. 守护循环: sing-box 退出后自动重起, 退避 5 / 15 / 30 / 60 秒
  3. 健康检查: 每 60 秒读 /proc/net/tcp、/proc/net/tcp6 查监听端口
  4. 路由首条加 IPv4-only 解析 (action=resolve, strategy=ipv4_only)
  5. 指定 DNS (8.8.8.8 / 1.1.1.1, sing-box 1.14 新格式 type+server,
     dns.strategy=ipv4_only, route.default_domain_resolver=google)
  6. 监听端口来自环境变量, 优先级 PORT > SERVER_PORT > 8080

部署后必须做的事:
  - 在控制台 Network 页给应用添加一个 HTTPS 类型域名
    (名称 sing-box, 被占用时自动加短后缀, 端口指主端口),
    拿到形如 sing-box-xxxx.go.tryaltare.ai 的域名。
  - 把 vless-link.txt 里的 HOST 换成那个域名 (端口固定 443),
    再导入客户端。可用环境变量 VLESS_HOST 直接指定域名,
    这样生成的链接就不用手工替换。
"""
import json
import os
import platform
import subprocess
import tarfile
import threading
import time
import urllib.request
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

# --- 按节点改这里 ---
# HOST 是 vless-link.txt 里的 HTTPS 域名占位, 部署后去控制台 Network 页
# 添加 HTTPS 类型域名, 把拿到的域名填到环境变量 VLESS_HOST (或改这里),
# 链接里的端口固定是 443 (HTTPS 公开端口), 不是容器内监听端口。
HOST = os.environ.get("VLESS_HOST", "sing-box-xxxx.go.tryaltare.ai")
NODE_NAME = "Altare-STO-WS"     # FlClash 里显示的节点名
WS_PATH = "/vless"
WS_PUBLIC_PORT = 443             # 客户端连 HTTPS 域名的端口
# --------------------------------------

SING_BOX_VERSION = "1.14.4"

# 监听端口: PORT > SERVER_PORT > 8080
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


def build_vless_link(user_uuid):
    """按当前 HOST 生成 VLESS + WS + TLS 链接。HOST 是 HTTPS 域名占位,
    部署后按控制台 Network 页添加的 HTTPS 域名替换 (端口固定 443)。
    注意: 容器内 WS 不带 TLS, TLS 由 Altare 的 HTTPS 反代终结。"""
    params = {
        "encryption": "none",
        "security": "tls",
        "sni": HOST,
        "fp": "chrome",
        "type": "ws",
        "path": WS_PATH,
        "host": HOST,
    }
    query = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params.items())
    return (f"vless://{user_uuid}@{HOST}:{WS_PUBLIC_PORT}?{query}"
            f"#{urllib.parse.quote(NODE_NAME)}")


def build_ws_config(user_uuid):
    """生成 VLESS + WS 配置 (容器内明文 WS, TLS 由 Altare HTTPS 反代终结)。
    注意: WS 入站的 user 不能带 xtls-rprx-vision 等 flow, 只能是纯 uuid。"""
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "vless",
                "tag": "vless-in",
                "listen": "0.0.0.0",
                "listen_port": PORT,
                "users": [
                    {"uuid": user_uuid}
                ],
                "transport": {
                    "type": "ws",
                    "path": WS_PATH,
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


def _config_uuid(config):
    try:
        return config["inbounds"][0]["users"][0]["uuid"]
    except (KeyError, IndexError, TypeError):
        return None


def _inbound_is_ws(config):
    """已有 config 的入站是不是 VLESS + WS。"""
    try:
        inbound = config["inbounds"][0]
    except (KeyError, IndexError, TypeError):
        return False
    return (inbound.get("type") == "vless"
            and isinstance(inbound.get("transport"), dict)
            and inbound["transport"].get("type") == "ws")


def generate_config(user_uuid=None):
    """全新服务器或从 v1 Reality 迁移: 生成 WS 配置 + vless-link.txt。
    user_uuid 为空时新生成一个; 迁移时传入旧 UUID, 链接里的 UUID 不变。
    密钥只写文件, 不打印到控制台。"""
    if not user_uuid:
        user_uuid = str(uuid.uuid4())
    config = build_ws_config(user_uuid)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    VLESS_LINK_FILE.write_text(build_vless_link(user_uuid) + "\n", encoding="utf-8")
    print("generated WS config.json and vless-link.txt", flush=True)


def rebuild_vless_link(config):
    """已有 WS config: 用 config 里的 UUID, 按当前 HOST 重建 vless-link.txt。"""
    user_uuid = _config_uuid(config)
    if not user_uuid:
        print("[node] WARNING: cannot rebuild vless link, no uuid in config", flush=True)
        return
    VLESS_LINK_FILE.write_text(build_vless_link(user_uuid) + "\n", encoding="utf-8")
    print("rebuilt vless-link.txt for current HOST (placeholder, see header)", flush=True)


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
    (和 listen) 改成当前环境端口。只改这两项, UUID 不动;
    改了端口就重建 vless-link.txt。"""
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
    只改 route.rules, UUID 不动。"""
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
    只改 dns / route.default_domain_resolver, UUID 不动。"""
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
    print("[node] WARNING: vless-link.txt uses placeholder HOST (HTTPS domain) "
          "and port 443; after deploy, add an HTTPS domain on the Altare console "
          "Network page and replace the host in the link (see header)", flush=True)
    ensure_sing_box()
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = None
        if config is not None and _inbound_is_ws(config):
            patch_log_level()
            patch_listen_port()
            patch_route_ipv4()
            patch_dns()
        else:
            # v1 的 Reality 配置 (或损坏的配置): 按旧 UUID 重新生成 WS 配置,
            # UUID 不变, Reality 密钥作废, 旧 Reality 链接失效。
            old_uuid = _config_uuid(config) if config else None
            print("[node] existing config is not VLESS+WS, regenerating as WS "
                  f"(keeping uuid={bool(old_uuid)})", flush=True)
            generate_config(old_uuid)
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
