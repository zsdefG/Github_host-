# AccelGui — 极域 / GitHub 访问加速控制台

Windows 图形化加速器，基于 **WPF + pythonnet（嵌入式 Python）**。通过**本地智能 DNS** 与 **HTTP 代理** 两种模式，对常见被墙/慢速域名返回优选 IP 或转发流量，实现加速与连通性改善。

> 逻辑层（Python）随程序内嵌分发，**目标机器无需安装 Python** 即可运行。

---

## 仓库结构

```
AccelGui\
├── AccelGui\           # WPF 图形界面工程（源码，见其下 README.md 的详细文档）
├── accel_server.py     # DNS / Proxy 服务逻辑（智能 DNS、DoH、端口自适应、Watchdog）
└── hosts_accel.py      # 加速域名清单（21 个）与 hosts 写入 / 回滚逻辑
```

- **`AccelGui\`**：WPF 主程序（net8.0-windows + pythonnet 3.1.0），打包脚本 `pack-runtime.ps1` 会在构建时嵌入 Python 运行时 + VC++ 运行库，输出目录整体拷贝即可在无 Python 的干净环境运行。**详细文档见 [`AccelGui/README.md`](AccelGui/README.md)**。
- **`accel_server.py`**：命令行也能直接使用（`python accel_server.py dns` / `python accel_server.py proxy`），GUI 通过 pythonnet 内嵌调用同一套逻辑。
- **`hosts_accel.py`**：纯 hosts 方案（无代理 / 无证书 / 无 MITM），`python hosts_accel.py` 自动加速、`stop` 一键回滚。

---

## 功能特性

- **双模式加速**：DNS 加速（本地 UDP DNS，加速域名直返优选 IP，其余转发上游，支持 DoH）；Proxy 加速（本地 HTTP 代理，仅 CONNECT 隧道，不装证书、不解密）。
- **DoH（DNS over HTTPS）**：默认开启，启动自动实测 10 个内置端点选最快，也可手动指定或填自定义 URL。
- **连接测试**：并行 TCP 443 探测优选 IP 延迟，服务运行中可验证链路。
- **端口自适应**：端口被占用自动 +1…+99 换用并回显。
- **系统代理**：Proxy 模式一键设置，异常强杀由 Watchdog 子进程自动还原。
- **托盘驻留 / 多开控制 / 三档显示模式**：精简 / 标准 / 完整。

---

## 快速开始

### 构建（Windows，需 .NET 8 SDK）

```powershell
cd hosts\AccelGui
dotnet build -c Release
```

构建完成后自动执行 `pack-runtime.ps1` 嵌入 Python 运行时与 VC++ 运行库。构建机无 Python 时跳过嵌入，程序回退系统 Python（打印警告）。

### 运行

运行 `AccelGui\AccelGui.exe`（框架依赖发布需目标机装 `.NET 8 Desktop Runtime`；若发布为自包含则免装）。首次启动可在"设置"里调整显示模式、DoH 与多开选项。

使用流程：选模式（DNS 默认端口 5353 / Proxy）→ 启动 → 停止（Proxy 模式自动还原系统代理）。涉及系统代理、防火墙时，DNS/代理端口需按提示放行。

---

## 设置与配置

- 设置存储于 `%APPDATA%\AccelGui\settings.json`（显示模式 / Python DLL / DoH 开关与端点 / 多开开关）。
- 自定义优选 IP：在 `AccelGui\hosts\ips.conf`（或脚本同目录）按行写 `域名 IP` 覆盖内置解析。

---

## 技术栈

- **GUI**：WPF（net8.0-windows）+ WinForms 托盘（NotifyIcon）
- **Python 交互**：pythonnet 3.1.0（统一 GIL 管理，见 `AccelGui/PythonRuntime.cs`）
- **打包**：`AccelGui/pack-runtime.ps1`（嵌入 Python 运行时 + VC 运行库 + hosts 脚本）

更多细节（Tab 式文档：目录结构、运行环境、常见问题等）见 [`AccelGui/README.md`](AccelGui/README.md)。