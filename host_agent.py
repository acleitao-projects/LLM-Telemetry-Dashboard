"""Host agent - tiny PASSIVE telemetry helper for the host that runs
llama.cpp (expected: Linux).

Run on the inference host:
    python3 host_agent.py            # listens on 127.0.0.1:8091
    python3 host_agent.py --port 8091 --host 0.0.0.0

It only READS system information, GPU information and process arguments.
It never controls Docker, restarts containers, loads/unloads models, or
sends inference requests of any kind.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---------------------------------------------------------------------------
# system info (Linux, stdlib only)
# ---------------------------------------------------------------------------
def _read_file(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def system_info() -> dict:
    mem_total_kb = 0
    mem_avail_kb = 0
    for line in _read_file("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            mem_total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            mem_avail_kb = int(line.split()[1])
    cpu_model = ""
    cpu_count = 0
    for line in _read_file("/proc/cpuinfo").splitlines():
        if line.startswith("processor"):
            cpu_count += 1
        if line.startswith("model name") and not cpu_model:
            cpu_model = line.split(":", 1)[1].strip()
    return {
        "hostname": platform.node() or "inference-host",
        "os": platform.system() + " " + platform.release(),
        "os_detail": _read_file("/etc/os-release").splitlines()[0] if _read_file("/etc/os-release") else None,
        "kernel": platform.release(),
        "cpu_model": cpu_model or platform.processor(),
        "cpu_threads": cpu_count or (platform.cpu_count() or 0),
        "ram_mb": mem_total_kb // 1024,
        "ram_used_mb": (mem_total_kb - mem_avail_kb) // 1024,
    }


def cpu_percent() -> float:
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = list(map(int, parts[1:]))
        idle = vals[3]
        total = sum(vals)
        # second sample after a short delay
        import time
        time.sleep(0.25)
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals2 = list(map(int, parts[1:]))
        idle2 = vals2[3]
        total2 = sum(vals2)
        dt = total2 - total
        di = idle2 - idle
        if dt <= 0:
            return 0.0
        return round(100.0 * (dt - di) / dt, 1)
    except Exception:
        return 0.0


def power_w() -> float | None:
    v = _read_file("/sys/class/power_supply/AC0/wattage") or _read_file(
        "/sys/class/power_supply/AC/wattage")
    if v:
        try:
            return int(v.strip()) / 1000.0
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# GPU (nvidia-smi, read-only)
# ---------------------------------------------------------------------------
def gpu_info() -> dict:
    out: dict = {"gpus": [], "nvidia_driver": None, "cuda": None, "pcie": None}
    smi = shutil.which("nvidia-smi")
    if not smi:
        return out
    try:
        r = subprocess.run([
            smi,
            "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,"
            "temperature.gpu,power.limit,power.draw,pcie.link.gen.max,"
            "pcie.link.width.max,driver_version",
            "--format=csv,noheader,nounits",
        ], capture_output=True, text=True, timeout=5)
    except Exception:
        try:
            r = subprocess.run(["nvidia-smi", "-q", "-d", "MEMORY,UTILIZATION,POWER,PCIE",
                                "-i", "0"], capture_output=True, text=True, timeout=5)
        except Exception:
            return out
    if r.returncode != 0 or not r.stdout:
        try:
            r2 = subprocess.run([smi, "-q"], capture_output=True, text=True, timeout=5)
            if r2.returncode == 0:
                txt = r2.stdout
                m = re.search(r"Driver Version\s*:\s*(\S+)", txt)
                if m:
                    out["nvidia_driver"] = m.group(1)
                m = re.search(r"CUDA Version\s*:\s*(\d+)", txt)
                if m:
                    out["cuda"] = m.group(1)
                m = re.search(r"Name\s*:\s*(.+)", txt)
                mem = re.search(r"Total\s*:\s*([\d.]+) MiB", txt)
                used = re.search(r"Used\s*:\s*([\d.]+) MiB", txt)
                util = re.search(r"GPU\s*:\s*(\d+)%", txt)
                out["gpus"].append({
                    "name": m.group(1).strip() if m else "unknown",
                    "vram_mb": int(float(mem.group(1))) if mem else None,
                    "vram_used_mb": int(float(used.group(1))) if used else None,
                    "util": float(util.group(1)) if util else None,
                })
        except Exception:
            pass
        return out
    for line in r.stdout.strip().splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 12:
            continue
        try:
            g = {
                "index": int(cols[0]),
                "uuid": cols[1],
                "name": cols[2],
                "vram_mb": int(float(cols[3])),
                "vram_used_mb": int(float(cols[4])),
                "util": float(cols[5]),
                "temp_c": float(cols[6]),
                "power_w": float(cols[8]),
                "pcie": f"Gen{cols[9]} x{cols[10]}",
            }
        except (ValueError, IndexError):
            continue
        out["gpus"].append(g)
        if not out["pcie"] and g.get("pcie"):
            out["pcie"] = g["pcie"]
        out["nvidia_driver"] = cols[11]
    return out


# ---------------------------------------------------------------------------
# llama.cpp process (read-only /proc scan)
# ---------------------------------------------------------------------------
def llama_process() -> dict:
    out: dict = {"argv": [], "pid": None, "version": None, "commit": None,
                 "docker_image": None, "container_id": None}
    cands = []
    for pid_dir in __import__("os").listdir("/proc"):
        if not pid_dir.isdigit():
            continue
        try:
            with open(f"/proc/{pid_dir}/cmdline", "rb") as f:
                raw = f.read()
            argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        except OSError:
            continue
        if not argv:
            continue
        joined = " ".join(argv)
        if "llama-server" in joined or "llama-server" in (argv[0] or ""):
            cands.append((int(pid_dir), argv))
    if not cands:
        return out
    pid, argv = max(cands, key=lambda c: c[0])
    out["argv"] = argv
    out["pid"] = pid
    # version from --version flag is not present; try binary --help is forbidden
    # (it would exec the binary). Use build info file if present next to binary.
    exe = ""
    try:
        exe = __import__("os").readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    m = re.search(r"llama\.cpp[^/]*?/b(\d+)", exe)
    if m:
        out["version"] = "b" + m.group(1)
    # docker hints from cgroup
    cgroup = _read_file(f"/proc/{pid}/cgroup")
    m = re.search(r"/docker/([0-9a-f]{12,})|/[0-9a-f]{64}\.scope", cgroup)
    if m:
        out["container_id"] = (m.group(1) or m.group(0))[:12]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8091)
    args = ap.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            try:
                if p == "/info":
                    d = system_info()
                    d["cpu_pct"] = cpu_percent()
                    pw = power_w()
                    if pw:
                        d["power_w"] = pw
                    self._send(d)
                elif p == "/gpu":
                    self._send(gpu_info())
                elif p == "/llama":
                    self._send(llama_process())
                elif p == "/health":
                    self._send({"status": "ok", "agent": "host"})
                else:
                    self.send_response(404)
                    self.end_headers()
            except Exception as e:
                self._send({"error": str(e)[:200]})

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"host agent listening on http://{args.host}:{args.port} (passive)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
