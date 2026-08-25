# hosts_accel.py — Hosts 模式加速器（GitHub 开发者生态）

纯 hosts 方案的域名加速工具：把 GitHub 家族及常用开发者站点的域名**固定解析到指定 IP**，写入系统 hosts 文件。

**与 Steam++（Watt Toolkit）的区别**：Steam++ 走本地代理（会安装根证书做 HTTPS 中间人解密）；本工具**只改 DNS 解析，不装证书、不代理、不碰流量内容**——更轻、更安全，代价是只优化"连接哪个 IP"，不提供代理转发。

---

## 环境要求
- Windows（写入系统 hosts 需要管理员权限，脚本会自动请求 UAC）
- Python 3.8+，**零第三方依赖**（仅标准库）

## 快速开始

```powershell
# 直接运行 = 自动加速（DoH 解析）→ 实时监控 → Ctrl+C 自动清理
python .\hosts_accel.py
```

运行后会自动：
1. 通过 **DoH（DNS-over-HTTPS）** 解析全部域名（默认 DNSPod）
2. 写入 hosts 加速条目（首次需确认 UAC 提权）
3. 刷新 DNS 缓存（`ipconfig /flushdns`）
4. 进入流量实时监控模式
5. Ctrl+C 退出时自动移除 hosts 条目 + 刷新 DNS

## 命令一览

| 命令 | 说明 |
|---|---|
| `python .\hosts_accel.py` | **默认 = 自动加速（DoH）→ 实时监控 → Ctrl+C 自动清理** |
| `python .\hosts_accel.py --doh-server 3` | 指定阿里云 DoH 端点 |
| `list` | 列出加速域名清单 |
| `status` | 查看当前 hosts 加速条目 |
| `stop` | 移除本工具添加的条目（需管理员） |
| `stop --dry-run` | 预演 |
| `doh-list` | 列出可用 DoH 端点 |
| `--doh-server URL或索引` | 指定 DoH 端点（索引 0-9 或完整 URL） |
| `--interval 3` | 监控刷新间隔（秒，默认 2） |
| `--no-clear` | 禁用原地刷新，改为逐帧追加输出（适合重定向/日志） |

## 指定 DoH 端点

```powershell
python .\hosts_accel.py --doh-server 3            # 索引 3 = 阿里云 DNS
python .\hosts_accel.py --doh-server "https://dns.alidns.com/resolve"  # 完整 URL
python .\hosts_accel.py doh-list                  # 查看所有可用端点
```

## 配置：优选 IP（可选）

默认使用 DoH 解析获取 IP。想手动指定优选 IP，编辑同目录 `ips.conf`（每行 `域名=IP`，`#` 开头为注释）：

```
# ips.conf 示例
github.com=140.82.112.4
raw.githubusercontent.com=185.199.108.133
```

## 加速域名清单（21 个）
- **GitHub 主域**：github.com、github.dev、api.github.com、github.githubassets.com、uploads.github.com、githubapp.com、github.io、codeload.github.com、collector.github.com
- **GitHub 内容子域**：raw / avatars / camo / objects / release-assets `.githubusercontent.com`
- **GitHub 功能域**：education、resources、archiveprogram、pipelines.actions（Actions）
- **开发者生态**：hub.docker.com、huggingface.co、greasyfork.org

## 工作原理
1. 运行脚本 → 自动通过 DoH 解析全部 21 个域名（或 `ips.conf` 覆盖）→ 生成 `域名 → IP` 条目；
2. 写入 hosts 的**标记块**之间（`# ===== WorkBuddy Hosts Accelerator START/END =====`）；
3. 写入前自动备份到 `C:\Windows\System32\drivers\etc\hosts.hosts.bak`；
4. 自动刷新 DNS 缓存；
5. 进入实时监控模式，统计到加速 IP 的 TCP（ESTABLISHED/SYN_SENT/CLOSE_WAIT）+ UDP（QUIC/HTTP3）连接；
6. Ctrl+C 退出时自动移除标记块 + 刷新 DNS。

## ⚠️ 重要注意事项
1. **启用后必须重启 Chrome**：Chrome 的「安全 DNS（DoH）」有独立缓存，不重启会继续连旧 IP，导致"看起来没生效"。
2. **不要与 Steam++ 同时开**：Steam++ 退出时会重写/还原 hosts，会抹掉本工具的条目；两个工具会互相覆盖。规则：二选一，切换前先 `stop` 对方的。
3. **监控看到的 IP 会变**：每次启动实时解析，DNS 轮询会让数值略有不同（正常现象）。
4. **管理员权限**：首次写入 hosts 需要管理员权限（自动弹 UAC）；监控本身不需要，但 Ctrl+C 自动清理需要管理员权限（已提权进程不受影响）。

## 可用 DoH 端点
| 索引 | 端点 | 提供商 |
|---|---|---|
| 0 | https://1.12.12.12/resolve | DNSPod |
| 1 | https://doh.pub/resolve | DNSPod |
| 2 | https://120.53.53.53/resolve | DNSPod |
| 3 | https://dns.alidns.com/resolve | 阿里云 |
| 4 | https://223.6.6.6/resolve | DNSPod |
| 5 | https://223.5.5.5/resolve | 阿里云 |
| 6 | https://dns.google/resolve | Google |
| 7 | https://doh.360.cn/resolve | 360 |
| 8 | https://cloudflare-dns.com/resolve | Cloudflare |
| 9 | https://101.6.6.6:8443/resolve | CNNIC |

## 安全说明
- 只修改自己标记块内的内容，不碰其它行；
- 每次写入自动备份，`stop` 可完整回滚；
- **不安装根证书、不做代理/中间人**，相比 Steam++ 的 Proxy 模式风险面小得多；
- 建议只在可信环境下使用，并把 hosts 备份妥善保管。