#!/usr/bin/env python3
"""Paired A/B of two nginx front configs/binaries on macOS.

Metric: front worker CPU microseconds per request (user+sys from ps),
which does not depend on whether ab saturates first. RPS is printed too.
usage: WORK=/path/with/objs bench.py spec.json  ($WORK is expanded in paths)
"""
import json, os, re, socket, statistics, subprocess, sys, tempfile, time
from pathlib import Path

spec = json.loads(os.path.expandvars(Path(sys.argv[1]).read_text()))
rounds = spec.get("rounds", 6)
secs = spec.get("seconds", 5)
origin_bin = spec["origin"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(binary, d, conf):
    d.mkdir(parents=True)
    (d / "logs").mkdir()
    (d / "nginx.conf").write_text(conf)
    p = subprocess.Popen([binary, "-p", str(d), "-c", "nginx.conf"])
    time.sleep(0.3)
    return p


def cputime(pid):
    out = subprocess.check_output(["ps", "-o", "time=", "-p", str(pid)], text=True).strip()
    m = re.match(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", out)
    h, mi, s = m.groups()
    return (int(h or 0) * 3600 + int(mi) * 60 + float(s))


HEAD = "daemon off; master_process off; error_log stderr crit; pid nginx.pid;\n" \
       "events { worker_connections 4096; }\n"

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    op = free_port()
    origin = start(origin_bin, td / "origin", HEAD + f"""http {{ access_log off;
keepalive_requests 10000000; server {{ listen 127.0.0.1:{op};
location / {{ return 200 '{{"ok":true,"items":[1,2,3]}}'; }} }} }}""")
    fronts = []
    for i, v in enumerate(spec["variants"]):
        fp = free_port()
        conf = HEAD + "http { keepalive_requests 10000000;\n" + v.get("http", "") + \
            f"\nupstream origin {{ server 127.0.0.1:{op}; keepalive 64; }}\n" \
            f"server {{ listen 127.0.0.1:{fp};\n" \
            "location /api/ { proxy_pass http://origin; proxy_http_version 1.1;" \
            " proxy_set_header Connection \"\"; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; }\n" \
            "location /direct { return 200 'ok'; } } }"
        fronts.append((v["name"], start(v["binary"], td / f"f{i}", conf), fp))
    res = {name: [] for name, _, _ in fronts}
    try:
        for sc in spec["scenarios"]:
            hdrs = []
            for h in sc["headers"]:
                hdrs += ["-H", h]
            per = {name: [] for name, _, _ in fronts}
            for r in range(rounds):
                order = fronts if r % 2 == 0 else fronts[::-1]
                for name, proc, port in order:
                    c0 = cputime(proc.pid)
                    out = subprocess.run(["ab", "-q", "-k", "-c", "32", "-t", str(secs), "-n", "100000000"]
                                         + hdrs + [f"http://127.0.0.1:{port}{sc['path']}"],
                                         capture_output=True, text=True).stdout
                    c1 = cputime(proc.pid)
                    n = int(re.search(r"Complete requests:\s+(\d+)", out).group(1))
                    failed = int(re.search(r"Failed requests:\s+(\d+)", out).group(1))
                    non2xx = re.search(r"Non-2xx responses:\s+(\d+)", out)
                    assert failed == 0 and not non2xx, out
                    rps = float(re.search(r"Requests per second:\s+([\d.]+)", out).group(1))
                    per[name].append(((c1 - c0) * 1e6 / n, rps))
            base = fronts[0][0]
            print(f"\n## {sc['name']}")
            for name in per:
                us = [x[0] for x in per[name]]
                rps = [x[1] for x in per[name]]
                line = f"{name:28s} cpu/req {statistics.median(us):6.2f} us   rps {statistics.median(rps):8.0f}"
                if name != base:
                    d = [100 * (a[0] / b[0] - 1) for a, b in zip(per[name], per[base])]
                    dr = [100 * (a[1] / b[1] - 1) for a, b in zip(per[name], per[base])]
                    line += (f"   cpu Δ {statistics.median(d):+5.1f}% [{min(d):+.1f},{max(d):+.1f}]"
                             f"   rps Δ {statistics.median(dr):+5.1f}%")
                print(line, flush=True)
    finally:
        for _, p, _ in fronts:
            p.terminate()
        origin.terminate()
