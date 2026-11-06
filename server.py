import json
import os
import platform
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import psutil

HOST_BIND = os.environ.get("DASHBOARD_BIND", "0.0.0.0")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
SNAPSHOT_INTERVAL = 0.5   # fast sections: cpu / memory / network / disk
SENSORS_INTERVAL = 5.0    # temperature & battery reads are comparatively slow
PROCS_INTERVAL = 2.0      # full process enumeration is expensive
IFACE_INFO_INTERVAL = 5.0  # interface IPs / link state rarely change

HOST = socket.gethostname()
PLATFORM = f"{platform.system()} {platform.release()}"
CPU_COUNT = psutil.cpu_count(logical=True) or 1

try:
    _net_prev = psutil.net_io_counters()
    _nic_prev = psutil.net_io_counters(pernic=True) or {}
    _disk_prev = psutil.disk_io_counters()
except Exception:
    _net_prev = _nic_prev = _disk_prev = None
_net_prev_t = _nic_prev_t = _disk_prev_t = time.time()
_addrs_cache = ({}, {})
_addrs_time = 0.0

_lock = threading.Lock()
_cache = {}
_cache_time = 0.0
_sensors_cache = {"temps": {}, "battery": None}
_sensors_time = 0.0
_procs_cache = {"count": 0, "top_cpu": [], "top_mem": []}
_procs_time = 0.0


def _safe(fn, default):
    """Run a collector; never let one failing section break the whole API."""
    try:
        return fn()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"dashboard: {fn.__name__} failed: {exc}", file=sys.stderr)
        return default


def _cpu():
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    return {
        "total": round(sum(per_core) / len(per_core), 1) if per_core else 0.0,
        "cores": [round(c, 1) for c in per_core],
        "count": len(per_core),
    }


def _memory():
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "percent": round(vm.percent, 1),
        "used": vm.used,
        "total": vm.total,
        "available": vm.available,
        "swap_percent": round(sw.percent, 1),
        "swap_used": sw.used,
        "swap_total": sw.total,
    }


def _delta(cur, prev, attr, dt):
    """Rate from monotonically increasing counters, guarded against resets."""
    if not cur or not prev:
        return 0.0
    c, p = getattr(cur, attr, 0), getattr(prev, attr, 0)
    if c < p:  # counter wrapped / device reset
        return 0.0
    return (c - p) / dt


def _refresh_iface_info(now):
    global _addrs_cache, _addrs_time
    if now - _addrs_time >= IFACE_INFO_INTERVAL:
        _addrs_cache = (psutil.net_if_addrs(), psutil.net_if_stats())
        _addrs_time = now


def _network():
    global _net_prev, _net_prev_t, _nic_prev, _nic_prev_t
    now = time.time()
    cur = psutil.net_io_counters()
    dt = max(now - _net_prev_t, 0.001)
    sent = _delta(cur, _net_prev, "bytes_sent", dt)
    recv = _delta(cur, _net_prev, "bytes_recv", dt)
    _net_prev, _net_prev_t = cur, now

    cur_nic = psutil.net_io_counters(pernic=True) or {}
    nic_dt = max(now - _nic_prev_t, 0.001)
    _refresh_iface_info(now)
    addrs, stats = _addrs_cache
    interfaces = {}
    for name in sorted(set(cur_nic) | set(addrs)):
        nic = cur_nic.get(name)
        prev = _nic_prev.get(name)
        interfaces[name] = {
            "ip": next((a.address for a in addrs.get(name, ()) if a.family == socket.AF_INET), None),
            "up": bool(stats.get(name) and stats[name].isup),
            "recv_rate": round(_delta(nic, prev, "bytes_recv", nic_dt), 1),
            "sent_rate": round(_delta(nic, prev, "bytes_sent", nic_dt), 1),
            "bytes_recv": nic.bytes_recv if nic else 0,
            "bytes_sent": nic.bytes_sent if nic else 0,
        }
    _nic_prev, _nic_prev_t = cur_nic, now

    return {
        "sent_rate": sent,
        "recv_rate": recv,
        "bytes_sent": cur.bytes_sent if cur else 0,
        "bytes_recv": cur.bytes_recv if cur else 0,
        "packets_sent": cur.packets_sent if cur else 0,
        "packets_recv": cur.packets_recv if cur else 0,
        "interfaces": interfaces,
    }


def _disk():
    global _disk_prev, _disk_prev_t
    now = time.time()
    cur = psutil.disk_io_counters()
    dt = max(now - _disk_prev_t, 0.001)
    read = _delta(cur, _disk_prev, "read_bytes", dt)
    write = _delta(cur, _disk_prev, "write_bytes", dt)
    _disk_prev, _disk_prev_t = cur, now
    partitions = []
    for p in psutil.disk_partitions(all=False):
        try:
            du = psutil.disk_usage(p.mountpoint)
            partitions.append({
                "device": p.device,
                "mountpoint": p.mountpoint,
                "fstype": p.fstype,
                "percent": round(du.percent, 1),
                "used": du.used,
                "total": du.total,
            })
        except (PermissionError, OSError):
            continue
    return {
        "read_rate": read,
        "write_rate": write,
        "read_count": cur.read_count if cur else 0,
        "write_count": cur.write_count if cur else 0,
        "partitions": partitions,
    }


def _sensors():
    sensors = {"temps": {}, "battery": None}
    if hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures()
            for name, entries in temps.items():
                sensors["temps"][name] = [
                    {
                        "label": e.label or name,
                        "current": e.current,
                        "high": getattr(e, "high", None),
                        "critical": getattr(e, "critical", None),
                    }
                    for e in entries
                ]
        except Exception:
            pass
    if hasattr(psutil, "sensors_battery"):
        try:
            batt = psutil.sensors_battery()
            if batt:
                sensors["battery"] = {
                    "percent": batt.percent,
                    "power_plugged": batt.power_plugged,
                    "secsleft": batt.secsleft,
                }
        except Exception:
            pass
    return sensors


def _processes():
    count = 0
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "cpu_percent",
                                  "memory_percent", "memory_info"]):
        count += 1
        info = p.info
        mi = info.get("memory_info")
        procs.append({
            "pid": info.get("pid"),
            "name": info.get("name") or "?",
            "user": info.get("username"),
            # normalize per-process cpu% (single-core relative) to system-wide %
            "cpu": round((info.get("cpu_percent") or 0.0) / CPU_COUNT, 1),
            "mem": round(info.get("memory_percent") or 0.0, 1),
            "rss": mi.rss if mi else 0,
        })
    top_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:5]
    top_mem = sorted(procs, key=lambda x: x["mem"], reverse=True)[:5]
    return {"count": count, "top_cpu": top_cpu, "top_mem": top_mem}


def snapshot():
    global _cache, _cache_time, _sensors_cache, _sensors_time, _procs_cache, _procs_time
    with _lock:
        now = time.time()
        if now - _cache_time >= SNAPSHOT_INTERVAL:
            if now - _sensors_time >= SENSORS_INTERVAL:
                _sensors_cache = _safe(_sensors, _sensors_cache)
                _sensors_time = now
            if now - _procs_time >= PROCS_INTERVAL:
                _procs_cache = _safe(_processes, _procs_cache)
                _procs_time = now
            _cache = {
                "timestamp": now,
                "host": HOST,
                "platform": PLATFORM,
                "cpu": _safe(_cpu, {"total": 0.0, "cores": [], "count": 0}),
                "memory": _safe(_memory, {}),
                "network": _safe(_network, {}),
                "disk": _safe(_disk, {}),
                "sensors": _sensors_cache,
                "processes": _procs_cache,
                "boot_time": psutil.boot_time(),
                "uptime": now - psutil.boot_time(),
                "load_avg": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
            }
            _cache_time = now
        return _cache


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: fewer connection churns at 1 Hz polling

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/api/stats":
            self._serve_json(snapshot())
        elif path == "/api/static" and self.headers.get("X-Poll") == "1":
            self._serve_json(snapshot())
        else:
            self.send_error(404)

    def _serve_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, name, ctype):
        try:
            with open(os.path.join(os.path.dirname(__file__), name), "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer((HOST_BIND, PORT), Handler)
    print(f"System dashboard running at http://localhost:{PORT} ({HOST}, {PLATFORM})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
