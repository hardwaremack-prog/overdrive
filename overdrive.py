#!/usr/bin/env python3
"""
Overdrive - a hardware showcase for the Raspberry Pi 500.

Runs a small local web server (Python standard library only, nothing to pip
install) and opens a Chromium app window that drives every part of the Pi:
all four Cortex-A76 cores, the VideoCore VII GPU, RAM, storage, Wi-Fi,
Bluetooth, Ethernet, the 40-pin GPIO header, the LEDs and audio.

    python3 overdrive.py            # start + open the app window
    python3 overdrive.py --lan      # also reachable from other devices
    python3 overdrive.py --no-browser
"""
import argparse
import glob
import json
import multiprocessing as mp
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8500


# ----------------------------------------------------------------- helpers
def sh(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def readf(path, default=""):
    try:
        with open(path, errors="replace") as f:
            return f.read().replace("\x00", "").strip()
    except Exception:
        return default


_HAS = {}


def has(cmd):
    if cmd not in _HAS:
        _HAS[cmd] = shutil.which(cmd) is not None
    return _HAS[cmd]


# --------------------------------------- CPU benchmark worker (picklable)
def cpu_worker(seconds):
    """Mandelbrot points per second on one core."""
    end = time.perf_counter() + seconds
    pts = 0
    while time.perf_counter() < end:
        for i in range(1000):
            c = complex((i % 40) / 16 - 1.8, (i // 40) / 20 - 0.6)
            z = 0j
            for _ in range(64):
                z = z * z + c
                if z.real * z.real + z.imag * z.imag > 4.0:
                    break
        pts += 1000
    return pts / seconds


# ------------------------------------------------------- firmware probes
def vc(*args):
    return sh(["vcgencmd", *args], 2) if has("vcgencmd") else ""


def vc_clock(name):
    m = re.search(r"=(\d+)", vc("measure_clock", name))
    return round(int(m.group(1)) / 1e6) if m else None


def vc_volts(name="core"):
    m = re.search(r"=([\d.]+)V", vc("measure_volts", name))
    return float(m.group(1)) if m else None


def pmic_power():
    """Pi 5-family boards report rail currents/voltages from the PMIC."""
    out = vc("pmic_read_adc")
    cur, vol = {}, {}
    for m in re.finditer(r"(\S+)_([AV])\s+\w+\(\d+\)=([\d.]+)", out):
        (cur if m.group(2) == "A" else vol)[m.group(1)] = float(m.group(3))
    if not cur:
        return None, None
    total = sum(cur[k] * vol[k] for k in cur if k in vol)
    core = cur.get("VDD_CORE", 0) * vol.get("VDD_CORE", 0)
    return round(total, 2), round(core, 2)


THROTTLE_BITS = {0: "Under-voltage", 1: "ARM frequency capped", 2: "Throttled", 3: "Soft temp limit"}


def throttled():
    m = re.search(r"0x([0-9a-fA-F]+)", vc("get_throttled"))
    if not m:
        return None
    v = int(m.group(1), 16)
    return {
        "raw": hex(v),
        "now": [t for b, t in THROTTLE_BITS.items() if v >> b & 1],
        "past": [t for b, t in THROTTLE_BITS.items() if v >> (b + 16) & 1],
    }


def soc_temp():
    t = readf("/sys/class/thermal/thermal_zone0/temp")
    if t.lstrip("-").isdigit():
        return round(int(t) / 1000, 1)
    m = re.search(r"=([\d.]+)", vc("measure_temp"))
    return float(m.group(1)) if m else None


DISK_RE = re.compile(r"^(mmcblk\d+|nvme\d+n\d+|sd[a-z]+)$")


# ------------------------------------------------------------ live sampler
class Sampler(threading.Thread):
    HIST = 180

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.live = {}
        self.hist = {"temp": [], "cpu": [], "watts": [], "arm": []}
        self.prev = None
        self.reset_peaks()

    def reset_peaks(self):
        self.peaks = {"temp": 0, "watts": 0, "arm": 0, "v3d": 0, "cpu": 0, "thr": []}

    @staticmethod
    def _cpu():
        rows = []
        for line in readf("/proc/stat").splitlines():
            if line.startswith("cpu"):
                v = list(map(int, line.split()[1:8]))
                rows.append((sum(v), v[3] + v[4]))
        return rows

    @staticmethod
    def _net():
        rx = tx = 0
        for line in readf("/proc/net/dev").splitlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() == "lo":
                continue
            f = data.split()
            rx += int(f[0])
            tx += int(f[8])
        return rx, tx

    @staticmethod
    def _disk():
        r = w = 0
        for line in readf("/proc/diskstats").splitlines():
            f = line.split()
            if len(f) > 9 and DISK_RE.match(f[2]):
                r += int(f[5]) * 512
                w += int(f[9]) * 512
        return r, w

    def sample(self):
        now = time.time()
        cpu, net, disk = self._cpu(), self._net(), self._disk()
        out = {"t": now}
        if self.prev:
            pt, pcpu, pnet, pdisk = self.prev
            dt = max(now - pt, 1e-3)
            pct = []
            for (tot, idle), (ptot, pidle) in zip(cpu, pcpu):
                d = tot - ptot
                pct.append(round(100 * (1 - (idle - pidle) / d), 1) if d > 0 else 0.0)
            out["cpu"] = pct
            out["net"] = {"rx": (net[0] - pnet[0]) / dt, "tx": (net[1] - pnet[1]) / dt}
            out["disk"] = {"r": (disk[0] - pdisk[0]) / dt, "w": (disk[1] - pdisk[1]) / dt}
        self.prev = (now, cpu, net, disk)

        freqs = []
        for p in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")):
            v = readf(p)
            if v.isdigit():
                freqs.append(round(int(v) / 1000))
        out["freqs"] = freqs
        out["temp"] = soc_temp()
        out["arm"] = vc_clock("arm") or (max(freqs) if freqs else None)
        out["core"] = vc_clock("core")
        out["v3d"] = vc_clock("v3d")
        out["volts"] = vc_volts("core")
        out["watts"], out["core_watts"] = pmic_power()
        out["throttled"] = throttled()
        fan = glob.glob("/sys/devices/platform/cooling_fan/hwmon/*/fan1_input")
        out["fan"] = int(readf(fan[0], "0") or 0) if fan else None

        mi = {}
        for line in readf("/proc/meminfo").splitlines():
            k, v = line.split(":", 1)
            mi[k] = int(v.split()[0]) * 1024
        total, avail = mi.get("MemTotal", 1), mi.get("MemAvailable", 0)
        out["mem"] = {"total": total, "used": total - avail, "pct": round(100 * (total - avail) / total, 1)}
        out["load"] = os.getloadavg()
        out["uptime"] = float(readf("/proc/uptime", "0").split()[0])

        with self.lock:
            p = self.peaks
            for key in ("temp", "watts", "arm", "v3d"):
                if out.get(key):
                    p[key] = max(p[key], out[key])
            if out.get("cpu"):
                p["cpu"] = max(p["cpu"], out["cpu"][0])
            if out.get("throttled"):
                for t in out["throttled"]["now"]:
                    if t not in p["thr"]:
                        p["thr"].append(t)
            out["peaks"] = {**p, "thr": list(p["thr"])}
            for key, val in (("temp", out["temp"]), ("cpu", out.get("cpu", [0])[0]),
                             ("watts", out["watts"]), ("arm", out["arm"])):
                h = self.hist[key]
                h.append(val)
                del h[:-self.HIST]
            self.live = out

    def run(self):
        while True:
            try:
                self.sample()
            except Exception as e:  # never let the sampler die
                print("sampler:", e, file=sys.stderr)
            time.sleep(1)

    def snapshot(self, with_hist=False):
        with self.lock:
            d = dict(self.live)
            if with_hist:
                d["hist"] = {k: list(v) for k, v in self.hist.items()}
            return d


SAMPLER = Sampler()


# ------------------------------------------------------------- system info
def system_info():
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', readf("/etc/os-release"), re.M))
    cpuinfo = readf("/proc/cpuinfo")
    rev = re.search(r"Revision\s*:\s*(\w+)", cpuinfo)
    maxf = readf("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    lscpu = sh(["lscpu"])
    cpu_model = re.search(r"Model name:\s*(.+)", lscpu)

    storage = []
    try:
        blk = json.loads(sh(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,MODEL,TRAN,FSTYPE"]) or "{}")
        for d in blk.get("blockdevices", []):
            if d.get("type") != "disk" or d["name"].startswith(("zram", "loop", "ram")):
                continue
            parts = [{"name": c["name"], "size": c.get("size"), "mount": c.get("mountpoint"),
                      "fs": c.get("fstype")} for c in d.get("children", []) or []]
            storage.append({"name": d["name"], "size": d.get("size"), "model": (d.get("model") or "").strip(),
                            "tran": d.get("tran"), "parts": parts})
    except Exception:
        pass

    usb = []
    for line in sh(["lsusb"]).splitlines():
        m = re.match(r"Bus (\d+) Device (\d+): ID (\S+)\s*(.*)", line)
        if m and "root hub" not in m.group(4):
            usb.append({"bus": m.group(1), "id": m.group(3), "name": m.group(4) or "Unknown device"})

    displays = []
    for st in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        d = os.path.dirname(st)
        conn = os.path.basename(d).split("-", 1)[1]
        status = readf(st)
        modes = readf(os.path.join(d, "modes")).splitlines()
        if conn.startswith(("HDMI", "DSI", "Composite", "Writeback")) or status == "connected":
            displays.append({"port": conn, "status": status, "mode": modes[0] if modes else ""})

    nets = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        if name == "lo":
            continue
        nets.append({"name": name, "state": readf(path + "/operstate"), "mac": readf(path + "/address"),
                     "speed": readf(path + "/speed"), "wireless": os.path.isdir(path + "/wireless")})

    return {
        "version": VERSION,
        "model": readf("/proc/device-tree/model") or platform.node(),
        "hostname": socket.gethostname(),
        "os": osr.get("PRETTY_NAME", platform.system()),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpu_model": cpu_model.group(1).strip() if cpu_model else platform.processor(),
        "cores": os.cpu_count(),
        "max_mhz": round(int(maxf) / 1000) if maxf.isdigit() else None,
        "revision": rev.group(1) if rev else None,
        "firmware": (vc("version").splitlines() or [""])[0],
        "ip": sh(["hostname", "-I"]).split(),
        "storage": storage,
        "usb": usb,
        "displays": displays,
        "nets": nets,
        "bluetooth": bool(glob.glob("/sys/class/bluetooth/hci*")),
        "tools": {c: has(c) for c in ("vcgencmd", "pinctrl", "nmcli", "bluetoothctl", "lsusb")},
    }


# --------------------------------------------------------------------- jobs
JOBS = {}
JOB_LOCK = threading.Lock()


def start_job(name, fn, **kw):
    with JOB_LOCK:
        j = JOBS.get(name)
        if j and j["state"] == "running":
            return False
        job = {"state": "running", "progress": 0, "msg": "Starting...", "result": None, "started": time.time()}
        JOBS[name] = job

    def run():
        try:
            res = fn(job, **kw)
            job.update(state="done", progress=100, result=res, msg="Done")
        except Exception as e:
            job.update(state="error", msg=str(e))

    threading.Thread(target=run, daemon=True).start()
    return True


_POOL = None


def pool():
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(max_workers=os.cpu_count(), mp_context=mp.get_context("spawn"))
    return _POOL


def _wait(job, futures, seconds, p0, p1, msg):
    t0 = time.time()
    while not all(f.done() for f in futures):
        job["progress"] = int(p0 + (p1 - p0) * min(1, (time.time() - t0) / seconds))
        job["msg"] = msg
        time.sleep(0.25)
    return [f.result() for f in futures]


def bench_cpu(job, seconds="6", mode="full"):
    secs = max(2, min(60, float(seconds)))
    n = os.cpu_count() or 4
    job["msg"] = "Waking up worker processes..."
    ex = pool()
    list(ex.map(cpu_worker, [0.05] * n))  # warm-up so spawn time is not measured
    res = {"cores": n}
    if mode != "stress":
        res["single"] = _wait(job, [ex.submit(cpu_worker, secs)], secs, 5, 45, "Single core sprint...")[0]
    per = _wait(job, [ex.submit(cpu_worker, secs) for _ in range(n)], secs,
                45 if mode != "stress" else 5, 100, f"All {n} cores flat out...")
    res["per_core"] = per
    res["multi"] = sum(per)
    if res.get("single"):
        res["scaling"] = res["multi"] / res["single"]
    return res


def bench_mem(job):
    mb = 256
    size = mb * 1024 * 1024
    job["msg"] = f"Allocating {mb} MB x2..."
    a = bytearray(os.urandom(1024 * 1024)) * mb
    b = bytearray(size)
    job["progress"] = 15
    job["msg"] = "Streaming copy (DRAM)..."
    reps = 8
    t = time.perf_counter()
    for i in range(reps):
        b[:] = a
        job["progress"] = 15 + int(45 * (i + 1) / reps)
    big = size * reps / (time.perf_counter() - t) / 1e9

    job["msg"] = "Cache-sized copy (L2/L3)..."
    blk = 256 * 1024
    sa, sb = memoryview(a)[:blk], memoryview(b)[:blk]
    reps2 = 4000
    t = time.perf_counter()
    for _ in range(reps2):
        sb[:] = sa
    small = blk * reps2 / (time.perf_counter() - t) / 1e9
    job["progress"] = 90
    del a, b
    return {"dram_gbs": round(big, 2), "cache_gbs": round(small, 2), "block_mb": mb}


def bench_disk(job, mb="512"):
    mb = max(64, min(2048, int(mb)))
    path = os.path.join(os.path.expanduser("~"), ".overdrive_disktest")
    free = shutil.disk_usage(os.path.dirname(path)).free
    if free < mb * 1024 * 1024 * 2:
        raise RuntimeError("Not enough free space for the storage test")
    chunk = os.urandom(4 * 1024 * 1024)
    n = mb // 4
    try:
        job["msg"] = f"Writing {mb} MB..."
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        t = time.perf_counter()
        for i in range(n):
            os.write(fd, chunk)
            job["progress"] = int(45 * (i + 1) / n)
        os.fsync(fd)
        os.close(fd)
        write = mb / (time.perf_counter() - t)

        job["msg"] = f"Reading {mb} MB back (cache dropped)..."
        fd = os.open(path, os.O_RDONLY)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        t = time.perf_counter()
        i = 0
        while os.read(fd, 4 * 1024 * 1024):
            i += 1
            job["progress"] = 45 + int(35 * i / n)
        read = mb / (time.perf_counter() - t)

        job["msg"] = "Random 4K reads..."
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        import random
        blocks = mb * 256
        count = 0
        t = time.perf_counter()
        while time.perf_counter() - t < 3:
            os.pread(fd, 4096, random.randrange(blocks) * 4096)
            count += 1
        iops = count / (time.perf_counter() - t)
        os.close(fd)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return {"write_mbs": round(write, 1), "read_mbs": round(read, 1), "iops": round(iops), "mb": mb}


def wifi_scan(job):
    if not has("nmcli"):
        raise RuntimeError("nmcli not found - Wi-Fi scan needs NetworkManager")
    job["msg"] = "Sweeping 2.4 GHz and 5 GHz..."
    out = sh(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,CHAN,FREQ,SECURITY,BSSID",
              "device", "wifi", "list", "--rescan", "yes"], 30)
    nets = []
    for line in out.splitlines():
        f = [x.replace("\\:", ":") for x in re.split(r"(?<!\\):", line)]
        if len(f) < 7:
            continue
        nets.append({"active": f[0] == "*", "ssid": f[1] or "(hidden)", "signal": int(f[2] or 0),
                     "chan": f[3], "freq": f[4], "security": f[5] or "open", "bssid": f[6]})
    nets.sort(key=lambda n: -n["signal"])
    return {"networks": nets}


def bt_scan(job, seconds="12"):
    if not has("bluetoothctl"):
        raise RuntimeError("bluetoothctl not found")
    secs = max(4, min(30, int(seconds)))
    sh(["bluetoothctl", "power", "on"], 5)
    job["msg"] = f"Listening for Bluetooth / BLE for {secs}s..."
    holder = {}
    th = threading.Thread(target=lambda: holder.update(out=sh(
        ["bluetoothctl", "--timeout", str(secs), "scan", "on"], secs + 8)))
    th.start()
    t0 = time.time()
    while th.is_alive():
        job["progress"] = min(95, int(100 * (time.time() - t0) / secs))
        time.sleep(0.3)
    out = holder.get("out", "") + "\n" + sh(["bluetoothctl", "devices"], 5)
    devs = {}
    for line in out.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m|\x01|\x02", "", line)
        m = re.search(r"Device ([0-9A-F:]{17})\s*(.*)", line)
        if not m:
            continue
        mac, rest = m.group(1), m.group(2).strip()
        d = devs.setdefault(mac, {"mac": mac, "name": "", "rssi": None})
        if rest.startswith(("Name:", "Alias:")):
            d["name"] = rest.split(":", 1)[1].strip()
        elif rest.startswith("RSSI:"):
            nums = re.findall(r"-\d+", rest)
            if nums:
                d["rssi"] = int(nums[-1])
        elif rest and ":" not in rest[:15] and not d["name"]:
            d["name"] = rest
    for d in devs.values():
        if not d["name"] or re.fullmatch(r"[0-9A-F]{2}(-[0-9A-F]{2}){5}", d["name"]):
            d["name"] = "(unnamed)"
    lst = sorted(devs.values(), key=lambda d: -(d["rssi"] if d["rssi"] is not None else -999))
    return {"devices": lst}


def _ping(host, n=10):
    out = sh(["ping", "-c", str(n), "-i", "0.2", "-q", "-W", "2", host], n + 6)
    m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)", out)
    loss = re.search(r"([\d.]+)% packet loss", out)
    return {"host": host, "min": float(m.group(1)), "avg": float(m.group(2)), "max": float(m.group(3)),
            "loss": float(loss.group(1)) if loss else None} if m else {"host": host, "error": "no reply"}


def net_test(job):
    res = {}
    gw = re.search(r"default via (\S+)", sh(["ip", "route"]))
    if gw and has("ping"):
        job["msg"] = "Pinging your router..."
        res["gateway"] = _ping(gw.group(1))
    job["progress"] = 20
    job["msg"] = "Pinging the internet..."
    res["internet"] = _ping("1.1.1.1") if has("ping") else None
    job["progress"] = 35
    job["msg"] = "Download speed test..."
    try:
        req = urllib.request.Request("https://speed.cloudflare.com/__down?bytes=100000000",
                                     headers={"User-Agent": "Overdrive/1.0"})
        got = 0
        t = time.perf_counter()
        with urllib.request.urlopen(req, timeout=10) as r:
            while True:
                buf = r.read(256 * 1024)
                if not buf:
                    break
                got += len(buf)
                el = time.perf_counter() - t
                job["progress"] = 35 + int(65 * min(1, max(got / 1e8, el / 12)))
                if el > 12:
                    break
        res["download_mbps"] = round(got * 8 / (time.perf_counter() - t) / 1e6, 1)
    except Exception as e:
        res["download_error"] = str(e)
    eth = readf("/sys/class/net/eth0/speed")
    res["eth_link"] = int(eth) if eth.lstrip("-").isdigit() and int(eth) > 0 else None
    return res


# --------------------------------------------------------------------- LEDs
def leds_list():
    out = []
    for d in sorted(glob.glob("/sys/class/leds/*")):
        trig = re.search(r"\[(\S+)\]", readf(d + "/trigger"))
        out.append({"name": os.path.basename(d), "on": readf(d + "/brightness", "0") not in ("0", ""),
                    "max": int(readf(d + "/max_brightness", "1") or 1), "trigger": trig.group(1) if trig else "",
                    "writable": os.access(d + "/brightness", os.W_OK)})
    return out


def _w(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val))
        return True
    except Exception:
        return False


def led_set(name, on):
    d = "/sys/class/leds/" + os.path.basename(name)
    _w(d + "/trigger", "none")
    return _w(d + "/brightness", readf(d + "/max_brightness", "1") if on else 0)


def led_show(job, seconds="10"):
    secs = max(3, min(60, float(seconds)))
    leds = [l for l in leds_list() if l["writable"]]
    if not leds:
        raise RuntimeError("No writable LEDs - run install.sh and accept the LED permission step")
    saved = [(l["name"], l["trigger"], l["on"], l["max"]) for l in leds]
    names = [l["name"] for l in leds]
    for n in names:
        _w(f"/sys/class/leds/{n}/trigger", "none")
    t0 = time.time()
    step = 0
    try:
        while time.time() - t0 < secs:
            phase = int((time.time() - t0) / 3) % 3
            for i, n in enumerate(names):
                if phase == 0:
                    on = i == step % len(names)  # chase
                elif phase == 1:
                    on = step % 2 == 0  # strobe
                else:
                    on = (step * 7 + i * 3) % 5 < 2  # sparkle
                _w(f"/sys/class/leds/{n}/brightness", 1 if on else 0)
            step += 1
            job["progress"] = int(100 * (time.time() - t0) / secs)
            job["msg"] = ["Chase", "Strobe", "Sparkle"][phase]
            time.sleep(0.12)
    finally:
        for n, trig, on, mx in saved:
            _w(f"/sys/class/leds/{n}/brightness", mx if on else 0)
            if trig and trig != "none":
                _w(f"/sys/class/leds/{n}/trigger", trig)
    return {"leds": names}


# --------------------------------------------------------------------- GPIO
PIN_RE = re.compile(r"^\s*(\d+):\s+(\S+)\s+(\S*)\s*\|\s*(hi|lo)\s*//\s*(.*)$")


def gpio_read():
    if not has("pinctrl"):
        return {"available": False, "pins": {}}
    pins = {}
    for line in sh(["pinctrl", "get", "0-27"], 3).splitlines():
        m = PIN_RE.match(line)
        if m:
            pins[m.group(1)] = {"mode": m.group(2), "pull": m.group(3), "level": m.group(4) == "hi",
                                "desc": m.group(5).strip()}
    return {"available": True, "pins": pins}


def gpio_set(pin, mode):
    pin = int(pin)
    if not 0 <= pin <= 27:
        raise ValueError("pin out of range")
    args = {"high": ["op", "dh"], "low": ["op", "dl"], "input": ["ip", "pn"],
            "pullup": ["ip", "pu"], "pulldown": ["ip", "pd"]}[mode]
    sh(["pinctrl", "set", str(pin), *args], 3)
    return gpio_read()


# ------------------------------------------------------- saved benchmark runs
DATA_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "overdrive")
RUNS_FILE = os.path.join(DATA_DIR, "runs.json")
RUNS_LOCK = threading.Lock()
RUN_FIELDS = ("name", "cpu1", "cpuN", "mem", "disk_r", "disk_w", "gpu", "gpu_fps", "peak_temp",
              "peak_watts", "peak_arm", "peak_v3d", "thr", "config")


def load_runs():
    try:
        with open(RUNS_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("runs"), list):
            return d
    except Exception:
        pass
    return {"runs": [], "baseline": None}


def save_runs(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RUNS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, RUNS_FILE)


def runs_action(action, body):
    with RUNS_LOCK:
        d = load_runs()
        if action == "add":
            run = {k: body.get(k) for k in RUN_FIELDS}
            run["name"] = str(run.get("name") or "Run")[:60]
            run["id"] = "r%d" % int(time.time() * 1000)
            run["ts"] = time.time()
            run["model"] = readf("/proc/device-tree/model") or platform.node()
            d["runs"].append(run)
            if not d.get("baseline"):
                d["baseline"] = run["id"]
        elif action == "delete":
            d["runs"] = [r for r in d["runs"] if r["id"] != body.get("id")]
            if d.get("baseline") == body.get("id"):
                d["baseline"] = d["runs"][0]["id"] if d["runs"] else None
        elif action == "baseline":
            if any(r["id"] == body.get("id") for r in d["runs"]):
                d["baseline"] = body["id"]
        elif action == "rename":
            for r in d["runs"]:
                if r["id"] == body.get("id"):
                    r["name"] = str(body.get("name") or r["name"])[:60]
        if action != "get":
            save_runs(d)
        return d


# ------------------------------------------------------------- overclocking
CONFIG_TXT = "/boot/firmware/config.txt" if os.path.exists("/boot/firmware/config.txt") else "/boot/config.txt"
OC_BEGIN, OC_END = "# >>> Overdrive OC >>>", "# <<< Overdrive OC <<<"
OC_KEYS = ("arm_freq", "gpu_freq", "core_freq", "v3d_freq", "over_voltage_delta", "over_voltage")
PRESETS = {
    "stock": {"name": "Stock", "desc": "Factory clocks (CPU 2.4 GHz). Removes Overdrive's settings.", "lines": []},
    "mild": {"name": "Mild", "desc": "CPU 2.6 GHz, GPU 1.0 GHz, no extra voltage. Works on almost every Pi 500.",
             "lines": ["arm_freq=2600", "gpu_freq=1000"]},
    "spicy": {"name": "Spicy", "desc": "CPU 2.8 GHz, GPU 1.0 GHz with a small voltage bump. Runs warm.",
              "lines": ["arm_freq=2800", "gpu_freq=1000", "over_voltage_delta=50000"]},
    "max": {"name": "Max", "desc": "CPU 3.0 GHz, GPU 1.1 GHz. Not every chip can do it, and it gets hot.",
            "lines": ["arm_freq=3000", "gpu_freq=1100", "over_voltage_delta=72000"]},
}


def is_pi5_family():
    return bool(re.search(r"Raspberry Pi (5|500|Compute Module 5)", readf("/proc/device-tree/model")))


def oc_state():
    cur = {}
    for line in vc("get_config", "int").splitlines():
        k, _, v = line.partition("=")
        if k in OC_KEYS:
            cur[k] = v.strip()
    block = []
    txt = readf(CONFIG_TXT)
    m = re.search(re.escape(OC_BEGIN) + r"(.*?)" + re.escape(OC_END), txt, re.S)
    if m:
        block = [l for l in m.group(1).strip().splitlines() if l and not l.startswith("[")]
    active = next((k for k, p in PRESETS.items() if p["lines"] == block), "custom")
    maxf = readf("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    return {"current": cur, "block": block, "active": active, "config_path": CONFIG_TXT,
            "pi5": is_pi5_family(), "presets": PRESETS, "pkexec": has("pkexec"),
            "max_mhz": round(int(maxf) / 1000) if maxf.isdigit() else None}


def write_oc_block(preset):
    """Runs as root (via pkexec). Replaces Overdrive's block at the end of config.txt."""
    lines = PRESETS[preset]["lines"]
    with open(CONFIG_TXT) as f:
        txt = f.read()
    shutil.copy2(CONFIG_TXT, CONFIG_TXT + ".overdrive-bak")
    txt = re.sub(r"\n*" + re.escape(OC_BEGIN) + r".*?" + re.escape(OC_END) + r"\n?", "\n", txt, flags=re.S)
    txt = txt.rstrip("\n") + "\n"
    if lines:
        txt += "\n" + OC_BEGIN + "\n[all]\n" + "\n".join(lines) + "\n" + OC_END + "\n"
    tmp = CONFIG_TXT + ".overdrive-tmp"
    with open(tmp, "w") as f:
        f.write(txt)
    os.replace(tmp, CONFIG_TXT)


def apply_oc(job, preset="stock"):
    if preset not in PRESETS:
        raise RuntimeError("Unknown preset")
    if not is_pi5_family():
        raise RuntimeError("Overclock presets are only for Raspberry Pi 5 / 500")
    if not has("pkexec"):
        raise RuntimeError("pkexec not found - add the lines to config.txt by hand")
    job["msg"] = "Waiting for your password..."
    r = subprocess.run(["pkexec", sys.executable, os.path.abspath(__file__), "--apply-oc", preset],
                       capture_output=True, text=True, timeout=300)
    if r.returncode in (126, 127):
        raise RuntimeError("Cancelled - nothing was changed")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "Could not write config.txt").strip()[-200:])
    return {"preset": preset, "lines": PRESETS[preset]["lines"]}


# --------------------------------------------------------------- HTTP server
RUNNERS = {"cpu": bench_cpu, "mem": bench_mem, "disk": bench_disk, "wifi": wifi_scan,
           "bt": bt_scan, "net": net_test, "leds": led_show, "oc": apply_oc}
LAST_SEEN = [0.0]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            with open(os.path.join(HERE, "overdrive.html"), "rb") as f:
                return self.send(f.read(), ctype="text/html; charset=utf-8")
        if p == "/icon.svg":
            with open(os.path.join(HERE, "overdrive.svg"), "rb") as f:
                return self.send(f.read(), ctype="image/svg+xml")
        if p == "/api/info":
            return self.send(system_info())
        if p == "/api/live":
            LAST_SEEN[0] = time.time()
            return self.send(SAMPLER.snapshot(with_hist="hist" in parse_qs(u.query)))
        if p.startswith("/api/job/"):
            return self.send(JOBS.get(p.rsplit("/", 1)[1], {"state": "idle"}))
        if p == "/api/leds":
            return self.send(leds_list())
        if p == "/api/gpio":
            return self.send(gpio_read())
        if p == "/api/runs":
            return self.send(runs_action("get", {}))
        if p == "/api/oc":
            return self.send(oc_state())
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if p.startswith("/api/run/"):
                name = p.rsplit("/", 1)[1]
                if name not in RUNNERS:
                    return self.send({"error": "unknown job"}, 404)
                return self.send({"started": start_job(name, RUNNERS[name], **q)})
            if p.startswith("/api/runs/"):
                action = p.rsplit("/", 1)[1]
                if action not in ("add", "delete", "baseline", "rename"):
                    return self.send({"error": "unknown action"}, 404)
                return self.send(runs_action(action, self.body()))
            if p == "/api/reboot":
                r = subprocess.run(["systemctl", "reboot"], capture_output=True, text=True, timeout=20)
                return self.send({"ok": r.returncode == 0, "error": r.stderr.strip()})
            if p == "/api/peaks/reset":
                with SAMPLER.lock:
                    SAMPLER.reset_peaks()
                return self.send({"ok": True})
            if p == "/api/led":
                b = self.body()
                return self.send({"ok": led_set(b["name"], b.get("on")), "leds": leds_list()})
            if p == "/api/gpio":
                b = self.body()
                return self.send(gpio_set(b["pin"], b["mode"]))
        except Exception as e:
            return self.send({"error": str(e)}, 400)
        self.send({"error": "not found"}, 404)


def open_window(url, kiosk=False):
    for b in ("chromium", "chromium-browser"):
        if has(b):
            args = [b, f"--app={url}", "--ignore-gpu-blocklist", "--enable-gpu-rasterization",
                    "--start-maximized", "--no-first-run"]
            if kiosk:
                args.append("--kiosk")
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return
    import webbrowser
    webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(description="Overdrive - Raspberry Pi 500 hardware showcase")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--lan", action="store_true", help="listen on all interfaces (view from other devices)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--kiosk", action="store_true", help="full-screen app window")
    ap.add_argument("--apply-oc", choices=list(PRESETS), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.apply_oc:  # privileged helper, launched through pkexec
        write_oc_block(args.apply_oc)
        print("Wrote", args.apply_oc, "to", CONFIG_TXT)
        return
    url = f"http://localhost:{args.port}/"

    try:
        srv = ThreadingHTTPServer(("0.0.0.0" if args.lan else "127.0.0.1", args.port), Handler)
    except OSError:
        print("Overdrive is already running - opening it.")
        if not args.no_browser:
            open_window(url, args.kiosk)
        return
    SAMPLER.start()
    print(f"Overdrive {VERSION} running at {url}" + ("  (LAN enabled)" if args.lan else ""))
    if not args.no_browser:
        open_window(url, args.kiosk)

        def watchdog():  # quit a while after the app window is closed
            while True:
                time.sleep(10)
                if LAST_SEEN[0] and time.time() - LAST_SEEN[0] > 150:
                    print("App window closed - shutting down.")
                    os._exit(0)
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
