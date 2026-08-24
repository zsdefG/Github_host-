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
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 复用 hosts_accel 的域名清单 / 优选 IP / 解析逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hosts_accel

MAX_BODY = 8_000_000  # HTTP 代理请求体上限

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
            result[base] = ips
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
    print("[i] 对候选 IP 做连通性健康检测（TCP 443）…", flush=True)
    return health_check(ip_map)


def match_base(domain: str, ip_map: dict):
    """匹配加速域名（精确或子域），返回最长命中的基础域名；无则 None。"""
    domain = domain.rstrip(".").lower()
    best = None
    for base in ip_map:
        if domain == base or domain.endswith("." + base):
            if best is None or len(base) > len(best):
                best = base
    return best


def resolve_host(hostname: str, ip_map: dict):
    """加速域名返回优选 IP 列表（已按健康检测排序），否则返回 None（走正常解析）。"""
    base = match_base(hostname, ip_map)
    if base and ip_map.get(base):
        return ip_map[base]
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


def run_dns(port: int, upstream: str, ip_map: dict) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    s.settimeout(0.5)
    print(f"[i] DNS 加速服务已启动: 127.0.0.1:{port}  (上游 {upstream}:53, 加速 {len(ip_map)} 个域名+子域)")
    print("[i] Ctrl+C 停止")
    try:
        while True:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                break
            try:
                parsed = parse_dns_question(data)
                if parsed is None:
                    continue
                qname, qtype, qend = parsed
                base = match_base(qname, ip_map)
                if base is not None:
                    ips = ip_map[base]
                    if qtype in (1, 255) and ips:  # A / ANY：返回全部健康 IP，客户端可自动回退
                        answers = [(1, 60, socket.inet_aton(ip)) for ip in ips]
                        resp = build_dns_response(data, qend, answers)
                        print(f"[dns] {qname} -> {', '.join(ips)}  (加速命中 {base})")
                    else:  # AAAA 等：空应答
                        resp = build_dns_response(data, qend, [])
                else:
                    resp = dns_forward(data, upstream)
                    if resp is None:
                        resp = build_dns_response(data, qend, [])
                        # 上游不可达时给 SERVFAIL
                        resp = data[0:2] + struct.pack(">HHHHH", 0x8182, 1, 0, 0, 0) + data[12:qend]
                    else:
                        print(f"[dns] {qname} -> 转发 {upstream}")
                s.sendto(resp, addr)
            except Exception as e:  # 单条查询失败不影响服务
                print(f"[dns] 处理异常: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
        print("\n[i] DNS 服务已停止")


# ---------------- HTTP 加速模式 ----------------
class DirectConn(http.client.HTTPConnection):
    """用指定 IP 直连，但 Host 头保持原始域名（支持虚拟主机/加速域名，且强制 IPv4）。"""

    def __init__(self, host, port, ip, timeout=30):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class ProxyHandler(BaseHTTPRequestHandler):
    ip_map: dict = {}
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[proxy] %s\n" % (fmt % args))

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
            print(f"[proxy] {hostname}:{port} -> 直连优选IP {target_ips[0]}" + (f" (+{len(target_ips)-1}备用)" if len(target_ips) > 1 else ""))
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
        for ip in (target_ips or [None]):  # 逐 IP 尝试；None = 交给系统解析
            try:
                conn = DirectConn(hostname, port, ip, timeout=30) if ip \
                    else http.client.HTTPConnection(hostname, port, timeout=30)
                if body:  # 空 body 不传，避免 Content-Length:0 触发上游慢路径
                    conn.request(method, path, body=body, headers=hdrs)
                else:
                    conn.request(method, path, headers=hdrs)
                resp = conn.getresponse()
                break
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
            print(f"[proxy] CONNECT {host}:{port} -> 直连优选IP {target_ips[0]}" + (f" (+{len(target_ips)-1}备用)" if len(target_ips) > 1 else ""))
        up = None
        last_err = None
        for ip in (target_ips or [None]):  # 逐 IP 尝试；None = 交给系统解析
            try:
                up = socket.create_connection((ip or host, port), timeout=10)
                break
            except OSError as e:
                last_err = e
        if up is None:
            self._error(502, f"connect failed: {last_err}")
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        down = self.connection
        down.settimeout(60)
        up.settimeout(60)
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
        print(f"[proxy] 连接处理异常: {client_address}", file=sys.stderr)


def run_proxy(port: int, ip_map: dict, set_system_proxy: bool = True) -> None:
    server = QuietServer(("127.0.0.1", port), ProxyHandler)
    server.daemon_threads = True
    ProxyHandler.ip_map = ip_map
    print(f"[i] HTTP 加速代理已启动: http://127.0.0.1:{port}  (加速 {len(ip_map)} 个域名, 无 MITM)")

    saved_proxy = None
    if set_system_proxy:
        if os.name != "nt":
            print("[i] 非 Windows，跳过自动设置系统代理（请在程序里手动配置代理）")
        else:
            saved_proxy = read_proxy_reg()
            try:
                write_proxy_reg(True, f"127.0.0.1:{port}", "localhost;127.0.0.1;<local>")
                notify_proxy_changed()
                print(f"[i] 已自动设置系统代理: 127.0.0.1:{port}（退出时自动还原）")
            except Exception as e:
                prompt("代理设置失败",
                       f"自动设置系统代理失败：{e}\n"
                       f"请在 Windows 设置→网络→代理 中手动把代理设为 127.0.0.1:{port}，"
                       f"或以管理员身份运行本程序。")
    else:
        print(f"[i] 已跳过自动设置系统代理（--no-set-proxy），请手动把代理设为 127.0.0.1:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] 代理已停止")
    finally:
        server.server_close()
        if saved_proxy is not None:
            try:
                write_proxy_reg(saved_proxy[0], saved_proxy[1], saved_proxy[2])
                notify_proxy_changed()
                print("[i] 系统代理已还原")
            except Exception as e:
                prompt("代理还原失败", f"系统代理还原失败：{e}\n请手动恢复原代理设置。")


# ---------------- 入口 ----------------
def main() -> int:
    p = argparse.ArgumentParser(description="hosts_accel 的 DNS / HTTP 加速服务（无 MITM）")
    p.add_argument("mode", choices=["dns", "proxy"], help="dns=本地智能DNS；proxy=HTTP正向代理")
    p.add_argument("--port", type=int, default=None, help="端口（dns 默认5353，proxy 默认8080）")
    p.add_argument("--upstream", default="223.5.5.5", help="DNS 上游（默认 223.5.5.5 阿里DNS）")
    p.add_argument("--no-set-proxy", action="store_true", help="proxy 模式下不要自动设置系统代理")
    args = p.parse_args()

    print("[i] 正在构建加速域名->IP 映射（解析中）…")
    ip_map = build_ip_map()
    if not ip_map:
        print("[!] 没有任何可加速的 IP（全部解析失败？）", file=sys.stderr)
        return 1
    print(f"[i] 共 {len(ip_map)} 个加速域名可用")

    try:
        if args.mode == "dns":
            run_dns(args.port or 5353, args.upstream, ip_map)
        else:
            run_proxy(args.port or 8080, ip_map, set_system_proxy=not args.no_set_proxy)
    except OSError as e:
        print(f"[!] 启动失败（端口被占用或需管理员权限）: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
