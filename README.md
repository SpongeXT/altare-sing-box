# altare-sing-box

Altare (Stockholm, 免费档) 上跑 sing-box 的自定义镜像, 做 VPN 节点用。

## 为什么需要自定义镜像

- Altare Docker 部署没有上传 `config.json` 的入口, 官方 `sing-box` 镜像本身
  也没有配置文件, 裸镜像部署起来就会崩。
- 所以这个镜像用 `python:3.12-slim` + `app.py`: 容器首次启动时下载
  sing-box 1.14.4, 生成 UUID + Reality 密钥对, 写到 `/data/config.json`
  (Altare data folder, 重部署保留)。凭据只写在容器里, 不打印到控制台,
  仓库里不含任何 UUID、密钥、vless 链接。

## 协议

VLESS + Reality over TCP, SNI `www.microsoft.com`, flow `xtls-rprx-vision`,
DNS 固定 8.8.8.8 / 1.1.1.1 (`default_domain_resolver: google`), 路由首条
IPv4-only 解析, 日志 warn, sing-box 退出自动重起 + 每 60 秒健康检查
(与 Nodaroo DE3 v2.2 同一套逻辑, 见 `app.py` 文件头)。

## 构建

GitHub Actions (`.github/workflows/build.yml`, 与
`SpongeXT/kasm-minimal-nossl` 同一套做法, 标准 `GITHUB_TOKEN` 登录 GHCR):

- 仓库应建为 `SpongeXT/altare-sing-box`, 默认分支 `main`
- 推送到 `main` 即构建 `linux/amd64` 并推到
  `ghcr.io/spongext/altare-sing-box:latest` (另打一个 `${{ github.sha }}` 标签)
- 注意: GHCR 新建包默认可能是私有, Altare 拉取前要把包设为公开,
  或者在 Altare 里开 Private registry 填登录信息 (未验证, 部署时确认)。

## 在 Altare 上部署

1. 控制台 Apps → New app → Docker image, 镜像填
   `ghcr.io/spongext/altare-sing-box:latest`, 点 Check。
2. 区域选 Stockholm (2026-11-27 05:11 核查时唯一有免费容量的地区,
   容量是实时快照, 会变), 名字填 `sing-box`。
3. Port 字段填应用监听的端口 (向导里 8080 可接受; 也可以留空走默认)。
   启动命令留空即可 (镜像默认 `python app.py`); 如需覆盖, 记得命令里
   可以用 `$PORT`。
4. Data folder 保持 `/data`。创建后资源会占满团队免费额度
   (4 GiB / 125% / 40 GiB)。
5. 创建后去应用的 Network 页抄下分配到的地址
   (形如 `<node>.tryaltare.ai:<assigned-port>`, loong 当时是
   `sg-sgp12.tryaltare.ai:26567`)。

## 部署后必须做的一步: 补填 vless 链接

`app.py` 在容器内不知道外部 host:端口, 所以 `/data/vless-link.txt` 里
的 HOST 默认是 `sing-box.go.altare.global`, 端口是容器内监听端口,
两者都只是占位。部署后:

1. 从控制台 Network 页抄下分配到的 `<node host>:<port>`。
2. 用 SFTP / 控制台文件页打开 `/data/vless-link.txt` (和 `config.json`,
   凭据只在容器里), 把链接里的 `host:port` 换成分配到的地址。
3. 导入客户端实测连通。注意 `go.altare.global` 的 HTTPS 域名会终结 TLS,
   不能直接当 VLESS Reality 的原始 TCP 地址用; 要用节点 host:端口,
   或 A 记录域名加端口的形式 (见 Altare 文档 "Connecting to servers")。

## 待验证 (部署时确认)

- GHCR 包的公开/私有状态, 以及 Altare 是否需要 Private registry 登录。
- 向导 Port 字段的值与 `SERVER_PORT` / `$PORT` 的实际对应关系
  (app.py 按 PORT > SERVER_PORT > 8080 解析, 覆盖了两种口径)。
- 分配到的外部端口是否等于容器内监听端口, 还是另行分配
  (若另行分配, `app.py` 的端口补丁逻辑只改内部端口, 链接仍需手工替换)。
- 只有 TCP 能到应用, UDP 不可用; Reality over TCP 已按此设计。
