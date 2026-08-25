# hosts_accel.py — Hosts 模式加速器（GitHub 开发者生态）

纯 hosts 方案的域名加速工具，把 GitHub 家族及常用开发者站点的域名固定解析到指定 IP，写入系统 hosts 文件。

**与 Steam++（Watt Toolkit）的区别**：Steam++ 走本地代理（会安装根证书做 HTTPS 中间人解密）；本工具只改 DNS 解析，不装证书、不代理、不碰流量内容——更轻、更安全。

## 快速开始

```powershell
# 直接运行 = 自动加速（DoH）→ 实时监控 → Ctrl+C 自动清理
python .\hosts_accel.py
```

运行后会自动：
1. 通过 **DoH（DNS-over-HTTPS）** 解析全部域名（默认 DNSPod `1.12.12.12`）
2. 写入 hosts 加速条目（首次需确认 UAC 提权）
3. 刷新 DNS 缓存
4. 进入流量实时监控模式
5. Ctrl+C 退出时自动移除 hosts 条目 + 刷新 DNS

## 命令

| 命令 | 说明 |
|---|---|
| `python hosts_accel.py` | 默认：自动加速 + 监控 |
| `python hosts_accel.py --doh-server 3` | 指定阿里云 DoH 端点 |
| `list` | 列出加速域名清单 |
| `status` | 查看当前 hosts 加速条目 |
| `stop` | 移除加速条目 |
| `doh-list` | 列出可用 DoH 端点 |
| `--interval 3` | 监控刷新间隔（秒，默认 2） |

## 指定 DoH 端点

```powershell
python hosts_accel.py --doh-server 3                         # 索引 3 = 阿里云 DNS
python hosts_accel.py --doh-server "https://dns.alidns.com/resolve"  # 完整 URL
python hosts_accel.py doh-list                               # 查看所有可用端点
```

## 配置：优选 IP

编辑同目录 `ips.conf`（每行 `域名=IP`）：

```
github.com=140.82.112.4
raw.githubusercontent.com=185.199.108.133
```

## 加速域名（21 个）

GitHub 主域、内容子域、功能域，以及 hub.docker.com、huggingface.co、greasyfork.org。

## 环境要求

- Windows（写入 hosts 需管理员权限，脚本自动请求 UAC）
- Python 3.8+，零第三方依赖