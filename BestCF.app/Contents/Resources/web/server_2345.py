#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BestCF 2345 本地多网口直连测试服务 (macOS 方案C: 开屏网口选择 + 顶部实时热切换)
核心能力：
1. 自动枚举 macOS 活跃物理网卡 (en1 Wi-Fi / en0 Ethernet) 与虚拟隧道 (utun / 系统默认路由)
2. 选定物理网卡时，通过 macOS 内核 IP_BOUND_IF (25) 强制绑定该物理网卡收发 UDP DNS 与 TCP/TLS 流量，100% 跳过小火箭 Fake-IP 与其他网卡
3. 支持在启动前选择网口，以及在主界面顶部下拉框随时毫秒级热切换网口
4. 内置 TLS Keep-Alive 连接池，精确测量并透传 X-RTT-Ms 延迟
"""

import os
import re
import sys
import ssl
import time
import json
import queue
import socket
import struct
import subprocess
import urllib.parse
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 2345
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
IP_BOUND_IF = 25  # macOS XNU <netinet/in.h> IP_BOUND_IF

_state_lock = threading.Lock()
CURRENT_DEVICE = "en1"
CURRENT_IF_INDEX = 0

def list_macos_interfaces() -> list[dict]:
    """枚举本机所有活跃 IPv4 网卡及网关信息"""
    hw_map = {}
    try:
        out = subprocess.check_output(["networksetup", "-listallhardwareports"], text=True, timeout=2)
        for block in out.split("\n\n"):
            m_port = re.search(r"Hardware Port:\s*(.+)", block)
            m_dev = re.search(r"Device:\s*(\S+)", block)
            if m_port and m_dev:
                hw_map[m_dev.group(1).strip()] = m_port.group(1).strip()
    except Exception:
        pass

    devs = []
    try:
        if_out = subprocess.check_output(["ifconfig"], text=True, timeout=2)
        cur_dev = None
        for line in if_out.splitlines():
            if not line.startswith("\t") and not line.startswith(" ") and ":" in line:
                cur_dev = line.split(":", 1)[0].strip()
            elif cur_dev and "inet " in line:
                m_ip = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", line)
                if m_ip:
                    ip = m_ip.group(1)
                    if ip != "127.0.0.1":
                        devs.append((cur_dev, ip))
    except Exception:
        pass

    result = []
    for dev, ip in devs:
        if dev.startswith("utun") and ip.startswith("198.18."):
            name = "Shadowrocket TUN"
            gw = "代理隧道"
            kind = "tun"
        else:
            name = hw_map.get(dev, dev)
            gw = ""
            kind = "physical"
            if name in hw_map.values():
                try:
                    info = subprocess.check_output(["networksetup", "-getinfo", name], text=True, timeout=1.5)
                    m_gw = re.search(r"Router:\s*(\d+\.\d+\.\d+\.\d+)", info)
                    if m_gw:
                        gw = m_gw.group(1)
                except Exception:
                    pass
        gw_desc = gw if gw else "直连"
        result.append({
            "device": dev,
            "name": name,
            "ip": ip,
            "gateway": gw_desc,
            "kind": kind,
            "label": f"{name} ({dev} · {ip} · 网关 {gw_desc})"
        })

    result.sort(key=lambda x: (0 if x["device"] == "en1" else (1 if x["device"].startswith("en") else 2)))
    result.append({
        "device": "system",
        "name": "系统默认路由",
        "ip": "Auto",
        "gateway": "跟随系统/小火箭",
        "kind": "system",
        "label": "系统默认路由 (跟随系统优先级 / 小火箭)"
    })
    return result

def init_default_interface():
    global CURRENT_DEVICE, CURRENT_IF_INDEX
    candidates = ["en1", "en0"]
    for dev in candidates:
        try:
            idx = socket.if_nametoindex(dev)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, struct.pack("I", idx))
            s.settimeout(1.0)
            s.connect(("223.5.5.5", 53))
            lip = s.getsockname()[0]
            s.close()
            if lip and not lip.startswith("198.18."):
                CURRENT_DEVICE = dev
                CURRENT_IF_INDEX = idx
                return
        except Exception:
            continue
    try:
        CURRENT_DEVICE = "en1"
        CURRENT_IF_INDEX = socket.if_nametoindex("en1")
    except Exception:
        CURRENT_DEVICE = "system"
        CURRENT_IF_INDEX = 0

init_default_interface()

# ==================== 1. DNS 解析器 (支持物理网卡直连与系统默认) ====================
_dns_cache: dict[tuple[str, str], tuple[list[str], float]] = {}
_dns_lock = threading.Lock()
DNS_SERVERS = ["223.5.5.5", "119.29.29.29"]

def _read_dns_name(data: bytes, offset: int) -> tuple[str, int]:
    labels = []
    jumped = False
    orig_offset = offset
    while True:
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if (length & 0xC0) == 0xC0:
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                orig_offset = offset + 2
                jumped = True
            offset = ptr
        else:
            offset += 1
            labels.append(data[offset:offset + length].decode("ascii", errors="ignore"))
            offset += length
    return ".".join(labels), (orig_offset if jumped else offset)

def resolve_host(domain: str, depth: int = 0) -> list[str]:
    domain = domain.strip().lower().rstrip(".")
    try:
        socket.inet_aton(domain)
        return [domain]
    except OSError:
        pass

    if depth > 4 or domain.startswith("ipv6.") or ":" in domain:
        return []

    with _state_lock:
        dev = CURRENT_DEVICE
        if_idx = CURRENT_IF_INDEX

    now = time.time()
    cache_key = (dev, domain)
    with _dns_lock:
        cached = _dns_cache.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

    # 若选择系统默认路由或 utun 隧道，使用系统 getaddrinfo
    if dev == "system" or dev.startswith("utun") or if_idx == 0:
        try:
            infos = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
            ips = list(dict.fromkeys(item[4][0] for item in infos if item[4]))
            if ips:
                with _dns_lock:
                    _dns_cache[cache_key] = (ips, now + 120)
            return ips
        except Exception:
            return []

    # 物理网卡 (en1 / en0)：强制绑定网卡向国内公共 DNS 发送 UDP 查询，绕过 Fake-IP
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in domain.split(".")) + b"\x00"
    tid = struct.pack("!H", int(now * 1000) & 0xFFFF)
    query = tid + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x01\x00\x01"

    ips: list[str] = []
    cnames: list[str] = []

    for dns_srv in DNS_SERVERS:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, struct.pack("I", if_idx))
            s.settimeout(1.5)
            s.sendto(query, (dns_srv, 53))
            data, _ = s.recvfrom(1024)
            s.close()

            ancount = struct.unpack("!H", data[6:8])[0]
            offset = len(query)
            for _ in range(ancount):
                _, offset = _read_dns_name(data, offset)
                rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", data[offset:offset + 10])
                offset += 10
                if rtype == 1 and rdlen == 4:
                    ip_str = socket.inet_ntoa(data[offset:offset + 4])
                    if not ip_str.startswith("198.18."):
                        ips.append(ip_str)
                elif rtype == 5:
                    cname_target, _ = _read_dns_name(data, offset)
                    if cname_target:
                        cnames.append(cname_target)
                offset += rdlen
            if ips:
                break
        except Exception:
            if s:
                try: s.close()
                except Exception: pass

    if not ips and cnames:
        ips = resolve_host(cnames[-1], depth + 1)

    if ips:
        with _dns_lock:
            _dns_cache[cache_key] = (ips, now + 300)
    return ips


# ==================== 2. TLS Keep-Alive 连接池 ====================
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_pool_lock = threading.Lock()
_conn_pool: dict[tuple[str, str, int], queue.LifoQueue] = {}

def clear_all_pools():
    with _dns_lock:
        _dns_cache.clear()
    with _pool_lock:
        for q in _conn_pool.values():
            while not q.empty():
                try:
                    sock, _ = q.get_nowait()
                    sock.close()
                except Exception:
                    pass
        _conn_pool.clear()

def select_interface(device: str) -> dict:
    global CURRENT_DEVICE, CURRENT_IF_INDEX
    device = device.strip()
    with _state_lock:
        if device == "system" or device.startswith("utun"):
            CURRENT_DEVICE = device
            CURRENT_IF_INDEX = 0
        else:
            idx = socket.if_nametoindex(device)
            CURRENT_DEVICE = device
            CURRENT_IF_INDEX = idx
    clear_all_pools()
    sys.stdout.write(f"🔀 [切换出网网口] 已切换至: {CURRENT_DEVICE} (index={CURRENT_IF_INDEX})\n")
    sys.stdout.flush()
    return {"ok": True, "current": CURRENT_DEVICE, "index": CURRENT_IF_INDEX}

def _get_pool(key: tuple[str, str, int]) -> queue.LifoQueue:
    with _pool_lock:
        if key not in _conn_pool:
            _conn_pool[key] = queue.LifoQueue(maxsize=8)
        return _conn_pool[key]

def _create_tls_socket(host: str, port: int, timeout: float) -> ssl.SSLSocket:
    with _state_lock:
        dev = CURRENT_DEVICE
        if_idx = CURRENT_IF_INDEX

    ips = resolve_host(host)
    if not ips:
        raise OSError(f"DNS resolution failed on {dev} for {host}")
    target_ip = ips[0]
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if if_idx > 0 and not dev.startswith("utun") and dev != "system":
        raw_sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, struct.pack("I", if_idx))
    raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    raw_sock.settimeout(timeout)
    tls_sock = _ssl_ctx.wrap_socket(raw_sock, server_hostname=host)
    tls_sock.connect((target_ip, port))
    return tls_sock

def _read_http_response(sock: ssl.SSLSocket) -> tuple[int, dict[str, str], bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("EOF before headers")
        buf += chunk

    header_bytes, rest = buf.split(b"\r\n\r\n", 1)
    header_lines = header_bytes.decode("iso-8859-1", errors="ignore").split("\r\n")
    status_parts = header_lines[0].split(" ", 2)
    status_code = int(status_parts[1]) if len(status_parts) >= 2 and status_parts[1].isdigit() else 502

    headers: dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    body = b""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        data = rest
        while True:
            while b"\r\n" not in data:
                more = sock.recv(4096)
                if not more:
                    break
                data += more
            line, data = data.split(b"\r\n", 1)
            chunk_size = int(line.split(b";")[0].strip(), 16)
            if chunk_size == 0:
                break
            while len(data) < chunk_size + 2:
                more = sock.recv(4096)
                if not more:
                    break
                data += more
            body += data[:chunk_size]
            data = data[chunk_size + 2:]
    elif "content-length" in headers:
        cl = int(headers["content-length"])
        data = rest
        while len(data) < cl:
            more = sock.recv(4096)
            if not more:
                break
            data += more
        body = data[:cl]
    else:
        data = rest
        while True:
            try:
                more = sock.recv(4096)
                if not more:
                    break
                data += more
            except Exception:
                break
        body = data

    return status_code, headers, body

def fetch_via_selected_interface(target_url: str, timeout_sec: float) -> tuple[int, str, bytes, float]:
    parsed = urllib.parse.urlsplit(target_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    if not host or host.startswith("ipv6.") or ":" in host:
        raise OSError("IPv6 not enabled")

    with _state_lock:
        dev = CURRENT_DEVICE

    pool = _get_pool((dev, host, port))
    req_bytes = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}{'' if port == 443 else f':{port}'}\r\n"
        f"User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36\r\n"
        f"Origin: http://127.0.0.1:{PORT}\r\n"
        f"Referer: http://127.0.0.1:{PORT}/\r\n"
        f"Accept: */*\r\n"
        f"Connection: keep-alive\r\n\r\n"
    ).encode("utf-8")

    while not pool.empty():
        try:
            sock, exp = pool.get_nowait()
        except queue.Empty:
            break
        if time.time() > exp:
            try: sock.close()
            except Exception: pass
            continue
        try:
            sock.settimeout(timeout_sec)
            t0 = time.perf_counter()
            sock.sendall(req_bytes)
            status, hdrs, body = _read_http_response(sock)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if hdrs.get("connection", "").lower() != "close":
                try:
                    pool.put_nowait((sock, time.time() + 15.0))
                except queue.Full:
                    sock.close()
            else:
                sock.close()
            return status, hdrs.get("content-type", "text/plain; charset=utf-8"), body, elapsed_ms
        except Exception:
            try: sock.close()
            except Exception: pass

    sock = _create_tls_socket(host, port, timeout_sec)
    try:
        t0 = time.perf_counter()
        sock.sendall(req_bytes)
        status, hdrs, body = _read_http_response(sock)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if hdrs.get("connection", "").lower() != "close":
            try:
                pool.put_nowait((sock, time.time() + 15.0))
            except queue.Full:
                sock.close()
        else:
            sock.close()
        return status, hdrs.get("content-type", "text/plain; charset=utf-8"), body, elapsed_ms
    except Exception:
        try: sock.close()
        except Exception: pass
        raise


# ==================== 3. HTTP 请求处理器 ====================
class BestCFHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/__direct_proxy" not in msg or "ip.json" in msg:
            sys.stdout.write(f"[{self.log_date_time_string()}] {msg}\n")
            sys.stdout.flush()

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/__interfaces"):
            with _state_lock:
                cur = CURRENT_DEVICE
            self._send_json(200, {
                "current": cur,
                "interfaces": list_macos_interfaces()
            })
            return

        if self.path.startswith("/__select_interface"):
            parsed = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            device = (qs.get("device") or ["en1"])[0]
            try:
                res = select_interface(device)
                self._send_json(200, res)
            except Exception as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            return

        if self.path.startswith("/__direct_proxy"):
            parsed = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            target_url = (qs.get("url") or [""])[0]
            timeout_ms = float((qs.get("timeout") or ["5000"])[0])
            timeout_sec = max(0.3, min(timeout_ms / 1000.0, 15.0))

            try:
                status, ctype, body, rtt_ms = fetch_via_selected_interface(target_url, timeout_sec)
                if "ip.json" in target_url:
                    sys.stdout.write(f"⚡ [网口 {CURRENT_DEVICE} 直连成功] {target_url} -> HTTP {status} ({rtt_ms:.1f}ms)\n")
                    sys.stdout.flush()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-RTT-Ms", f"{rtt_ms:.2f}")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                if "ip.json" in target_url and "ipv6." not in target_url:
                    sys.stdout.write(f"⚠️ [网口 {CURRENT_DEVICE} 请求异常] {target_url} -> {e}\n")
                    sys.stdout.flush()
                err_body = str(e).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(err_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(err_body)
            return

        return super().do_GET()


class ThreadedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print("==========================================================")
    print(f"⚡ BestCF 本地多网口直连服务 (端口: {PORT})")
    print(f"📡 默认绑定物理网卡: {CURRENT_DEVICE} (index={CURRENT_IF_INDEX})")
    print(f"🌐 浏览器访问地址:   http://127.0.0.1:{PORT}")
    print("==========================================================")
    sys.stdout.flush()
    server = ThreadedServer(("127.0.0.1", PORT), BestCFHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 服务已停止")
