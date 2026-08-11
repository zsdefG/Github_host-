# hosts_accel.py — Hosts 模式加速器（GitHub 开发者生态）

纯 hosts 方案的域名加速工具：把 GitHub 家族及常用开发者站点的域名**固定解析到指定 IP**，写入系统 hosts 文件。

**与 Steam++（Watt Toolkit）的区别**：Steam++ 走本地代理（会安装根证书做 HTTPS 中间人解密）；本工具**只改 DNS 解析，不装证书、不代理、不碰流量内容**——更轻、更安全，代价是只优化"连接哪个 IP"，不提供代理转发。

---

## 环境要求
- Windows（写入系统 hosts 需要管理员权限，脚本会自动请求 UAC）
- Python 3.8+，**零第三方依赖**（仅标准库）

## 快速开始

```powershell
cd D:\文档\workbuddy\Daliy\hosts

# 1) 启用加速（需管理员，弹一次 UAC）
python .\hosts_accel.py start

# 2) 重要：刷新 DNS 并完全重启 Chrome（或 chrome://net-internals/#dns → Clear host cache）
ipconfig /flushdns

# 3) 查看加速状态 / 实时流量监控（Ctrl+C 自动 stop 并退出）
python .\hosts_accel.py status
python .\hosts_accel.py
```

## 命令一览

| 命令 | 说明 |
|---|---|
| `python .\hosts_accel.py` | **默认 = 实时流量监控**：先打印全部监测 IP，再对"实时连接表"原地刷新（不刷屏）；Ctrl+C = 执行 stop 退出 |
| `list` | 列出加速域名清单 |
| `status` | 查看 hosts 中当前加速条目 |
| `start` | 解析域名并把结果写入 hosts（需管理员） |
| `start --ip github.com=140.82.112.4` | 手动指定优选 IP（可多次 `--ip`） |
| `start --dry-run` | 预演，只打印将写入的内容 |
| `stop` | 移除本工具添加的条目（需管理员） |
| `stop --dry-run` | 预演 |
| `--interval 3` | 监控刷新间隔（秒，默认 2），与 `--no-clear` 同为全局参数 |
| `--no-clear` | 禁用原地刷新，改为逐帧追加输出（适合重定向/日志） |

> 直接运行 `.py` 无参数时，也可指定 `python .\hosts_accel.py --interval 1 --no-clear`。

## 配置：优选 IP（可选）
默认把域名解析为**当前 DNS 结果**固化（防污染/抖动）。想指定真正的"优选 IP"：

- 命令行：`start --ip github.com=140.82.112.4 --ip raw.githubusercontent.com=185.199.108.133`
- 或编辑同目录 `ips.conf`（每行 `域名=IP`，`#` 开头为注释）：

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
1. `start` 解析全部 21 个域名（或 `ips.conf`/`--ip` 覆盖）→ 生成 `域名 → IP` 条目；
2. 写入 hosts 的**标记块**之间（`# ===== WorkBuddy Hosts Accelerator START/END =====`）；
3. 写入前自动备份到 `C:\Windows\System32\drivers\etc\hosts.hosts.bak`；
4. `stop` / 监控 Ctrl+C：只删除标记块，**绝不触碰 hosts 其它内容**；用 `latin-1` 无损读写，保留 UTF-8 BOM。

监控统计到加速 IP 的 **TCP（ESTABLISHED/SYN_SENT/CLOSE_WAIT）+ UDP（QUIC/HTTP3）** 连接，Chrome 访问 GitHub 的 UDP 流量也能看到。

## ⚠️ 重要注意事项
1. **启用后必须重启 Chrome**：Chrome 的「安全 DNS（DoH）」有独立缓存，不重启会继续连旧 IP，导致"看起来没生效"。
2. **不要与 Steam++ 同时开**：Steam++ 退出时会重写/还原 hosts，会抹掉本工具的条目；两个工具会互相覆盖。规则：二选一，切换前先 `stop` 对方的。
3. **监控看到的 IP 会变**：每次启动实时解析，DNS 轮询会让数值略有不同（正常现象）。
4. **DoH 差异**：Chrome 用 DoH 解析出的某些 GitHub IP（如 140.82.x）本地 DNS 不返回时，监控可能监不到那部分连接；启用 hosts 固定 + 重启 Chrome 后可消除大部分差异。
5. **管理员权限**：`start`/`stop` 需要管理员；监控本身不需要（但非管理员时 Ctrl+C 无法直接移除条目，会提示你用管理员跑 `stop`）。

## 常见问题
| 现象 | 处理 |
|---|---|
| 监控"没有输出" | 已修复：脚本按行刷新输出，任何环境即时显示 |
| 监控"刷屏" | 已修复：目标清单只打印一次，仅实时连接表原地刷新 |
| 访问 GitHub 监控无连接 | ① 确认 hosts 有加速块（`status`）；② 重启 Chrome；③ 刷新 DNS |
| 加速"失效" | 检查 `status` 是否还有条目；若被 Steam++ 覆盖，`stop` 后重新 `start` |

## 安全说明
- 只修改自己标记块内的内容，不碰其它行；
- 每次写入自动备份，`stop` 可完整回滚；
- **不安装根证书、不做代理/中间人**，相比 Steam++ 的 Proxy 模式风险面小得多；
- 建议只在可信环境下使用，并把 hosts 备份（`hosts\hosts.backup-20260811`）妥善保管。
# Github_host-
# Github_host-
