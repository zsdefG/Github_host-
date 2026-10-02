#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hosts 模式加速器 —— GitHub 开发者生态（纯 hosts 方案，无代理 / 无证书 / 无 MITM）

原理：把加速域名固定解析到指定 IP，写入系统 hosts 文件。
相比 Watt Toolkit 的 Proxy 模式更轻、更安全（不安装根证书、不解密流量）。

用法（Windows 下写入需要管理员权限，脚本会自动请求提权）：
    python hosts_accel.py                      # 默认：自动加速（DoH）→ 实时监控 → Ctrl+C 自动清理
    python hosts_accel.py --doh-server 3        # 指定阿里云 DoH 端点
    python hosts_accel.py list                 # 列出加速域名清单
    python hosts_accel.py status               # 查看当前 hosts 加速状态
    python hosts_accel.py stop                 # 移除本工具添加的条目
    python hosts_accel.py stop --dry-run
    python hosts_accel.py doh-list             # 列出可用 DoH 端点

说明：
- 默认使用 DoH（DNS-over-HTTPS）解析，避免本地 DNS 污染/抖动。
- 可通过 --doh-server 指定其他 DoH 端点（索引或完整 URL）。
- 也可编辑同目录下的 ips.conf（每行 domain=ip）手动指定 IP。
- 只增删自己的标记块（START/END 注释之间），绝不修改 hosts 其它内容；
  每次写入前自动备份到 hosts.bak。
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

# ---------------- 配置 ----------------
if os.name == "nt":
    HOSTS_PATH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/drivers/etc/hosts"
else:
    HOSTS_PATH = Path("/etc/hosts")

MARKER_START = "# ===== WorkBuddy Hosts Accelerator START ====="
MARKER_END = "# ===== WorkBuddy Hosts Accelerator END ====="
# 被覆盖条目的备份块（Steam++ 同款思路）：写入加速条目时把普通区同名条目
# 以 "# <ip> <domain>" 注释形式暂存，停止加速时还原回去，避免丢用户原有配置。
BACKUP_START = "# ===== WorkBuddy Hosts Accelerator BACKUP START ====="
BACKUP_END = "# ===== WorkBuddy Hosts Accelerator BACKUP END ====="

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

# DoH（DNS-over-HTTPS）端点列表，用于 --doh 模式解析域名
DOH_ENDPOINTS = [
    "https://1.12.12.12/resolve",
    "https://doh.pub/resolve",
    "https://120.53.53.53/resolve",
    "https://dns.alidns.com/resolve",
    "https://223.6.6.6/resolve",
    "https://223.5.5.5/resolve",
    "https://dns.google/resolve",
    "https://doh.360.cn/resolve",
    "https://cloudflare-dns.com/resolve",
    "https://101.6.6.6:8443/resolve",
]


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


def doh_resolve(domain: str, endpoint: str) -> list:
    """使用 DoH JSON API 解析域名，返回 A 记录 IP 列表。

    endpoint 示例：https://dns.alidns.com/resolve
    使用 Google JSON API 格式：?name=DOMAIN&type=A
    """
    url = f"{endpoint}?name={urllib.parse.quote(domain)}&type=A"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hosts_accel/1.0"})
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read().decode())
        if data.get("Status") != 0:
            return []
        ips = []
        for ans in data.get("Answer", []):
            if ans.get("type") == 1:  # A 记录
                ips.append(ans["data"])
        return ips
    except Exception as e:
        print(f"  [warn] DoH 解析失败: {domain} @ {endpoint} ({e})")
        return []


def build_entries(overrides: dict, doh_url: str = "") -> list:
    """生成 [(domain, ip), ...]；优先用 override，否则用当前 DNS（或 DoH 解析）。"""
    entries = []
    for d in DOMAINS:
        ip = overrides.get(d, "")
        if not ip:
            ips = doh_resolve(d, doh_url) if doh_url else resolve(d)
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


# ---------------- hosts 读写（latin-1 无损字节往返） ----------------
def read_hosts() -> str:
    if not HOSTS_PATH.exists():
        return ""
    return HOSTS_PATH.read_bytes().decode("latin-1")


def write_hosts(text: str) -> None:
    HOSTS_PATH.write_bytes(text.encode("latin-1"))


def has_section(text: str) -> bool:
    return MARKER_START in text and MARKER_END in text


def _block_ranges(text: str) -> list:
    """返回文本中所有本工具块的内存范围 [(start, end), ...]（含 MARKER 与 BACKUP）。"""
    ranges = []
    for a, b in ((MARKER_START, MARKER_END), (BACKUP_START, BACKUP_END)):
        s = text.find(a)
        e = text.find(b)
        if s != -1 and e != -1 and e > s:
            ne = text.find("\n", e)
            ranges.append((s, len(text) if ne == -1 else ne + 1))
    return ranges


def _strip_blocks(text: str) -> str:
    """删除所有本工具块（MARKER/BACKUP），其余内容原样保留。"""
    for s, e in sorted(_block_ranges(text), reverse=True):
        text = text[:s] + text[e:]
    return text


def parse_backup(text: str) -> dict:
    """从 BACKUP 块解析被覆盖条目 {domain: ip}。"""
    s = text.find(BACKUP_START)
    e = text.find(BACKUP_END)
    if s == -1 or e == -1 or e <= s:
        return {}
    out = {}
    for line in text[s:e].splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[0] == "#":
            # parts[1] 必须是合法 IP，排除伪造/注释样式的行
            if parts[1].startswith("=") or parts[1].startswith("#"):
                continue
            try:
                ipaddress.ip_address(parts[1])
            except ValueError:
                continue
            out[parts[2]] = parts[1]
    return out


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


def parse_plain_entries(text: str) -> dict:
    """解析加速块外普通区中的 {domain: ip}。

    只取 "ip domain 行" 且此域名不在加速块内，用于写入前备份被覆盖的条目。
    """
    accel_block = set()
    s = text.find(MARKER_START)
    e = text.find(MARKER_END)
    if s != -1 and e != -1 and e > s:
        accel_block = {line.strip().split()[1]
                       for line in text[s:e].splitlines()
                       if line.strip() and not line.strip().startswith("#")
                       and len(line.strip().split()) >= 2}
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 2 or parts[0].startswith("#"):
            continue
        domain, ip = parts[1], parts[0]
        if domain in accel_block:
            continue
        out[domain] = ip
    return out


def write_section(entries: list) -> None:
    """把加速条目写入 hosts（替换旧块）。

    Steam++ 同款细节：写入前先把普通区中将被覆盖的同名条目移入
    BACKUP 块（注释行），停止加速时再还原，而不是直接丢掉。
    """
    text = read_hosts()
    covered = {d for d, _ in entries}
    backup_map = parse_backup(text)      # 继承上一次的备份（不重复备份）
    text = _strip_blocks(text)

    # 逐行扫描普通区：被覆盖域名的行移入备份并从文本删除
    out_lines = []
    for line in text.splitlines():
        parts = line.strip().split()
        is_entry = len(parts) >= 2 and not parts[0].startswith("#")
        if is_entry and parts[1] in covered:
            if parts[1] not in backup_map:
                backup_map[parts[1]] = parts[0]
            continue                     # 该行不再输出到普通区
        out_lines.append(line)
    text = "\n".join(out_lines).rstrip() + ("\n" if out_lines else "")

    backup_lines = [f"# {ip:<15}{domain}" for domain, ip in backup_map.items()]
    section = [MARKER_START] + [f"{ip:<16} {d}" for d, ip in entries] + [MARKER_END]
    if backup_lines:
        section += [BACKUP_START] + backup_lines + [BACKUP_END]

    text = text.rstrip() + "\n\n" + "\n".join(section) + "\n\n"
    bak = backup()
    write_hosts(text)
    print(f"[+] 已写入 {len(entries)} 条 → {HOSTS_PATH}")
    if backup_lines:
        print(f"[+] 已备份被覆盖的 {len(backup_lines)} 条原条目（停止加速时还原）")
    print(f"[+] 备份文件：{bak}")


def remove_section(text: str) -> str:
    """删除本工具的全部块（MARKER + BACKUP），其余内容原样保留。"""
    return _strip_blocks(text)


def backup() -> Path:
    bak = HOSTS_PATH.with_suffix(".hosts.bak")
    shutil.copy2(HOSTS_PATH, bak)
    return bak


def flush_dns() -> None:
    """刷新系统 DNS 缓存。"""
    if os.name == "nt":
        try:
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, text=True)
            print("[i] DNS 缓存已刷新")
        except Exception:
            print("[i] 建议手动刷新 DNS：ipconfig /flushdns")


def perform_stop() -> None:
    """实际移除加速条目，并还原备份块中的被覆盖条目（调用方保证已提权）。"""
    text = read_hosts()
    if not has_section(text):
        print("[i] 无本工具条目，无需处理")
        return
    bak = backup()
    old_backup = parse_backup(text)
    cleaned = remove_section(text)

    # 还原：备份条目写回普通区，条件是不与当前普通区已有条目冲突
    restored_lines = []
    current = parse_plain_entries(cleaned)
    for domain, ip in old_backup.items():
        if domain not in current:
            restored_lines.append(f"{ip:<16} {domain}")
    if restored_lines:
        cleaned = cleaned.rstrip() + "\n\n" + "\n".join(restored_lines) + "\n\n"

    write_hosts(cleaned)
    print(f"[-] 已移除加速条目 → {HOSTS_PATH}")
    if restored_lines:
        print(f"[-] 已还原被覆盖的 {len(restored_lines)} 条原配置")
    print(f"[-] 备份文件：{bak}")
    flush_dns()


# ---------------- 子命令 ----------------
def cmd_list(_args) -> int:
    print(f"加速域名清单（{len(DOMAINS)} 个，GitHub 开发者生态）：")
    for i, d in enumerate(DOMAINS, 1):
        print(f"  {i:2}. {d}")
    print(f"\n提示：可编辑 {CONF_FILE} 指定优选 IP（每行 domain=ip）")
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


def cmd_doh_list(_args) -> int:
    print(f"可用 DoH 端点（{len(DOH_ENDPOINTS)} 个）：")
    for i, url in enumerate(DOH_ENDPOINTS):
        print(f"  {i}. {url}")
    print()
    print("直接运行脚本即自动使用 DoH（默认端点 0）：")
    print(f"  python hosts_accel.py")
    print(f"  python hosts_accel.py --doh-server 2   # 用索引")
    print(f"  python hosts_accel.py --doh-server {DOH_ENDPOINTS[0]}  # 用完整 URL")
    return 0


def _resolve_doh_url(raw: str) -> str:
    """解析 --doh-server 参数：索引号或完整 URL，返回完整 DoH URL。"""
    if raw is None:
        return DOH_ENDPOINTS[0]
    if raw.isdigit():
        idx = int(raw)
        if 0 <= idx < len(DOH_ENDPOINTS):
            return DOH_ENDPOINTS[idx]
        print(f"[warn] 索引 {idx} 超出范围（0-{len(DOH_ENDPOINTS) - 1}），使用默认")
        return DOH_ENDPOINTS[0]
    return raw.rstrip("/")


def _auto_start(doh_url: str) -> bool:
    """自动执行加速（写入 hosts + 刷新 DNS）。返回 True 成功，False 全部失败。"""
    overrides = load_overrides()
    entries = build_entries(overrides, doh_url)
    if not entries:
        print("[!] 全部解析失败，跳过加速", flush=True)
        return False
    write_section(entries)
    flush_dns()
    return True


def _repair_hosts_if_tampered(doh_url: str) -> None:
    """检测 hosts 加速块是否被外部软件改写/删除，是则自动重建。

    对应 Steam++ 的 FileSystemWatcher 轮询版：加速运行期间若被其他加速器、
    杀软或用户手动改动（块缺失、域名条目缺失），检测到后自动写回，
    避免加速静默失效。非管理员/解析失败时仅提示不强行写入。
    """
    text = read_hosts()
    if not has_section(text):
        # 块整个没了：说明加速条目被外部移除，主动重建（若之前本就启用）
        print("[warn] 检测到 hosts 加速块已被外部删除，自动重建…", flush=True)
        if is_admin():
            _auto_start(doh_url)
        else:
            print("[warn] 当前无管理员权限，请在管理员终端执行：python hosts_accel.py", flush=True)
        return

    # 块内域名条目缺失/计数不符 => 部分被改写，重建一次
    overrides = load_overrides()
    expected = set()
    for d in DOMAINS:
        if overrides.get(d) or resolve(d):
            expected.add(d)
    present = {d for d, _ in parse_section_entries(text)}
    missing = expected - present
    if missing:
        print(f"[warn] 检测到 hosts 加速块被改写（缺失 {len(missing)} 个域名），自动重建…", flush=True)
        if is_admin():
            _auto_start(doh_url)
        else:
            print("[warn] 当前无管理员权限，请在管理员终端执行：python hosts_accel.py", flush=True)


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
    """默认模式：自动加速（DoH）→ 流量监控 → Ctrl+C 自动 stop。"""

    # ===== 第一步：自动启动加速（DoH，默认 DNSPod）=====
    doh_url = _resolve_doh_url(getattr(args, 'doh_server', None))
    if not has_section(read_hosts()):
        if not is_admin():
            print("[i] 需要管理员权限写入 hosts，请求提权…", flush=True)
            elevate()
            return 0  # elevate() 内部 exit，不会走到这里

        print(f"[i] 自动加速（DoH: {doh_url}）…", flush=True)
        _auto_start(doh_url)

    # ===== 第二步：加载监测集合 =====
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
        print("[i] 当前未启用 hosts 加速，仅展示到上述 IP 的流量。", flush=True)

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
    check_interval = max(15.0, interval * 4)   # hosts 篡改检测周期（至少 15s）
    last_check = 0.0
    try:
        while True:
            # ---- hosts 加速块完整性检查（Steam++ 同款：外部改写了 hosts 自动重建） ----
            now = time.time()
            if now - last_check >= check_interval:
                last_check = now
                _repair_hosts_if_tampered(doh_url)

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
                    "无参数自动使用 DoH 加速 + 流量监控，Ctrl+C 自动清理。")
    p.add_argument("--interval", type=float, default=2.0, help="监控刷新间隔（秒，默认 2）")
    p.add_argument("--no-clear", action="store_true", help="禁用原地刷新（逐帧追加输出；默认终端下原地覆盖，不刷屏）")
    p.add_argument("--doh-server", metavar="URL或索引", default=None,
                    help=f"DoH 端点：URL 或索引 0-{len(DOH_ENDPOINTS) - 1}（默认 0={DOH_ENDPOINTS[0]}）。"
                         f"用 `doh-list` 查看所有端点")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="列出域名清单")
    sub.add_parser("status", help="查看当前 hosts 加速状态")
    pt = sub.add_parser("stop", help="移除加速条目")
    pt.add_argument("--dry-run", action="store_true", help="只预览不写入")
    sub.add_parser("doh-list", help="列出可用 DoH 端点")
    args = p.parse_args()

    handlers = {"list": cmd_list, "status": cmd_status, "stop": cmd_stop,
                 "doh-list": cmd_doh_list}
    fn = handlers.get(args.cmd)
    if fn is None:
        return run_monitor(args)   # 无参数 = 流量实时监控
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
