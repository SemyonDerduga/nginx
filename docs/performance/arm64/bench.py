#!/usr/bin/env python3
"""Compare nginx variants on macOS with ApacheBench. Paths may contain $WORK."""
import argparse
import csv
import itertools
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

HEAD = ("daemon off; master_process off; error_log stderr crit; pid nginx.pid;\n"
        "events { worker_connections 4096; }\n")
FIELDS = ("scenario", "round", "order", "variant", "requests", "failed", "non2xx",
          "ab_seconds", "rps", "cpu_start_s", "cpu_end_s", "cpu_us_req")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def start(stack, binary, directory, conf, port):
    directory.mkdir()
    (directory / "logs").mkdir()
    (directory / "nginx.conf").write_text(HEAD + conf)
    proc = subprocess.Popen([binary, "-p", str(directory), "-c", "nginx.conf"])
    stack.callback(stop, proc)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{binary} exited at startup (status {proc.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return proc
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"{binary} did not listen on port {port}")


def cputime(pid):
    if platform.system() == "Linux":
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    value = subprocess.check_output(["ps", "-o", "time=", "-p", str(pid)], text=True).strip()
    match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", value)
    if not match:
        raise ValueError(f"unsupported ps CPU time: {value!r}")
    h, m, s = match.groups()
    return int(h or 0) * 3600 + int(m) * 60 + float(s)


def ab_results(out):
    def field(label, converter):
        match = re.search(r"^" + re.escape(label) + r":\s*([\d.]+)", out, re.M)
        if not match:
            raise ValueError(f"missing ApacheBench field {label!r}\n{out}")
        return converter(match[1])

    failed = field("Failed requests", int)
    non2xx = field("Non-2xx responses", int) if "Non-2xx responses:" in out else 0
    requests = field("Complete requests", int)
    if not requests or failed or non2xx:
        raise RuntimeError(f"ApacheBench reported complete={requests}, failed={failed}, non2xx={non2xx}")
    return dict(requests=requests, failed=failed, non2xx=non2xx,
                ab_seconds=field("Time taken for tests", float), rps=field("Requests per second", float))


def summarize(scenario, per):
    print(f"\n## {scenario}", flush=True)
    for name, rows in per.items():
        print(f"{name}: {statistics.median(r['cpu_us_req'] for r in rows):.2f} us/req, "
              f"{statistics.median(r['rps'] for r in rows):.0f} RPS")
    for ref, name in itertools.combinations(per, 2):
        print(f"{name} vs {ref}: ", end="")
        for key in ("cpu_us_req", "rps"):
            deltas = [100 * (a[key] / b[key] - 1) for a, b in zip(per[name], per[ref])]
            print(f" {key} {statistics.median(deltas):+.1f}% [{min(deltas):+.1f},{max(deltas):+.1f}]", end="")
        print(flush=True)


def version(command):
    return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, help="new per-run CSV (default: timestamped filename)")
    parser.add_argument("--max-requests", type=int, default=5000000,
                        help="ApacheBench sample capacity per run; raise if reached (default: 5000000)")
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    rounds, secs = spec.get("rounds", 6), spec.get("seconds", 5)
    variants = spec["variants"]
    names = [v["name"] for v in variants]
    if len(set(names)) != len(names) or len(names) < 2 or rounds < 1 or secs < 1 or args.max_requests < 32:
        parser.error("need distinct variants, positive rounds/seconds and a request limit of at least 32")

    out = args.output or Path(time.strftime("arm64-rounds-%Y%m%d-%H%M%S.csv"))
    meta = out.with_name(out.name + ".metadata.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    binaries = [os.path.expandvars(path) for path in (spec["origin"], *(v["binary"] for v in variants))]
    info = dict(platform=platform.platform(), machine=platform.machine(), logical_cpus=os.cpu_count(),
                ab=version(["ab", "-V"]), binaries={b: version([b, "-V"]) for b in dict.fromkeys(binaries)},
                max_requests=args.max_requests, spec=spec)
    if platform.system() == "Darwin":
        info.update(macos=version(["sw_vers"]), cpu=version(["sysctl", "-n", "machdep.cpu.brand_string"]),
                    memory_bytes=int(version(["sysctl", "-n", "hw.memsize"])))
    if out.exists() or meta.exists():
        parser.error(f"choose a new output path: {out}")
    with meta.open("x") as f:
        json.dump(info, f, indent=2)
    with out.open("x", newline="") as f, tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        writer = csv.DictWriter(f, FIELDS)
        writer.writeheader()
        td, op = Path(tmp), free_port()
        origin = start(stack, binaries[0], td / "origin", f"""http {{ access_log off;
keepalive_requests 10000000; server {{ listen 127.0.0.1:{op};
location / {{ return 200 '{{"ok":true,"items":[1,2,3]}}'; }} }} }}""", op)
        fronts = []
        for i, v in enumerate(variants):
            port = free_port()
            conf = ("http { keepalive_requests 10000000;\n" + v.get("http", "") +
                    f"\nupstream origin {{ server 127.0.0.1:{op}; keepalive 64; }}\n"
                    f"server {{ listen 127.0.0.1:{port};\n"
                    "location /api/ { proxy_pass http://origin; proxy_http_version 1.1;"
                    ' proxy_set_header Connection ""; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; }\n'
                    "location /direct { return 200 'ok'; } } }")
            fronts.append((v["name"], start(stack, binaries[i+1], td / f"f{i}", conf, port), port))
        for sc in spec["scenarios"]:
            hdrs = [arg for h in sc["headers"] for arg in ("-H", h)]
            per = {name: [] for name in names}
            for r in range(rounds):
                offset = (r // 2) % len(fronts)
                order = fronts[offset:] + fronts[:offset]
                if r % 2:
                    order.reverse()
                for pos, (name, proc, port) in enumerate(order, 1):
                    c0 = cputime(proc.pid)
                    cmd = ["ab", "-q", "-k", "-c", "32", "-t", str(secs), "-n", str(args.max_requests),
                           *hdrs, f"http://127.0.0.1:{port}{sc['path']}"]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=secs + 60)
                    if result.returncode:
                        raise RuntimeError(f"ApacheBench exited {result.returncode}\n{result.stderr}\n{result.stdout}")
                    if proc.poll() is not None or origin.poll() is not None:
                        raise RuntimeError("NGINX exited during the run")
                    c1 = cputime(proc.pid)
                    row = ab_results(result.stdout)
                    if row["requests"] >= args.max_requests:
                        raise RuntimeError("sample capacity reached; rerun with a larger --max-requests")
                    if c1 <= c0 or row["rps"] <= 0:
                        raise RuntimeError("no measurable CPU time or throughput; increase seconds")
                    row.update(scenario=sc["name"], round=r+1, order=pos, variant=name,
                               cpu_start_s=c0, cpu_end_s=c1, cpu_us_req=(c1-c0)*1e6/row["requests"])
                    writer.writerow(row)
                    f.flush()
                    per[name].append(row)
            summarize(sc["name"], per)
    print(f"Runs: {out}\nEnvironment: {meta}")


if __name__ == "__main__":
    main()
