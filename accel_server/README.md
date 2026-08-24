# accel_server.py — DNS 与 HTTP 加速服务（hosts 模式的进阶）

在 `hosts_accel.py` 的 hosts 模式之外，提供两种**本地常驻服务**，复用同一套域名清单与优选 IP：覆盖 hosts 做不到的场景（通配子域 / 命令行 git / 只想加速浏览器），且**全程无 MITM**——不装根证书、不解密任何流量。

**与 Steam++（Watt Toolkit）的区别**：Steam++ 做 HTTPS 中间人代理（安装自签根证书解密流量）；本工具只做「DNS 应答优化 + 直连优选 IP」，**看不到你的 HTTPS 明文**。

---

## 环境要求
- Windows / Linux / macOS（系统代理自动设置仅 Windows 生效）
- Python 3.10+（`str | None` 语法），**零第三方依赖**（仅标准库）
- 必须与 `hosts_accel.py` 放在**同一目录**（运行时导入它的域名清单）

## 快速开始

### DNS 加速（覆盖所有走系统 DNS 的程序，含通配子域）
```powershell
cd D:\文档\workbuddy\Daliy\hosts

# 1) 启动本地 DNS 服务（默认 5353 端口）
python .\accel_server.py dns

# 2) 把系统网络适配器的 DNS 指向 127.0.0.1:5353（或用代理工具/路由器指向它）
#    —— 想让浏览器和 git 都吃到加速，系统 DNS 就设成它
```

### HTTP 加速（只加速配了代理的程序，典型=浏览器）
```powershell
# 1) 启动代理（默认 8080，会自动把系统代理设成本地代理，退出时自动还原）
python .\accel_server.py proxy

# 2) 不想自动改系统代理时（自己手动在浏览器里配）
python .\accel_server.py proxy --no-set-proxy
```

两种模式都 **Ctrl+C 停止**（proxy 模式停止时会还原系统代理）。

## 命令一览

| 命令 | 说明 |
|---|---|
| `python .\accel_server.py dns` | 本地智能 DNS：加速域名（含 `*.子域` 通配）直接返回优选 IP，其余转发上游 |
| `dns --port 5353` | 指定端口（默认 5353） |
| `dns --upstream 223.5.5.5` | 指定上游 DNS（默认 223.5.5.5 阿里 DNS） |
| `python .\accel_server.py proxy` | HTTP 正向代理：加速域名直连优选 IP；HTTPS 走 CONNECT 隧道（不解密） |
| `proxy --port 8080` | 指定端口（默认 8080） |
| `proxy --no-set-proxy` | 跳过自动设置系统代理（自己手动配） |
| 全局参数 | 两个模式共用：`--port`、`--upstream`（仅 dns）、`--no-set-proxy`（仅 proxy） |

## 系统代理自动设置（proxy 模式，仅 Windows）
- **启动时**：自动把系统代理写成 `127.0.0.1:端口`（回绕地址 `localhost;127.0.0.1;<local>` 直连不代理），并通知浏览器立即生效；
- **退出时**（Ctrl+C）：自动还原你原来的代理设置；
- **失败时**（无权限/注册表异常）：控制台 + **Windows 弹窗**双重提示，告诉你手动配置或换管理员运行；
- 设置/还原前后会**读取你的原代理配置并完整还原**，不会覆盖你的既有设置。

## 配置：优选 IP（可选）
与 `hosts_accel.py` **共用**同目录 `ips.conf`（每行 `域名=IP`），也支持 `hosts_accel.py start --ip 域名=IP` 写入的覆盖。启动时优先用覆盖值，否则取当前 DNS 解析结果。

```
# ips.conf 示例（与 hosts_accel.py 共用）
github.com=140.82.112.4
raw.githubusercontent.com=185.199.108.133
```

## 加速域名清单（21 个，与 hosts_accel.py 同一份）
- **GitHub 主域**：github.com、github.dev、api.github.com、github.githubassets.com、uploads.github.com、githubapp.com、github.io、codeload.github.com、collector.github.com
- **GitHub 内容子域**：raw / avatars / camo / objects / release-assets `.githubusercontent.com`
- **GitHub 功能域**：education、resources、archiveprogram、pipelines.actions（Actions）
- **开发者生态**：hub.docker.com、huggingface.co、greasyfork.org

## 工作原理

**DNS 模式**：收到查询 → 解析 qname → 命中加速域名（含 `*.子域` 通配，最长前缀优先）→ 直接回**全部健康优选 IP（多条 A 记录）**（A/ANY），AAAA 回空；未命中 → 原样转发上游 DNS（默认 223.5.5.5，上游不可达回 SERVFAIL）。

**Proxy 模式**：加速域名 → **`DirectConn` 用优选 IP 直连，Host 头保持原始域名**（支持虚拟主机，且强制 IPv4 避开 IPv6 慢路径）；HTTPS 请求 → CONNECT 隧道，双向泵数据但**不做中间人、不解密**；响应**流式转发**（边读上游边 flush），chunked/无 Content-Length 的大响应也不会卡死。

**多 IP 回退（稳定性关键）**：启动时对每个域名的候选 IP（本地 DNS + 内置 GitHub 官方 IP 池）并发做 **TCP 443 健康检测**，按延迟升序排序，淘汰当前不通的 IP；请求时**逐个尝试**可用 IP，第一个挂了自动换下一个——不会再因单一优选 IP 抖动而整体失败。DNS 模式回包给出多条 A 记录，浏览器/系统自带 failover。

## ⚠️ 重要注意事项
1. **DNS 模式端口**：Windows 的 **5353 属于系统预留端口段**，直接绑定会报 `WinError 10013`。遇到就换高位端口：`python .\accel_server.py dns --port 25353`，再把系统 DNS 指向 `127.0.0.1:25353`。
2. **绑 53 端口需管理员**：想让系统 DNS 设成 `127.0.0.1`（标准 53）需要管理员权限运行；用高位端口 + 代理工具指向则不需要。
3. **proxy 会接管系统代理**：开 proxy 后，浏览器之外的"直连流量"也会走它（都正常转发）；**别和 Steam++ 同时开**——它也会改系统代理/hosts，两者会互相覆盖，二选一。
4. **命令行 git 默认不走 proxy**：proxy 模式只覆盖配了代理的程序；想让 git 也吃到加速，用 **DNS 模式**（git 走系统 DNS）或 hosts 模式。
5. **DoH**：浏览器用「安全 DNS（DoH）」时可能绕过本地解析。DNS/proxy 模式要覆盖到浏览器，可在浏览器设置里关掉 DoH 或加系统代理；hosts 模式则需重启 Chrome 生效。

## 与 hosts_accel.py 怎么选

| 场景 | 用哪个 | 常驻进程 |
|---|---|---|
| 日常浏览 / 命令行 git | `hosts_accel.py start`（hosts 模式） | 无，写完即走 |
| 清单外子域慢、git 慢、不想反复改 hosts | `accel_server.py dns` | 有 |
| 只想加速浏览器、不能动系统 DNS / hosts | `accel_server.py proxy` | 有 |

**组合建议**：日常 hosts 模式做基线；遇到"某个子域慢 / git clone 卡"再按症状切 DNS 或 proxy。

## 安全说明
- **不做 HTTPS 中间人、不装根证书、不解密流量**——这是与 Steam++ 的本质区别；
- 只监听 `127.0.0.1`，不暴露到局域网/公网；
- proxy 只覆盖配了代理的程序；DNS 只改解析结果，不影响内容；
- 真正的 HTTPS 内容级加速需要 MITM + 根证书（Steam++ 模式），本工具默认不做、也不建议。

## 常见问题
| 现象 | 处理 |
|---|---|
| HTTPS 加速不稳定 / 时好时坏 | 已根治：启动时健康检测 + 多 IP 回退（见"工作原理"）。若仍不稳，多为当前网络下 GitHub 各 IP 普遍受限，可重跑一次让健康检测重新选 IP |
| 启动报端口占用 / 10013 | 换高位端口，如 `--port 25353` |
| DNS 查询没回包 | 先确认端口在监听（`netstat -ano \| findstr 端口`）；确认没同时开多个实例 |
| proxy 访问某网站慢 | 多为该站 IPv6/连接问题，本工具已强制 IPv4 直连；可对比直连该站速度 |
| 系统代理设置失败 | 以管理员运行，或手动在 Windows 设置→网络→代理 配 `127.0.0.1:端口` |
| 退出后系统代理没还原 | 手动检查 Windows 代理设置；还原失败时脚本已弹窗提示，手动恢复即可 |
| 与 Steam++ 同开互相覆盖 | 二选一；切换前先停掉对方（proxy 按 Ctrl+C 还原、Steam++ 在设置里关加速） |
