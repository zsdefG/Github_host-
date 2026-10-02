#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
accel_server.py —— hosts_accel 的 DNS 与 HTTP 加速服务
========================================================
在 hosts 模式之外，提供两种本地服务（复用 hosts_accel.py 的域名清单与优选 IP）：

  [DNS 加速]  本地智能 DNS：对加速域名（含子域通配）直接返回优选 IP，
              其余域名转发上游 DNS。比 hosts 强在支持 *.子域 通配。
  [HTTP 加速] 本地正向代理：对加速域名直连优选 IP（绕过本机 DNS 解析），
              HTTPS 走 CONNECT 隧道（不做 MITM/不解密/不装证书）。

用法：
  python accel_server.py dns    [--port 5353] [--upstream 223.5.5.5]
  python accel_server.py proxy  [--port 8080] [--no-set-proxy]
  (Ctrl+C 停止)

把 DNS 模式配成系统 DNS（需管理员绑 53 端口，或让代理软件指向 5353）；
proxy 模式启动时【自动把系统代理设为 127.0.0.1:端口】，退出时自动还原；
若自动设置失败（无权限等）会弹窗提示手动配置。不想自动设置加 --no-set-proxy。

安全边界：本工具不做 HTTPS 中间人，不安装根证书，不解密任何流量。
真正的 HTTPS 内容级加速需要 MITM + 根证书（即 Steam++ Proxy 模式），
那不是本工具的默认行为，也不建议。
"""

import argparse
import http.client
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 复用 hosts_accel 的域名清单 / 优选 IP / 解析逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hosts_accel

MAX_BODY = 8_000_000  # HTTP 代理请求体上限
PORT_RETRY = 100      # 端口占用自适应：请求端口被占用时最多向后尝试的端口数

# GitHub 官方 IP 补充池（健康检测会筛掉当前不通的，只作候选）
IP_POOL = {
    "github.com": ["140.82.114.4", "140.82.112.4", "140.82.113.4", "20.205.243.166"],
    "api.github.com": ["140.82.114.6", "140.82.112.6", "20.205.243.168"],
    "codeload.github.com": ["140.82.114.9", "140.82.112.9", "20.205.243.165"],
    "githubapp.com": ["140.82.114.30", "140.82.113.30"],
    "collector.github.com": ["140.82.113.22", "140.82.114.22"],
    "education.github.com": ["140.82.114.22", "140.82.113.22"],
    "github.githubassets.com": ["185.199.108.215", "185.199.109.215", "185.199.110.215", "185.199.111.215"],
    "github.io": ["185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"],
    "raw.githubusercontent.com": ["185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133"],
    "avatars.githubusercontent.com": ["185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133"],
    "camo.githubusercontent.com": ["185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133"],
    "objects.githubusercontent.com": ["185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133"],
    "release-assets.githubusercontent.com": ["185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133"],
    "resources.github.com": ["185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133"],
    "archiveprogram.github.com": ["185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"],
}


# ---------------- 全局共享 IP 映射（复检线程热更新） ----------------
IP_MAP_LOCK = threading.RLock()  # 复检更新 + 服务读取共用一把可重入锁
IP_MAP: dict = {}                # {域名: [优选IP,...]}，与传入各服务的引用一致

# ---------------- 日志回调（GUI 内嵌时接管 print） ----------------
_LOG_CALLBACK = None  # 由 set_log_callback 设置，签名 fn(stream:str, msg:str)


def set_log_callback(fn) -> None:
    """设置日志回调（stream: 'out'/'err'）。GUI 内嵌时接管全部输出；None 恢复 print。"""
    global _LOG_CALLBACK
    _LOG_CALLBACK = fn


def _log(stream: str, msg: str) -> None:
    if _LOG_CALLBACK is not None:
        try:
            _LOG_CALLBACK(stream, msg)
            return
        except Exception:
            pass  # 回调异常退回 print，不中断服务
    (sys.stderr if stream == "err" else sys.stdout).write(msg + "\n")


# ---------------- 域名 -> 优选 IP 映射 ----------------
def probe_443(ip: str, timeout: float = 2.0):
    """探测 IP:443 是否可连，返回延迟(ms)；失败返回 None。"""
    t0 = time.time()
    try:
        s = socket.create_connection((ip, 443), timeout=timeout)
        s.close()
        return (time.time() - t0) * 1000
    except OSError:
        return None


def health_check(ip_map: dict, timeout: float = 2.0) -> dict:
    """对每个域名的候选 IP 做 TCP 443 探测，按延迟升序排序；全挂则保留原列表（请求时再逐个试）。"""
    result = {}
    for base, ips in ip_map.items():
        if len(ips) <= 1:
            # 单 IP 也探测，避免复检盲区；不通则保留原列表（请求时交给系统解析回退）
            lat = probe_443(ips[0], timeout)
            result[base] = ips if lat is not None else list(ips)
            continue
        with ThreadPoolExecutor(max_workers=8) as ex:
            lats = list(ex.map(lambda ip: probe_443(ip, timeout), ips))
        alive = sorted((lat, ip) for lat, ip in zip(lats, ips) if lat is not None)
        result[base] = [ip for _, ip in alive] or list(ips)
    return result


def build_ip_map() -> dict:
    """返回 {域名: [IP,...]}，优先 ips.conf/覆盖，否则当前 DNS 解析 + GitHub 官方 IP 池补充，再健康排序。"""
    overrides = hosts_accel.load_overrides()
    ip_map: dict = {}
    for d in hosts_accel.DOMAINS:
        ip = overrides.get(d)
        if ip:
            ip_map.setdefault(d, []).append(ip)
            continue
        seen = set()
        for ip in hosts_accel.resolve(d):
            if ip not in seen:
                ip_map.setdefault(d, []).append(ip)
                seen.add(ip)
        for ip in IP_POOL.get(d, []):
            if ip not in seen:
                ip_map.setdefault(d, []).append(ip)
                seen.add(ip)
    _log("out", "[i] 对候选 IP 做连通性健康检测（TCP 443）…")
    return health_check(ip_map)


def snapshot_ip_map() -> dict:
    """返回 IP_MAP 的深拷贝快照 {域名: [优选IP,...]}，供 GUI 表格展示（锁内复制）。"""
    with IP_MAP_LOCK:
        return {d: list(ips) for d, ips in IP_MAP.items()}


def refresh_ip_map() -> int:
    """构建域名->IP 映射并写入全局 IP_MAP（GUI 启动服务前调用），返回可用域名数。"""
    ip_map = build_ip_map()
    if not ip_map:
        return 0
    with IP_MAP_LOCK:
        IP_MAP.clear()
        IP_MAP.update(ip_map)
    return len(ip_map)


def snapshot_ip_map_json() -> str:
    """IP_MAP 的 JSON 快照（锁内复制后序列化），供 GUI 表格展示。"""
    with IP_MAP_LOCK:
        return json.dumps({d: ips for d, ips in IP_MAP.items()})


# 连接测试默认域名（覆盖最常用的加速场景）
CONN_TEST_DOMAINS = [
    "github.com", "api.github.com", "raw.githubusercontent.com",
    "avatars.githubusercontent.com", "codeload.github.com",
    "github.githubassets.com",
]


def connection_test(domains: list | None = None, timeout: float = 2.0,
                    max_ips_per_domain: int = 4) -> str:
    """链路连接测试：对每个目标域名的候选 IP 做 TCP 443 探测。

    优先用全局 IP_MAP（服务未启动时为空则临时构建），每域名最多测 max_ips_per_domain 个
    候选、并行探测。返回 JSON 字符串：
      {"elapsed": 秒, "domains": [{"domain", "ok", "total", "ips": [{"ip", "latency"}]}]}
    latency 为毫秒，None 表示不通。GUI 调用后逐行展示。
    """
    domains = list(domains or CONN_TEST_DOMAINS)
    with IP_MAP_LOCK:
        ip_map = {d: list(ips) for d, ips in IP_MAP.items()}
    if not ip_map:
        _log("out", "[i] 连接测试：IP_MAP 为空，临时构建域名->IP 映射…")
        ip_map = build_ip_map()
    candidates: list = []
    for d in domains:
        ips = ip_map.get(d) or IP_POOL.get(d, [])
        if not ips:  # 连候选池都没有时回退实时解析
            ips = list(hosts_accel.resolve(d))
        candidates.extend((d, ip) for ip in ips[:max_ips_per_domain])
    if not candidates:
        return json.dumps({"elapsed": 0, "domains": []}, ensure_ascii=False)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        lats = list(ex.map(lambda c: probe_443(c[1], timeout), candidates))
    by_domain: dict = {}
    for (d, ip), lat in zip(candidates, lats):
        by_domain.setdefault(d, []).append({"ip": ip,
                                             "latency": round(lat, 1) if lat is not None else None})
    result = []
    for d in domains:
        items = by_domain.get(d, [])
        result.append({"domain": d,
                       "ok": sum(1 for it in items if it["latency"] is not None),
                       "total": len(items),
                       "ips": items})
    return json.dumps({"elapsed": round(time.time() - t0, 1), "domains": result},
                      ensure_ascii=False)


def _run_recheck(timeout: float) -> int:
    """执行一轮 IP 复检：对 IP_MAP 快照重新健康检测并热更新，返回更新的域名数。"""
    with IP_MAP_LOCK:
        snapshot = {d: list(ips) for d, ips in IP_MAP.items()}
    if not snapshot:
        return 0
    refreshed = health_check(snapshot, timeout)
    with IP_MAP_LOCK:
        IP_MAP.clear()
        IP_MAP.update(refreshed)
    # 按集合比较（顺序变化不算"更新"），避免误报
    changed = {d for d in refreshed if set(refreshed[d]) != set(snapshot.get(d, ()))}
    if changed:
        _log("out", f"[i] IP 复检完成，更新 {len(changed)} 个域名: "
                    f"{', '.join(sorted(changed))}")
    return len(changed)


def recheck_loop(interval: float = 60.0, timeout: float = 2.0,
                 stop_event: threading.Event | None = None) -> None:
    """后台复检线程：定期复检 + 按需触发复检。

    对应 Steam++ 的实时动态优选：CDN 边缘 IP 是动态的，
    启动时的一次快照会随时间失效（即"时通时不通"的根因之一）。
    线程以 0.5s 粒度轮询等待，三个唤醒来源：
      - stop_event 置位：立即退出（GUI 内嵌停止服务用）；
      - _RECHECK_REQUEST 置位且距上次按需复检 >= _ONDEMAND_MIN_GAP：提前执行
        一轮快速复检（请求全候选失败时触发，快速淘汰死 IP），并清信号；
      - 否则每 interval 秒周期性复检一次。
    复检只替换映射内容（引用不变），服务线程读取受 IP_MAP_LOCK 保护。
    全部挂的域名保留原列表，请求时逐 IP 回退。
    """
    _RECHECK_REQUEST.clear()  # 清掉旧线程/旧会话可能残留的触发信号
    global _LAST_ONDEMAND_TS
    last_periodic = time.monotonic()
    while True:
        if stop_event is not None and stop_event.wait(0.5):
            return  # 停止信号，退出
        now = time.monotonic()
        # 按需触发（防抖）：失败信号要求重检，且距上次按需重检已超过最小间隔
        if (_RECHECK_REQUEST.is_set()
                and now - _LAST_ONDEMAND_TS >= _ONDEMAND_MIN_GAP):
            _RECHECK_REQUEST.clear()
            _LAST_ONDEMAND_TS = now
            try:
                _run_recheck(timeout)
            except Exception as e:
                # 单轮复检失败不能让线程死亡（否则复检永久停止），记录后下一轮继续
                _log("err", f"[warn] IP 复检异常，下轮重试: {e}")
            continue
        # 周期触发
        if now - last_periodic >= interval:
            last_periodic = now
            try:
                _run_recheck(timeout)
            except Exception as e:
                _log("err", f"[warn] IP 复检异常，下轮重试: {e}")


def match_base(domain: str, ip_map: dict):
    """匹配加速域名（精确或子域），返回最长命中的基础域名；无则 None。"""
    domain = domain.rstrip(".").lower()
    best = None
    with IP_MAP_LOCK:  # 防止复检线程并发 clear/update
        for base in ip_map:
            if domain == base or domain.endswith("." + base):
                if best is None or len(base) > len(best):
                    best = base
    return best


def resolve_host(hostname: str, ip_map: dict):
    """加速域名返回优选 IP 列表（已按健康检测排序），否则返回 None（走正常解析）。"""
    with IP_MAP_LOCK:
        base = match_base(hostname, ip_map)
        if base and ip_map.get(base):
            return list(ip_map[base])  # 拷贝快照，避免复检并发修改
    return None


# ---------------- 系统代理设置（仅 Windows） ----------------
PROXY_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def prompt(title: str, msg: str) -> None:
    """控制台 + Windows 弹窗双重提示。"""
    print(f"[!] {msg}", file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)  # MB_ICONERROR
        except Exception:
            pass


def read_proxy_reg() -> tuple:
    """读取当前系统代理 (ProxyEnable, ProxyServer, ProxyOverride)。非 Windows 返回全空。"""
    enable, server, override = 0, "", ""
    if os.name != "nt":
        return enable, server, override
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_REG_PATH) as k:
            for name, store in (("ProxyEnable", 0), ("ProxyServer", 1), ("ProxyOverride", 2)):
                try:
                    val = winreg.QueryValueEx(k, name)[0]
                    if store == 0:
                        enable = int(val)
                    elif store == 1:
                        server = str(val)
                    else:
                        override = str(val)
                except OSError:
                    pass
    except OSError:
        pass
    return enable, server, override


def write_proxy_reg(enable: bool, server: str, override: str | None = None) -> None:
    """写入系统代理（HKCU，一般无需管理员）。失败抛异常由调用方处理。"""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, int(bool(enable)))
        winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
        if override is not None:
            winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, override)


def notify_proxy_changed() -> None:
    """通知 WinINET 代理设置已变（浏览器/系统立即生效，无需重启）。"""
    try:
        import ctypes
        w = ctypes.windll.wininet.InternetSetOptionW
        w(None, 39, None, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        w(None, 37, None, 0)  # INTERNET_OPTION_REFRESH
    except Exception:
        pass


def restore_proxy(saved_proxy) -> None:
    """把系统代理还原为 saved_proxy 元组 (enable, server, override)。失败弹窗提示。"""
    if saved_proxy is None:
        return
    try:
        write_proxy_reg(saved_proxy[0], saved_proxy[1], saved_proxy[2])
        notify_proxy_changed()
        _log("out", "[i] 系统代理已还原")
    except Exception as e:
        prompt("代理还原失败", f"系统代理还原失败：{e}\n请手动恢复原代理设置。")


def detect_stale_proxy() -> None:
    """兜底：检测上次异常退出残留的系统代理（指向本机且端口已无服务），自动关闭还原。

    覆盖两种残留：enable=1（代理仍启用）和 enable=0 但 server 字符串残留。
    端口仍有监听时不动（可能是我们自己或别的服务在跑）。
    """
    if os.name != "nt":
        return
    try:
        enable, server, override = read_proxy_reg()
        if not server.startswith("127.0.0.1:"):
            return
        port_s = server.rsplit(":", 1)[1]
        if not port_s.isdigit():
            return
        # 端口有监听 = 可能仍是我们或别的服务在跑，不动；无监听 = 残留
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            in_use = s.connect_ex(("127.0.0.1", int(port_s))) == 0
        if not in_use:
            write_proxy_reg(False, "", "")
            notify_proxy_changed()
            _log("out", f"[i] 检测到上次残留的系统代理 {server}（端口无服务），已自动关闭")
    except Exception as e:
        _log("err", f"[warn] 残留代理检测失败: {e}")


# ---------------- DNS 加速模式 ----------------
def parse_dns_question(data: bytes):
    """解析 DNS 查询，返回 (qname, qtype, 问题区结束偏移) 或 None。"""
    if len(data) < 12:
        return None
    qd = struct.unpack(">H", data[4:6])[0]
    if qd == 0:
        return None
    off = 12
    labels = []
    while off < len(data):
        ln = data[off]
        if ln == 0:
            off += 1
            break
        if ln & 0xC0 == 0xC0:  # 压缩指针（查询里极少见）
            off += 2
            break
        off += 1
        if off + ln > len(data):
            return None
        labels.append(data[off:off + ln])
        off += ln
    if off + 4 > len(data):
        return None
    qtype, _qclass = struct.unpack(">HH", data[off:off + 4])
    qname = ".".join(l.decode("ascii", "replace") for l in labels).lower()
    return qname, qtype, off + 4


def build_dns_response(query: bytes, qend: int, answers) -> bytes:
    """构造 DNS 响应；answers: [(qtype, ttl, rdata_bytes)]。"""
    tid = query[0:2]
    flags = 0x8180  # QR=1, RD=1, RA=1, RCODE=0
    resp = tid + struct.pack(">HHHHH", flags, 1, len(answers), 0, 0)
    resp += query[12:qend]  # 原样回显问题区
    for qtype, ttl, rdata in answers:
        resp += b"\xc0\x0c"  # 指针指向问题区域名(偏移12)
        resp += struct.pack(">HHIH", qtype, 1, ttl, len(rdata)) + rdata
    return resp


def dns_forward(query: bytes, upstream: str, timeout: float = 3.0) -> bytes | None:
    """转发到上游 DNS，失败返回 None。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(query, (upstream, 53))
        data, _ = s.recvfrom(4096)
        return data
    except OSError:
        return None
    finally:
        s.close()


# ---------------- DoH 上游（DNS over HTTPS 转发） ----------------
DOH_SERVERS = [
    {"name": "腾讯 DNSPod 1.12.12.12", "url": "https://1.12.12.12/resolve"},
    {"name": "DNSPod doh.pub", "url": "https://doh.pub/resolve"},
    {"name": "阿里 120.53.53.53", "url": "https://120.53.53.53/resolve"},
    {"name": "阿里 dns.alidns.com", "url": "https://dns.alidns.com/resolve"},
    {"name": "阿里 223.6.6.6", "url": "https://223.6.6.6/resolve"},
    {"name": "阿里 223.5.5.5", "url": "https://223.5.5.5/resolve"},
    {"name": "Google dns.google", "url": "https://dns.google/resolve"},
    {"name": "Cloudflare cloudflare-dns.com", "url": "https://cloudflare-dns.com/resolve"},
    {"name": "360 doh.360.cn", "url": "https://doh.360.cn/resolve"},
    {"name": "清华 TUNA 101.6.6.6:8443", "url": "https://101.6.6.6:8443/resolve"},
]

_DOH_PROTO_CACHE: dict = {}   # {endpoint: "rfc8484"|"json"}，协议自动探测结果缓存
_DOH_CACHE_LOCK = threading.Lock()


def encode_qname(name: str) -> bytes:
    """域名 -> DNS qname 字节（JSON 响应的 CNAME data 需要）。"""
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("ascii", "ignore")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _doh_rfc8484_query(base_url: str, query: bytes, timeout: float) -> bytes:
    """RFC 8484：POST {base}/dns-query（application/dns-message），返回二进制响应。"""
    url = base_url.rstrip("/") + "/dns-query"
    req = urllib.request.Request(
        url, data=query, method="POST",
        headers={"Content-Type": "application/dns-message",
                 "Accept": "application/dns-message"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _doh_json_query(base_url: str, qname: str, qtype: int, timeout: float) -> list:
    """JSON API：GET {base}/resolve?name=&type=，返回 [(qtype, ttl, rdata_bytes)]。
    base_url 可含或不含 /resolve 结尾（幂等）；/resolve 端点通常只支持
    A/AAAA/CNAME，其余类型返回空列表（调用方给空应答）。
    """
    _QTYPE_NAME = {1: "A", 5: "CNAME", 28: "AAAA", 15: "MX", 16: "TXT",
                   2: "NS", 6: "SOA", 255: "ANY"}
    tname = _QTYPE_NAME.get(qtype, str(qtype))
    base = base_url.rstrip("/")
    if not base.endswith("/resolve"):
        base += "/resolve"
    url = f"{base}?{urllib.parse.urlencode({'name': qname, 'type': tname})}"
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8", "replace"))
    answers = []
    for a in obj.get("Answer", []):
        t = int(a.get("type", 0))
        data_s = str(a.get("data", ""))
        ttl = int(a.get("TTL", 60) or 60)
        try:
            if t == 1:
                answers.append((1, ttl, socket.inet_aton(data_s)))
            elif t == 28:
                answers.append((28, ttl, socket.inet_pton(socket.AF_INET6, data_s)))
            elif t == 5:
                answers.append((5, ttl, encode_qname(data_s)))
        except (OSError, ValueError):
            continue  # 跳过无法编码的记录
    return answers


def dns_forward_doh(query: bytes, qname: str, qtype: int, qend: int,
                    endpoint: str, timeout: float = 3.0) -> bytes | None:
    """DoH 转发非加速域名。自动探测协议：优先 RFC 8484，失败降级 JSON /resolve。
    两者都失败返回 None（调用方回退 UDP 上游）。探测结果缓存到 _DOH_PROTO_CACHE。
    """
    base = endpoint.rstrip("/")
    if base.endswith("/resolve"):
        json_base, rfc_base = base, base[:-len("/resolve")]
    else:
        json_base, rfc_base = base + "/resolve", base

    def try_rfc():
        return _doh_rfc8484_query(rfc_base, query, timeout)

    def try_json():
        return build_dns_response(query, qend,
                                  _doh_json_query(json_base, qname, qtype, timeout))

    with _DOH_CACHE_LOCK:
        proto = _DOH_PROTO_CACHE.get(endpoint)
    if proto == "json":  # 已探测为 JSON：直接用，失败不降级（留给 UDP 回退）
        try:
            return try_json()
        except Exception:
            return None
    try:  # 未知 / 已探测为 RFC8484：先走 RFC8484
        resp = try_rfc()
        with _DOH_CACHE_LOCK:
            _DOH_PROTO_CACHE[endpoint] = "rfc8484"
        return resp
    except Exception:
        pass
    try:  # 降级 JSON
        resp = try_json()
        with _DOH_CACHE_LOCK:
            _DOH_PROTO_CACHE[endpoint] = "json"
        return resp
    except Exception:
        return None


def detect_best_doh(candidates=None, timeout: float = 3.0):
    """自动从 DOH_SERVERS（或指定 candidates，元素为 url 字符串或含 "url" 的 dict）中
    并行实测选最快可用端点。返回 (endpoint_url, min_elapsed_秒)；全部失败返回 ("", 0.0)。
    用于 GUI 未指定 DoH 端点时自动选择最优上游。
    """
    src = [s if isinstance(s, str) else s["url"] for s in (candidates or DOH_SERVERS)]
    results = {}

    def probe(url):
        try:
            t0 = time.perf_counter()
            q = _make_query("github.com", 1)
            resp = dns_forward_doh(q, "github.com", 1, 0, url, timeout)
            if resp:
                results[url] = time.perf_counter() - t0
        except Exception:
            pass

    threads = [threading.Thread(target=probe, args=(u,), daemon=True) for u in src]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if not results:
        return ("", 0.0)
    best = min(results, key=results.get)
    return (best, results[best])


def _make_query(qname: str, qtype: int = 1, qid: int = 0x4A21) -> bytes:
    """构造最小 DNS 查询包（用于 DoH 端点测速）。"""
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    return header + encode_qname(qname) + struct.pack(">HH", qtype, 1)


def start_dns(port: int, upstream: str, ip_map: dict,
              recheck: float = 0.0, timeout: float = 2.0,
              use_doh: bool = False, doh_endpoint: str = "") -> dict:
    """启动 DNS 加速服务（非阻塞）。返回 handle dict 供 stop_dns 停止。

    端口占用自适应：请求端口被占用时自动改用下一个端口（最多 +PORT_RETRY-1），
    实际端口写入 handle["port"]。
    use_doh=True 时，未命中加速清单的域名先经 DoH 转发（RFC8484/JSON 自动探测），
    失败回退 UDP upstream。
    """
    # UDP bind 循环：plain socket 不开 SO_REUSEADDR，Windows 下被占用必报 10048
    s = None
    actual = port
    for candidate in range(port, port + PORT_RETRY):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", candidate))
            actual = s.getsockname()[1]  # port==0 时取系统分配的端口
            break
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            s = None
    if s is None:
        raise OSError(f"无法绑定 UDP 端口 {port}-{port + PORT_RETRY - 1}（全被占用）")
    s.settimeout(0.5)
    stop_event = threading.Event()
    handle = {"sock": s, "stop": stop_event, "port": actual}
    if port != 0 and actual != port:
        _log("out", f"[warn] 端口 {port} 已被占用，已自动改用 UDP 端口 {actual}")
    if use_doh:
        _log("out", f"[i] DNS 加速服务已启动: 127.0.0.1:{actual}  (DoH 上游 {doh_endpoint}, 失败回退 UDP {upstream}, 加速 {len(ip_map)} 个域名+子域)")
    else:
        _log("out", f"[i] DNS 加速服务已启动: 127.0.0.1:{actual}  (上游 {upstream}:53, 加速 {len(ip_map)} 个域名+子域)")
    if recheck > 0:
        t = threading.Thread(target=recheck_loop,
                             kwargs={"interval": recheck, "timeout": timeout,
                                     "stop_event": stop_event},
                             daemon=True)
        t.start()
        handle["recheck_t"] = t
        _log("out", f"[i] 已启动后台 IP 复检（每 {recheck:g} 秒）…")

    def _serve(sock: socket.socket, upstream_: str, ip_map_: dict,
               use_doh_: bool = False, doh_endpoint_: str = "") -> None:
        try:
            while not stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                try:
                    parsed = parse_dns_question(data)
                    if parsed is None:
                        continue
                    qname, qtype, qend = parsed
                    # match_base + 读取放进同一把锁：避免复检 clear/update 后 base 已失效
                    with IP_MAP_LOCK:
                        base = match_base(qname, ip_map_)
                        ips = list(ip_map_.get(base, ())) if base is not None else []
                    if base is not None:
                        if qtype in (1, 255) and ips:  # A / ANY：返回全部健康 IP，客户端可自动回退
                            answers = [(1, 60, socket.inet_aton(ip)) for ip in ips]
                            resp = build_dns_response(data, qend, answers)
                            _log("out", f"[dns] {qname} -> {', '.join(ips)}  (加速命中 {base})")
                        else:  # AAAA 等：空应答
                            resp = build_dns_response(data, qend, [])
                    else:
                        if use_doh_:
                            resp = dns_forward_doh(data, qname, qtype, qend,
                                                   doh_endpoint_, timeout)
                            if resp is not None:
                                _log("out", f"[dns] {qname} -> DoH 转发 {doh_endpoint_}")
                            else:
                                _log("err", f"[dns] {qname} -> DoH 失败，回退 UDP {upstream_}")
                                resp = dns_forward(data, upstream_)
                                if resp is not None:
                                    _log("out", f"[dns] {qname} -> 转发 {upstream_}")
                        else:
                            resp = dns_forward(data, upstream_)
                            if resp is not None:
                                _log("out", f"[dns] {qname} -> 转发 {upstream_}")
                        if resp is None:  # 上游不可达时给 SERVFAIL
                            resp = data[0:2] + struct.pack(">HHHHH", 0x8182, 1, 0, 0, 0) + data[12:qend]
                    sock.sendto(resp, addr)
                except Exception as e:  # 单条查询失败不影响服务
                    _log("err", f"[dns] 处理异常: {e}")
        except OSError as e:
            if not stop_event.is_set():
                _log("err", f"[dns] 服务异常: {e}")
        finally:
            sock.close()
            _log("out", "\n[i] DNS 服务已停止")

    t = threading.Thread(target=_serve,
                         args=(s, upstream, ip_map, use_doh, doh_endpoint),
                         daemon=True)
    t.start()
    handle["serve_t"] = t
    return handle


def stop_dns(handle: dict) -> None:
    """停止 DNS 服务（stop_event 置位，循环内超时检查退出）。"""
    if not handle:
        return
    stop = handle.get("stop")
    if stop:
        stop.set()
    try:
        handle["serve_t"].join(timeout=2.0)
    except Exception:
        pass


# ---------------- HTTP 加速模式 ----------------
class DirectConn(http.client.HTTPConnection):
    """用指定 IP 直连，但 Host 头保持原始域名（支持虚拟主机/加速域名，且强制 IPv4）。"""

    def __init__(self, host, port, ip, timeout=30):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


# ---------------- 失败 IP 冷却 + 并发建连（解决"时通时不通"） ----------------
# GitHub 等加速域名的边缘 IP 是动态的：某 IP 探测时可用，几分钟后可能失效。
# 原实现串行逐 IP 尝试，列表第一个死 IP 会让请求阻塞长达 10~30s，浏览器侧表现为
# "时通时不通"。修复：把候选 IP 并发建连（谁先通用谁）+ 失败的 IP 进入短期冷却
# （90s 内不再优先尝试），下个请求直接跳过坏 IP，等复检/自然过期后自愈。
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN: dict = {}  # ip -> 冷却到期时间戳


def _mark_failed(ip: str | None) -> None:
    """把某候选 IP 标记为短期失败（90s 冷却）。空值忽略。"""
    if not ip:
        return
    with _COOLDOWN_LOCK:
        _COOLDOWN[ip] = time.time() + 90.0


# ---------------- 按需触发复检（失败即重检，近似 Steam++ 实时动态解析） ----------------
# 候选 IP 是启动快照 + 60s 固定周期复检；坏 IP 会让请求失败（已进 90s 冷却）。
# Steam++ 是每次连接都实时做多源 DNS 解析，成本偏高；折中：单次请求所有候选都
# 失败时立即置位信号，唤醒复检线程提前执行一轮 health_check，快速淘汰死 IP，
# 这样后续请求拿到的是还算新鲜的 IP 列表。
_RECHECK_REQUEST = threading.Event()
_LAST_ONDEMAND_TS = 0.0    # 最近一次按需复检时刻（monotonic 秒），防抖
_ONDEMAND_MIN_GAP = 5.0    # 两次按需复检最小间隔（秒），防止网络全挂时连续打探测


def request_recheck() -> None:
    """请求立即执行一轮 IP 复检（非阻塞）：置位信号，recheck_loop 下个检查点被唤醒。"""
    _RECHECK_REQUEST.set()


def _prune_cooldown() -> None:
    """清理已过期的冷却记录。"""
    now = time.time()
    with _COOLDOWN_LOCK:
        expired = [k for k, v in _COOLDOWN.items() if v <= now]
        for k in expired:
            _COOLDOWN.pop(k, None)


def _usable_candidates(ips: list) -> list:
    """过滤冷却中的 IP；若全部冷却则原样返回（宁可慢试也不断网兜底）。"""
    _prune_cooldown()
    with _COOLDOWN_LOCK:
        ok = [ip for ip in ips if ip not in _COOLDOWN]
    return ok or list(ips)


def _fast_connect(host: str, port: int, ips: list, timeout: float = 4.0) -> tuple:
    """并发尝试候选 IP，返回 (socket, 实际使用的 IP)。

    - 加速域名传候选 IP 列表：并发建连，谁先成功用谁（其余立即关闭），
      避免串行等待死 IP 造成页面卡死。全失败返回 (None, 最后一次错误)。
    - 非加速域名传空列表：交给系统解析（inline 回退）。
    注意：返回的 socket 连接超时 = timeout，调用方如需长传输应再 settimeout。
    """
    if not ips:
        try:
            return socket.create_connection((host, port), timeout), host
        except OSError as e:
            return None, e
    lock_ = threading.Lock()
    first = [None]  # 成功: (sock, ip)；失败: OSError
    stop = threading.Event()

    def worker(ip: str) -> None:
        try:
            s = socket.create_connection((ip, port), timeout)
            with lock_:
                if first[0] is None or isinstance(first[0], OSError):
                    first[0] = (s, ip)
                    stop.set()
                else:
                    try:
                        s.close()
                    except OSError:
                        pass
        except OSError as e:
            with lock_:
                if not stop.is_set() and (first[0] is None or isinstance(first[0], OSError)):
                    first[0] = e

    ws = [threading.Thread(target=worker, args=(ip,), daemon=True) for ip in ips]
    for t in ws:
        t.start()
    # 成功信号（stop 仅在 worker 成功时置位）；拿不到成功就等全部线程结束（全失败）
    stop.wait(timeout + 1.0)
    with lock_:
        r = first[0]
    if r is not None and not isinstance(r, OSError):
        return r  # 已成功：立即返回，不等待仍在超时的失败连接（daemon 线程自灭）
    for t in ws:
        t.join()
    r = first[0]
    if r is None or isinstance(r, OSError):
        return None, (r if isinstance(r, OSError) else OSError("all candidates failed"))
    return r


class ProxyHandler(BaseHTTPRequestHandler):
    ip_map: dict = {}
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        _log("err", "[proxy] %s" % (fmt % args))

    def _relay(self, method: str) -> None:
        """HTTP 正向代理：绝对 URI 形式，加速域名直连优选 IP。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._error(413, "body too large")
            return
        body = self.rfile.read(length) if length else b""
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.scheme or not parsed.hostname:
            self._error(400, "absolute URL required for proxy request")
            return
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target_ips = resolve_host(hostname, self.ip_map)
        if target_ips:
            _log("out", f"[proxy] {hostname}:{port} -> 直连优选IP {target_ips[0]}" + (f" (+{len(target_ips)-1}备用)" if len(target_ips) > 1 else ""))
        else:
            # 非加速域名：强制解析 IPv4 直连，避免 IPv6 慢路径
            try:
                target_ips = [socket.getaddrinfo(hostname, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]]
            except OSError:
                target_ips = []

        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("proxy-connection", "connection", "keep-alive", "host", "content-length")}
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        conn = None
        resp = None
        last_err = None
        # 并发建连（加速域名逐候选 IP 同时尝试，谁先通用谁）避免坏 IP 阻塞；
        # 失败后把候选 IP 全部冷却，下个请求直接跳开。非加速域名 target_ips 为空，
        # _fast_connect 走系统解析。
        sock, ip_used = _fast_connect(hostname, port, _usable_candidates(target_ips or []), timeout=30)
        if sock is None:
            last_err = ip_used
            for ip in target_ips or []:
                _mark_failed(ip)
            if target_ips:
                request_recheck()  # 加速候选全挂：唤醒复检线程提前淘汰死 IP
        else:
            try:
                # 加速域名：ip_used 是优选 IP；非加速域名：_fast_connect 已按系统解析建连。
                # 直接把已建立的 socket 注入连接对象，跳过其内部 connect()。
                conn = DirectConn(hostname, port, ip_used, timeout=30)
                conn.sock = sock
                if body:  # 空 body 不传，避免 Content-Length:0 触发上游慢路径
                    conn.request(method, path, body=body, headers=hdrs)
                else:
                    conn.request(method, path, headers=hdrs)
                resp = conn.getresponse()
            except Exception as e:
                last_err = e
                if conn:
                    try:
                        conn.close()
                    except OSError:
                        pass
        if resp is None:
            self._error(502, f"upstream error: {last_err}")
            return
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "connection", "keep-alive"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")  # 用连接关闭界定结束，支持流式转发
        self.end_headers()
        try:
            # 流式转发：边读上游边 flush，避免 chunked 无 Content-Length 的响应被整包读慢
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.close_connection = True
            conn.close()

    def do_CONNECT(self) -> None:
        """HTTPS 隧道：加速域名直连优选 IP，其余正常。不做 MITM。"""
        hostport = self.path
        host, _, port = hostport.rpartition(":")
        try:
            port = int(port or 443)
        except ValueError:
            self._error(400, "bad CONNECT target")
            return
        target_ips = resolve_host(host, self.ip_map)
        if target_ips:
            _log("out", f"[proxy] CONNECT {host}:{port} -> 直连优选IP {target_ips[0]}" + (f" (+{len(target_ips)-1}备用)" if len(target_ips) > 1 else ""))
        up = None
        last_err = None
        # 并发尝试候选 IP（加速域名）：谁先连上用谁；失败 IP 进 90s 冷却
        sock, ip_used = _fast_connect(host, port, _usable_candidates(target_ips or []), timeout=10)
        if sock is None:
            last_err = ip_used
            for ip in target_ips or []:
                _mark_failed(ip)  # 全部失败：标记冷却，避免下个请求继续撞
            if target_ips:
                request_recheck()  # 加速候选全挂：唤醒复检线程提前淘汰死 IP
        else:
            up = sock
            up.settimeout(60)
        if up is None:
            self._error(502, f"connect failed: {last_err}")
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        down = self.connection
        down.settimeout(60)
        # up.settimeout(60) 已在建连后设置
        stop = threading.Event()

        def pump(src, dst):
            try:
                while not stop.is_set():
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                stop.set()

        t1 = threading.Thread(target=pump, args=(down, up), daemon=True)
        t2 = threading.Thread(target=pump, args=(up, down), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.close_connection = True  # 隧道结束直接关闭连接，避免 handler 再读请求报错
        try:
            up.close()
        except OSError:
            pass

    def _error(self, code: int, msg: str) -> None:
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass

    do_GET = lambda self: self._relay("GET")
    do_POST = lambda self: self._relay("POST")
    do_PUT = lambda self: self._relay("PUT")
    do_DELETE = lambda self: self._relay("DELETE")
    do_PATCH = lambda self: self._relay("PATCH")
    do_HEAD = lambda self: self._relay("HEAD")
    do_OPTIONS = lambda self: self._relay("OPTIONS")


class QuietServer(ThreadingHTTPServer):
    """只打印一行异常摘要，不打长 traceback（客户端断开等常见情况）。"""

    def handle_error(self, request, client_address):
        _log("err", f"[proxy] 连接处理异常: {client_address}")


def _tcp_port_free(port: int) -> bool:
    """探测本机 TCP 端口是否空闲（裸 bind 试探，不开 SO_REUSEADDR）。

    Windows 上 SO_REUSEADDR 语义与 Linux 不同（允许重复绑定），
    直接裸 bind 才能可靠区分"端口被占用"。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------- Watchdog：被强制结束任务时自动还原系统代理 ----------------
# 主进程被任务管理器/其他方式 TerminateProcess 强杀时，AppDomain 内任何 finally、
# CTRL_CLOSE_EVENT 回调都不会执行（进程直接消失），唯一可靠兜底是独立子进程。
# start_proxy 设置系统代理后 spawn 一个隐藏 watchdog 子进程监控 owner_pid；
# 正常停止（stop_proxy）时删除标记文件并杀掉 watchdog；owner 被强杀时 watchdog
# 存活并接管把系统代理写回 saved 值。
_WATCHDOG_CODE = r"""
import sys, time, os, ctypes
owner_pid, marker = int(sys.argv[1]), sys.argv[2]
saved = sys.argv[3:]                     # [enable, server, override]
k = ctypes.windll.kernel32
h = k.OpenProcess(0x1000, False, owner_pid)   # PROCESS_QUERY_LIMITED_INFORMATION
STILL_ACTIVE = 259
while True:
    time.sleep(3)
    if not os.path.exists(marker):
        break                            # 主进程正常停止（标记已删除），不还原
    dead = False
    if h:
        code = ctypes.c_ulong(0)
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            dead = True
        elif code.value != STILL_ACTIVE:
            dead = True
    if not dead:
        continue                          # 主进程还活着，继续等
    # 主进程被强杀：接管还原系统代理
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as rk:
            winreg.SetValueEx(rk, "ProxyEnable", 0, winreg.REG_DWORD, 1 if saved[0] != "0" else 0)
            winreg.SetValueEx(rk, "ProxyServer", 0, winreg.REG_SZ, saved[1])
            winreg.SetValueEx(rk, "ProxyOverride", 0, winreg.REG_SZ, saved[2])
        ctypes.windll.wininet.InternetSetOptionW(None, 39, None, 0)
        ctypes.windll.wininet.InternetSetOptionW(None, 37, None, 0)
    finally:
        try:
            os.remove(marker)
        except OSError:
            pass
"""


def _spawn_watchdog(owner_pid: int, marker: str, saved_proxy) -> int | None:
    """spawn 隐藏 watchdog 子进程（pythonw -c，无控制台窗口）。返回 pid 或 None。"""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        alt = exe[:-4] + "w.exe"
        if os.path.exists(alt):
            exe = alt
    argv = [exe, "-c", _WATCHDOG_CODE, str(owner_pid), marker,
            str(saved_proxy[0]), str(saved_proxy[1]), str(saved_proxy[2])]
    try:
        p = subprocess.Popen(argv, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             close_fds=True)
        return p.pid
    except Exception:
        return None


def _kill_watchdog(pid: int) -> None:
    """终止 watchdog 子进程（尽力而为）。"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if h:
            k.TerminateProcess(h, 0)
            k.CloseHandle(h)
    except Exception:
        pass


def start_proxy(port: int, ip_map: dict, set_system_proxy: bool = True,
                recheck: float = 0.0, timeout: float = 2.0) -> dict:
    """启动 HTTP 加速代理（非阻塞）。返回 handle dict 供 stop_proxy 停止。

    端口占用自适应：请求端口被占用时自动改用下一个端口（最多 +PORT_RETRY-1），
    实际端口写入 handle["port"]，系统代理同步写入实际端口。
    """
    # 必须先清理残留再创建 server：server 构造会 bind 端口，
    # 若先 bind，detect_stale_proxy 会误判"端口有服务"而跳过清理。
    if set_system_proxy and os.name == "nt":
        detect_stale_proxy()

    # 端口占用检测：先裸 bind 试探（可靠报 10048），空出后再交给 QuietServer 正式监听；
    # 构造仍失败（竞态/其他原因）则继续下一个端口。
    server = None
    actual = port
    for candidate in range(port, port + PORT_RETRY):
        if not _tcp_port_free(candidate):
            continue
        try:
            server = QuietServer(("127.0.0.1", candidate), ProxyHandler)
            actual = server.server_address[1]  # port==0 时取系统分配的端口
            break
        except OSError:
            server = None
            continue
    if server is None:
        raise OSError(f"无法绑定 TCP 端口 {port}-{port + PORT_RETRY - 1}（全被占用）")
    server.daemon_threads = True
    ProxyHandler.ip_map = ip_map
    if port != 0 and actual != port:
        _log("out", f"[warn] 端口 {port} 已被占用，已自动改用 TCP 端口 {actual}")
    _log("out", f"[i] HTTP 加速代理已启动: http://127.0.0.1:{actual}  (加速 {len(ip_map)} 个域名, 无 MITM)")

    saved_proxy = None
    console_handler = None  # 保持 SetConsoleCtrlHandler 回调引用，防被 GC
    watchdog_pid = None
    marker = None
    if set_system_proxy:
        if os.name != "nt":
            _log("out", "[i] 非 Windows，跳过自动设置系统代理（请在程序里手动配置代理）")
        else:
            saved_proxy = read_proxy_reg()
            try:
                write_proxy_reg(True, f"127.0.0.1:{actual}", "localhost;127.0.0.1;<local>")
                notify_proxy_changed()
                _log("out", f"[i] 已自动设置系统代理: 127.0.0.1:{actual}（退出时自动还原）")
                # Watchdog：被强制结束任务（TerminateProcess）时自动还原系统代理。
                # 标记文件供 watchdog 判断"正常停止"（stop_proxy 会删除它）。
                marker = os.path.join(tempfile.gettempdir(), f"accel_watchdog_{os.getpid()}")
                try:
                    with open(marker, "w") as f:
                        f.write(str(os.getpid()))
                except OSError:
                    marker = None
                    _log("err", "[warn] 无法写入 watchdog 标记文件，被强制结束任务时不会自动还原")
                if marker:
                    watchdog_pid = _spawn_watchdog(os.getpid(), marker, saved_proxy)
                    if watchdog_pid is None:
                        _log("err", "[warn] watchdog 启动失败，被强制结束任务时不会自动还原")
                    else:
                        _log("out", f"[i] watchdog 已启动（pid={watchdog_pid}）：被强制结束任务时自动还原系统代理")
                # 注册「窗口 X / 关闭」事件：点叉直接终止进程时 finally 不会执行，
                # 必须在这里拦截 CTRL_CLOSE_EVENT 立即还原。
                try:
                    import ctypes
                    saved_ref = saved_proxy

                    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
                    def _on_close(ctrl_type: int) -> int:
                        if ctrl_type == 2:  # CTRL_CLOSE_EVENT：用户点了窗口 X
                            try:
                                restore_proxy(saved_ref)
                            except Exception:
                                pass
                        return 1  # 返回 TRUE：表示已处理，系统等待清理完成后终止进程

                    ctypes.windll.kernel32.SetConsoleCtrlHandler(_on_close, True)
                    console_handler = _on_close
                except Exception:
                    pass  # 非 Windows / 无控制台时不注册
            except Exception as e:
                prompt("代理设置失败",
                       f"自动设置系统代理失败：{e}\n"
                       f"请在 Windows 设置→网络→代理 中手动把代理设为 127.0.0.1:{actual}，"
                       f"或以管理员身份运行本程序。")
    else:
        _log("out", f"[i] 已跳过自动设置系统代理（--no-set-proxy），请手动把代理设为 127.0.0.1:{actual}")

    stop_event = threading.Event()
    handle = {"server": server, "saved_proxy": saved_proxy,
              "console_handler": console_handler, "stop": stop_event,
              "port": actual, "watchdog_pid": watchdog_pid, "marker": marker}
    if recheck > 0:
        t = threading.Thread(target=recheck_loop,
                             kwargs={"interval": recheck, "timeout": timeout,
                                     "stop_event": stop_event},
                             daemon=True)
        t.start()
        handle["recheck_t"] = t
        _log("out", f"[i] 已启动后台 IP 复检（每 {recheck:g} 秒）…")

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    handle["serve_t"] = t
    return handle


def stop_proxy(handle: dict) -> None:
    """停止 HTTP 加速代理：停复检 → shutdown server → 还原系统代理。"""
    if not handle:
        return
    stop = handle.get("stop")
    if stop:
        stop.set()
    server = handle.get("server")
    if server:
        try:
            server.shutdown()  # serve_forever 阻塞中，shutdown 触发退出
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
    try:
        handle["serve_t"].join(timeout=3.0)
    except Exception:
        pass
    # 撤销 watchdog：先删标记（watchdog 见到标记消失即退出，不会还原），再杀进程
    marker = handle.get("marker")
    if marker:
        try:
            os.remove(marker)
        except OSError:
            pass
    wpid = handle.get("watchdog_pid")
    if wpid:
        _kill_watchdog(wpid)
    restore_proxy(handle.get("saved_proxy"))


# ---------------- 入口 ----------------
def main() -> int:
    p = argparse.ArgumentParser(description="hosts_accel 的 DNS / HTTP 加速服务（无 MITM）")
    p.add_argument("mode", choices=["dns", "proxy"], help="dns=本地智能DNS；proxy=HTTP正向代理")
    p.add_argument("--port", type=int, default=None, help="端口（dns 默认5353，proxy 默认8080）")
    p.add_argument("--upstream", default="223.5.5.5", help="DNS 上游（默认 223.5.5.5 阿里DNS）")
    p.add_argument("--doh", default="",
                   help="DoH 端点。dns 模式下填写则未命中加速清单的域名经 DoH 转发"
                        "（RFC8484/JSON 自动探测，失败回退 UDP 上游）。可填内置别名如 1.12.12.12 / "
                        "doh.pub / dns.alidns.com / dns.google / doh.360.cn 或完整 URL")
    p.add_argument("--no-set-proxy", action="store_true", help="proxy 模式下不要自动设置系统代理")
    p.add_argument("--recheck", type=float, default=60.0,
                   help="后台 IP 复检间隔秒（默认 60；设 0 关闭复检）")
    args = p.parse_args()

    _log("out", "[i] 正在构建加速域名->IP 映射（解析中）…")
    ip_map = build_ip_map()
    if not ip_map:
        _log("err", "[!] 没有任何可加速的 IP（全部解析失败？）")
        return 1
    IP_MAP.clear()
    IP_MAP.update(ip_map)
    _log("out", f"[i] 共 {len(ip_map)} 个加速域名可用")

    # start_dns/start_proxy 内建复检线程（recheck>0 时），不再手动启动
    # DoH 端点解析：别名（如 1.12.12.12 / doh.pub）匹配 DOH_SERVERS；否则按完整 URL 处理
    doh_endpoint = ""
    if args.doh:
        target = args.doh.strip()
        matched = next((s["url"] for s in DOH_SERVERS if target in s["url"]), None)
        doh_endpoint = matched or (target if target.startswith("http") else f"https://{target}")

    handle = None
    try:
        if args.mode == "dns":
            handle = start_dns(args.port if args.port is not None else 5353,
                               args.upstream, IP_MAP, recheck=args.recheck,
                               use_doh=bool(doh_endpoint), doh_endpoint=doh_endpoint)
            stop = stop_dns
        else:
            handle = start_proxy(args.port if args.port is not None else 8080,
                                 IP_MAP, set_system_proxy=not args.no_set_proxy,
                                 recheck=args.recheck)
            stop = stop_proxy
    except OSError as e:
        _log("err", f"[!] 启动失败（端口全被占用）: {e}")
        return 1
    _log("out", f"[i] 服务已启动，实际端口: {handle.get('port')}")

    try:
        # 服务与复检线程均为 daemon，主线程阻塞等待 Ctrl+C
        threading.Event().wait()
    except KeyboardInterrupt:
        _log("out", "\n[i] 收到 Ctrl+C，正在停止…")
    finally:
        stop(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
