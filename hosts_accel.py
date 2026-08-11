#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hosts 模式加速器 —— GitHub 开发者生态（纯 hosts 方案，无代理 / 无证书 / 无 MITM）

原理：把加速域名固定解析到指定 IP，写入系统 hosts 文件。
相比 Watt Toolkit 的 Proxy 模式更轻、更安全（不安装根证书、不解密流量）。

用法（Windows 下写入需要管理员权限，脚本会自动请求提权）：
    python hosts_accel.py                      # 无参数 = 流量实时监控（Ctrl+C 停止并退出；非管理员会提示运行 stop）
    python hosts_accel.py list                 # 列出加速域名清单
    python hosts_accel.py status               # 查看当前 hosts 加速状态
    python hosts_accel.py start                # 解析域名并把结果固化进 hosts
    python hosts_accel.py start --ip github.com=140.82.112.4 --ip raw.githubusercontent.com=185.199.108.133
    python hosts_accel.py start --dry-run      # 预演，只打印将写入的内容
    python hosts_accel.py stop                 # 移除本工具添加的条目
    python hosts_accel.py stop --dry-run

说明：
- 不带 --ip 时，默认把清单内域名解析为"当前 DNS 结果"并固化（防 DNS 污染/抖动）。
  想要真正的"优选 IP"，用 --ip 指定，或编辑本脚本同目录下的 ips.conf（每行 domain=ip）。
- 只增删自己的标记块（START/END 注释之间），绝不修改 hosts 其它内容；
  每次写入前自动备份到 hosts.bak。
"""

import argparse
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# ---------------- 配置 ----------------
if os.name == "nt":
    HOSTS_PATH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/drivers/etc/hosts"
else:
    HOSTS_PATH = Path("/etc/hosts")

MARKER_START = "# ===== WorkBuddy Hosts Accelerator START ====="
MARKER_END = "# ===== WorkBuddy Hosts Accelerator END ====="

# 加速清单：GitHub 家族 + 开发者生态站点（与 Watt Toolkit「GitHub 加速」一致）
DOMAINS = [
    # GitHub 主域
    "github.com", "github.dev", "api.github.com", "github.githubassets.com",
    "uploads.github.com", "githubapp.com", "github.io",
    "codeload.github.com", "collector.github.com",
    # GitHub 内容子域（githubusercontent.com 裸域无 A 记录，改用具体子域）
    "raw.githubusercontent.com", "avatars.githubusercontent.com",
    "camo.githubusercontent.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    # GitHub 功能域
    "education.github.com", "resources.github.com", "archiveprogram.github.com",
    "pipelines.actions.githubusercontent.com",
    # 开发者生态（非 GitHub）
    "hub.docker.com", "huggingface.co", "greasyfork.org",
]

# 同目录下的优选 IP 配置文件（可选）：每行 domain=ip
CONF_FILE = Path(__file__).resolve().parent / "ips.conf"


# ---------------- 权限与提权 ----------------
def is_admin() -> bool:
    if os.name != "nt":
        return (hasattr(os, "geteuid") and os.geteuid() == 0) or not hasattr(os, "geteuid")
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate(argv_tail=None) -> None:
    """以管理员权限重新运行当前脚本，成功后原进程退出。

    argv_tail: 提权后要执行的命令行参数（默认沿用当前 sys.argv[1:]）。
    例如监控模式 Ctrl+C 时需要执行 stop，可传 ["stop"]。
    """
    if os.name != "nt":
        print("[!] 请用 sudo 运行本脚本")
        sys.exit(1)
    import ctypes
    script = os.path.abspath(sys.argv[0])
    args = " ".join('"%s"' % a for a in (argv_tail if argv_tail is not None else sys.argv[1:]))
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, '"%s" %s' % (script, args), None, 1)
    if ret <= 32:
        print("[!] 提权失败（错误码 %d），请手动以管理员身份运行" % ret)
        sys.exit(1)
    sys.exit(0)


# ---------------- 配置读取 ----------------
def load_overrides() -> dict:
    """读取 ips.conf 中的 domain=ip 映射。"""
    ov = {}
    if CONF_FILE.exists():
        for line in CONF_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            d, ip = line.split("=", 1)
            ov[d.strip().lower()] = ip.strip()
    return ov


def resolve(domain: str) -> list:
    """解析域名当前 A 记录。失败返回空列表。"""
    try:
        return socket.gethostbyname_ex(domain)[2]
    except OSError as e:
        print(f"  [warn] DNS 解析失败: {domain} ({e})")
        return []


def build_entries(overrides: dict) -> list:
    """生成 [(domain, ip), ...]；优先用 override，否则用当前 DNS。"""
    entries = []
    for d in DOMAINS:
        ip = overrides.get(d, "")
        if not ip:
            ips = resolve(d)
            if not ips:
                continue
            ip = ips[0]
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            print(f"  [warn] 非法 IP，跳过: {d}={ip}")
            continue
        entries.append((d, ip))
    return entries


def parse_section_entries(text: str) -> list:
    """从 hosts 现有的加速标记块中解析 [(domain, ip), ...]。"""
    s = text.find(MARKER_START)
    e = text.find(MARKER_END)
    if s == -1 or e == -1:
        return []
    out = []
    for line in text[s:e].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out.append((parts[1], parts[0]))  # (domain, ip)
    return out


def write_section(entries: list) -> None:
    """把加速条目写入 hosts（替换旧块），写入前自动备份。"""
    text = read_hosts()
    if has_section(text):
        text = remove_section(text)
    section = "\n".join(
        [MARKER_START] + [f"{ip:<16} {d}" for d, ip in entries] + [MARKER_END])
    text = text.rstrip() + "\n\n" + section + "\n\n"
    bak = backup()
    write_hosts(text)
    print(f"[+] 已写入 {len(entries)} 条 → {HOSTS_PATH}")
    print(f"[+] 备份：{bak}")


# ---------------- hosts 读写（latin-1 无损字节往返） ----------------
def read_hosts() -> str:
    if not HOSTS_PATH.exists():
        return ""
    return HOSTS_PATH.read_bytes().decode("latin-1")


def write_hosts(text: str) -> None:
    HOSTS_PATH.write_bytes(text.encode("latin-1"))


def has_section(text: str) -> bool:
    return MARKER_START in text and MARKER_END in text


def remove_section(text: str) -> str:
    """只删除本工具的标记块，其余内容原样保留。"""
    start = text.find(MARKER_START)
    if start == -1:
        return text
    end = text.find(MARKER_END)
    if end == -1:
        return text
    end = text.find("\n", end)
    if end == -1:
        end = len(text)
    return text[:start] + text[end + 1:]


def backup() -> Path:
    bak = HOSTS_PATH.with_suffix(".hosts.bak")
    shutil.copy2(HOSTS_PATH, bak)
    return bak


def flush_dns_hint() -> None:
    if os.name == "nt":
        print("[i] 建议刷新 DNS 缓存：ipconfig /flushdns")


def perform_stop() -> None:
    """实际移除加速条目（不检查权限，调用方保证已提权）。"""
    text = read_hosts()
    if not has_section(text):
        print("[i] 无本工具条目，无需处理")
        return
    bak = backup()
    write_hosts(remove_section(text))
    print(f"[-] 已移除加速条目 → {HOSTS_PATH}")
    print(f"[-] 备份：{bak}")
    flush_dns_hint()


# ---------------- 子命令 ----------------
def cmd_list(_args) -> int:
    print(f"加速域名清单（{len(DOMAINS)} 个，GitHub 开发者生态）：")
    for i, d in enumerate(DOMAINS, 1):
        print(f"  {i:2}. {d}")
    print(f"\n提示：可用 --ip domain=ip 或编辑 {CONF_FILE} 指定优选 IP")
    return 0


def cmd_status(_args) -> int:
    text = read_hosts()
    if not has_section(text):
        print(f"[i] 未加速：{HOSTS_PATH} 中无本工具条目")
        return 0
    s = text.find(MARKER_START)
    e = text.find(MARKER_END)
    print(f"[i] 当前 hosts 加速条目（{HOSTS_PATH}）：")
    print(text[s:e + len(MARKER_END)])
    return 0


def cmd_start(args) -> int:
    ov = load_overrides()
    for kv in (args.ip or []):
        d, _, ip = kv.partition("=")
        ov[d.strip().lower()] = ip.strip()
    entries = build_entries(ov)
    if not entries:
        print("[!] 没有可写入的条目（全部解析失败？）")
        return 1

    section = "\n".join(
        [MARKER_START] + [f"{ip:<16} {d}" for d, ip in entries] + [MARKER_END])

    if args.dry_run:
        print("== 预演（不写入）==")
        print(section)
        print(f"== 共 {len(entries)} 条 ==")
        return 0

    if not is_admin():
        print("[i] 需要管理员权限，请求提权…")
        elevate()
        return 0  # elevate 内部会退出，这里不会走到

    write_section(entries)
    flush_dns_hint()
    return 0


def cmd_stop(args) -> int:
    text = read_hosts()
    if not has_section(text):
        print("[i] 无本工具条目，无需处理")
        return 0

    if args.dry_run:
        print("== 预演：将移除以下块 ==")
        s = text.find(MARKER_START)
        e = text.find(MARKER_END)
        print(text[s:e + len(MARKER_END)])
        return 0

    if not is_admin():
        print("[i] 需要管理员权限，请求提权…")
        elevate()
        return 0

    perform_stop()
    return 0


# ---------------- 流量实时监控（默认模式） ----------------
def get_established_to(ip_set: set, raw_out: str = None) -> tuple:
    """返回 ({目标IP: 连接数}, 错误信息)。

    统计到加速 IP 的：
    - TCP：ESTABLISHED / SYN_SENT / CLOSE_WAIT 状态的连接
    - UDP：QUIC/HTTP3 这类 UDP 连接（Chrome 访问 GitHub 默认走 UDP 443）
    raw_out 仅用于测试注入 netstat 文本。
    """
    counts = {}
    if raw_out is None:
        try:
            if os.name == "nt":
                raw_out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
            else:
                raw_out = subprocess.check_output(["netstat", "-an"], text=True, errors="replace")
        except Exception as e:
            return counts, str(e)

    # TCP 行: TCP  local:port  remote:port  ESTABLISHED  pid
    # UDP 行: UDP  local:port  remote:port  pid
    for line in raw_out.splitlines():
        m = re.match(r"^\s*(TCP|UDP)\s+\S+\s+(\S+)(?:\s+(\S+))?", line, re.IGNORECASE)
        if not m:
            continue
        proto = m.group(1).upper()
        remote = m.group(2)
        state = m.group(3)
        ip = remote.rsplit(":", 1)[0].strip("[]")
        if ip not in ip_set:
            continue
        if proto == "TCP":
            if state and state.upper() in ("ESTABLISHED", "ESTAB", "SYN_SENT", "CLOSE_WAIT"):
                counts[ip] = counts.get(ip, 0) + 1
        elif proto == "UDP":
            counts[ip] = counts.get(ip, 0) + 1
    return counts, None


def enable_ansi() -> None:
    """Windows 控制台开启 VT 转义序列支持（Windows Terminal / VS Code 终端默认已支持，此调用无害）。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def run_monitor(args) -> int:
    """默认模式：实时输出到加速域名各 IP 的流量情况；Ctrl+C 时若已加速则执行 stop 后退出。"""
    overrides = load_overrides()

    # 监测集合 = 每个加速域名的全部 A 记录（Chrome 可能连其中任意一个）
    ip_map = {}
    for d in DOMAINS:
        ip = overrides.get(d)
        if ip:
            ip_map.setdefault(ip, set()).add(d)
            continue
        ips = resolve(d)
        for ip in ips:
            ip_map.setdefault(ip, set()).add(d)
    pinned = set(ip_map)
    if not pinned:
        print("[!] 全部域名解析失败，无法监控。", flush=True)
        return 1

    interval = max(0.5, getattr(args, "interval", 2.0))
    print(f"[i] 流量监控已启动：监测 {len(DOMAINS)} 个域名 → {len(pinned)} 个目标 IP（刷新 {interval}s；Ctrl+C 退出）", flush=True)
    if has_section(read_hosts()):
        print("[i] hosts 加速已启用；Ctrl+C 将执行 stop 移除加速。", flush=True)
    else:
        print("[i] 当前未启用 hosts 加速，仅展示到上述 IP 的流量（可先运行 start 开启）。", flush=True)

    # 目标 IP 清单只在启动时打印一次（避免每帧长列表滚动刷屏）
    def trunc(s: str, w: int) -> str:
        return s if len(s) <= w else s[:w - 1] + "…"

    print("---- 监测目标（具体 IP → 域名）----", flush=True)
    for ip in sorted(pinned):
        ds = trunc("、".join(sorted(ip_map[ip])), 44)
        print(f"  {ip:<18}{ds}", flush=True)
    print("-" * 66, flush=True)

    # 终端下仅对"实时连接表"这一小块做 ANSI 原地覆盖刷新；管道/重定向则逐帧追加
    tty = sys.stdout.isatty() and not getattr(args, "no_clear", False)
    if tty:
        enable_ansi()

    prev_n = 0
    try:
        while True:
            counts, err = get_established_to(pinned)
            frame = []
            frame.append(f"== 加速域名连接实时监控  [ {time.strftime('%H:%M:%S')} | 刷新 {interval}s | Ctrl+C 退出 ] ==")
            frame.append(f"{'域名':<40}{'目标IP':<18}连接数")
            frame.append("-" * 66)
            active = 0
            for ip in sorted(counts, key=lambda x: -counts[x]):
                ds = trunc("、".join(sorted(ip_map[ip])), 38)
                frame.append(f"{ds:<40}{ip:<18}{counts[ip]}")
                active += counts[ip]
            if not counts:
                frame.append("  （当前无到上述 IP 的活跃连接）")
            frame.append("-" * 66)
            frame.append(f"  活跃连接总数: {active}  |  有流量的目标 IP: {len(counts)}/{len(pinned)}")
            if err:
                frame.append(f"  [warn] netstat 不可用: {err}")

            if tty:
                if prev_n > 0:
                    sys.stdout.write(f"\x1b[{prev_n}A\r")   # 光标上移回上一帧起点
                sys.stdout.write("\n".join(frame))
                sys.stdout.write("\x1b[J")                  # 清掉旧帧多出的行
            else:
                sys.stdout.write("\n".join(frame) + "\n\n")
            sys.stdout.flush()
            prev_n = len(frame)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[i] Ctrl+C 收到", flush=True)
        text = read_hosts()
        if has_section(text):
            print("    正在执行 stop（移除加速条目）…", flush=True)
            if not is_admin():
                print("[!] 当前进程无管理员权限，无法直接移除 hosts 条目。", flush=True)
                print("    请在【管理员 PowerShell】运行：python .\\hosts_accel.py stop", flush=True)
                return 0
            perform_stop()
        else:
            print("    未启用 hosts 加速，无需移除。", flush=True)
        return 0


# ---------------- 入口 ----------------
def main() -> int:
    # 任何环境下都按行刷新输出，避免管道/非 TTY 下"没有输出"
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
    p = argparse.ArgumentParser(
        description="Hosts 模式加速器（GitHub 开发者生态；纯 DNS 固化，无代理/证书）。"
                    "无参数运行时进入流量实时监控，Ctrl+C 自动执行 stop。")
    p.add_argument("--interval", type=float, default=2.0, help="监控刷新间隔（秒，默认 2）")
    p.add_argument("--no-clear", action="store_true", help="禁用原地刷新（逐帧追加输出；默认终端下原地覆盖，不刷屏）")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="列出域名清单")
    sub.add_parser("status", help="查看当前 hosts 加速状态")
    ps = sub.add_parser("start", help="写入加速条目")
    ps.add_argument("--ip", action="append", metavar="domain=ip", help="指定域名 IP（可多次）")
    ps.add_argument("--dry-run", action="store_true", help="只预览不写入")
    pt = sub.add_parser("stop", help="移除加速条目")
    pt.add_argument("--dry-run", action="store_true", help="只预览不写入")
    args = p.parse_args()

    handlers = {"list": cmd_list, "status": cmd_status, "start": cmd_start, "stop": cmd_stop}
    fn = handlers.get(args.cmd)
    if fn is None:
        return run_monitor(args)   # 无参数 = 流量实时监控
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
