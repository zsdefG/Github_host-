# AccelGui — 极域 / GitHub 访问加速控制台

基于 **WPF + pythonnet（嵌入式 Python 3.12）** 的 Windows 图形化加速器。通过**本地智能 DNS** 与 **HTTP 代理** 两种模式，对常见被墙/慢速域名返回优选 IP 或转发流量，实现加速与连通性改善。

> 逻辑层（Python）随程序内嵌分发，**目标机器无需安装 Python** 即可运行。

---

## 功能特性

- **双模式加速**
  - *DNS 加速*：本地 UDP DNS 服务，对内置 21 个加速域名（`hosts_accel.py` 的 `DOMAINS`）直接返回优选 IP；其余域名转发到上游 DNS（默认 `223.5.5.5`，支持 DoH）。
  - *Proxy 加速*：本地 HTTP 代理 + 动态 IP 映射，加速域名流量转发到优选 IP；不装证书、不做 HTTPS 中间人（仅 CONNECT 隧道）。
- **DoH（DNS over HTTPS）**：默认开启。端点留空时启动自动实测内置清单（腾讯 DNSPod / 阿里 / doh.pub / Google / Cloudflare / 360 / 清华 TUNA 等 10 个）并选最快可用；也支持下拉指定或填写自定义 URL（RFC 8484 优先，自动降级 JSON）。
- **连接测试**：并行 TCP 443 探测优选 IP 延迟；服务运行中可追加链路验证（DNS 模式校验解析结果、Proxy 模式建立 CONNECT 隧道）。
- **端口自适应**：启动端口被占用时自动换用下个端口（最多 +99），并在界面上回显示实际端口。
- **系统代理**：Proxy 模式可一键设置系统代理，异常退出（结束任务/强杀）由 Watchdog 子进程自动还原。
- **托盘驻留**：点击 × 隐藏到托盘继续运行，双击/右键"显示窗口"恢复，右键"退出"彻底停止并还原代理。
- **多开控制**：默认单实例（再次启动激活已有窗口），设置里可开启多开。
- **显示模式**：精简（状态栏 + 启动/停止）/ 标准（+配置 + 日志）/ 完整（+实时优选 IP 表，每 5 秒刷新）。
- **环境检测**：启动时后台输出运行时信息（.NET 版本 / 部署形态 / 系统已装 .NET 运行时清单 / 嵌入式 Python / 脚本目录）到日志，便于在干净环境（如 Windows Sandbox）定位问题。

---

## 目录结构（发布产物）

```
AccelGui\                        ← 构建输出的整个目录拷贝到目标机即可运行
├── AccelGui.exe                 # 主程序（.NET 8 Desktop，框架依赖）
├── Python.Runtime.dll …         # .NET 程序集
├── vcruntime140.dll             # VC++ 运行库（python 依赖，随程序分发）
├── vcruntime140_1.dll
├── msvcp140.dll
├── runtime\python\              # 嵌入式 Python 运行时（无需目标机装 Python）
│   ├── python312.dll
│   ├── Lib\                     # 标准库（已剔除 site-packages 等非必需项）
│   └── DLLs\
└── hosts\                       # 加速逻辑脚本
    ├── accel_server.py          # DNS + Proxy + DoH + 端口自适应 + Watchdog
    ├── hosts_accel.py           # 加速域名清单（DOMAINS，21 个）与解析
    └── ips.conf（可选）         # 手动覆盖域名 → IP
```

---

## 运行环境

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11 x64 |
| .NET | 需 `.NET 8 Desktop Runtime`（当前为框架依赖发布；若发布为自包含则免装） |
| Python | **不需要**（嵌入式运行时已打包）；仅当删除 `runtime\python` 时才回退系统 Python |
| VC++ 运行库 | 已随程序分发（`vcruntime140*.dll` 等），干净环境（如 Windows Sandbox）亦可运行 |

---

## 快速开始

### 构建（Windows，需 .NET 8 SDK）

```powershell
cd hosts\AccelGui
dotnet build -c Release
```

构建完成后（`bin\Release\net8.0-windows\`）会自动执行 `pack-runtime.ps1`：
- 从构建机探测 Python 3.x，把运行时（`python*.dll` + `Lib` + `DLLs`）打包到 `runtime\python\`；
- 从构建机 `System32` 分发 VC++ 运行库（`vcruntime140.dll` 等）到应用根目录与 python 目录；
- 把 `accel_server.py` / `hosts_accel.py` / `ips.conf`（如有）复制到 `hosts\`。

> 构建机无 Python 时跳过嵌入式打包（程序仍可回退系统 Python），脚本会打印警告。

### 运行

直接运行 `AccelGui.exe`。首次启动可在"设置"里调整显示模式、DoH 与多开选项。

---

## 使用说明

1. **选择模式**：`DNS 加速`（默认端口 `5353`）或 `Proxy 加速`。
2. **启动**：点击"启动"。DNS 模式请将系统/路由器 DNS 指向 `127.0.0.1:5353`（或勾选 Proxy 模式的"自动设置系统代理"）。
3. **连接测试**：启动前测速优选 IP；启动后可验证链路。
4. **停止**：点击"停止"；Proxy 模式会自动还原系统代理。
5. **托盘**：点 × 最小化到托盘（服务继续运行），右键托盘图标可显示窗口或退出。

### 设置项

| 设置 | 说明 |
|---|---|
| 显示模式 | 精简 / 标准 / 完整 |
| 允许同时打开多个程序窗口 | 默认关闭 = 单实例（重复启动激活已有窗口） |
| DoH 启用 | 默认开启（DNS 模式生效） |
| DoH 端点 | 留空 = 启动自动选最快；或选内置 / 自定义 URL |
| Python 解释器 | python*.dll 路径，留空自动探测（嵌入式优先）；仅异常时需手动指定 |

### 配置文件

存储于 `%APPDATA%\AccelGui\settings.json`：

```jsonc
{
  "DisplayMode": 1,        // 0=精简 1=标准 2=完整
  "PythonDll": "",         // 手动 Python DLL 路径（留空自动）
  "UseDoh": true,          // DoH 默认开启
  "DohEndpoint": "",       // DoH 端点 URL；空 = 启动自动选择
  "AllowMultiple": false   // 多开开关
}
```

### 自定义优选 IP

在 `hosts\ips.conf` 中按行写 `域名 IP` 即可覆盖 `hosts_accel.py` 内置解析结果。

---

## 常见问题

**Q：Windows Sandbox / 干净系统里启动报 `The type initializer for 'Delegates' threw an exception`？**

这是 pythonnet 加载 `python312.dll` 失败所致，通常是缺少 VC++ 运行库。较新版本已把 `vcruntime140.dll` / `vcruntime140_1.dll` / `msvcp140.dll` 随程序分发，请重新构建并整体拷贝输出目录后再试。

**Q：DNS 加速不生效？**

- 确认本机防火墙放行 UDP/DNS 端口；DNS 模式请把解析 DNS 指向 `127.0.0.1:<端口>`。
- 未命中加速清单的域名走 UDP 上游转发；DoH 端点不可达时自动回退 UDP 上游。

**Q：端口被占用？**

程序会自动换用下一个空闲端口（原端口 +1…+99）并在界面回显。

**Q：托盘退出后系统代理没还原？**

正常路径（托盘"退出"/点停止）都会还原；仅被"结束任务"强杀时由 Watchdog 子进程还原，极端情况下可手动在系统代理设置中关闭。

---

## 技术栈与维护

- **GUI**：WPF（net8.0-windows）+ WinForms 托盘（NotifyIcon）
- **Python 交互**：pythonnet 3.1.0（动态调用 `accel_server.py`，GIL 统一管理见 `PythonRuntime.cs`）
- **打包**：`pack-runtime.ps1`（嵌入 Python 运行时 + VC 运行库 + hosts 脚本）

源码入口：`MainWindow.xaml(.cs)` 界面与交互，`PythonRuntime.cs` Python 引擎单例，`accel_server.py` 全部加速逻辑。