#!/usr/bin/env python3
"""Observe the upstream connections NGINX actually uses. No throughput timing."""
import argparse
import contextlib
import http.client
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time


def local_ports(count):
    with contextlib.ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(count)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]


@contextlib.contextmanager
def server(binary, root, name, port, http_directives):
    prefix = root / name
    prefix.mkdir()
    # Some builds set relative temporary directories. Keep every path private.
    (prefix / "logs").mkdir()
    config = prefix / "nginx.conf"
    config.write_text(f"""
daemon off;
master_process off;
worker_processes 1;
pid {prefix}/nginx.pid;
error_log stderr error;
events {{ worker_connections 2048; }}
http {{
    access_log off;
    keepalive_requests 10000;
    {http_directives}
}}
""")
    with (prefix / "stderr").open("w+") as log:
        proc = subprocess.Popen(
            [binary, "-p", str(prefix) + "/", "-c", str(config)],
            stdout=subprocess.DEVNULL, stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while proc.poll() is None and time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.025)
            else:
                log.seek(0)
                raise RuntimeError(f"{name} failed to start: {log.read()}")
            yield
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def one_case(binary, peers, capacity, rounds=8):
    proxy_port, *upstream_ports = local_ports(peers + 1)
    listen = "\n".join(f"listen 127.0.0.1:{port};" for port in upstream_ports)
    members = "\n".join(f"server 127.0.0.1:{port};" for port in upstream_ports)
    keepalive = "" if capacity is None else f"keepalive {capacity} local;"
    backend = f"""server {{ {listen}
        location / {{ return 200 "$server_port $connection $connection_requests"; }}
    }}"""
    frontend = f"""upstream origins {{ {members} {keepalive} }}
        server {{ listen 127.0.0.1:{proxy_port};
            location / {{ proxy_pass http://origins; }}
        }}"""
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        with server(binary, root, "backend", upstream_ports[0], backend), \
             server(binary, root, "frontend", proxy_port, frontend), \
             contextlib.closing(http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=3)) as client:
            rows = []
            for _ in range((rounds + 2) * peers):
                client.request("GET", "/")
                res = client.getresponse()
                data = res.read()
                if res.status != 200:
                    raise RuntimeError(f"GET failed: HTTP {res.status} {data!r}")
                rows.append(tuple(map(int, data.split())))
    rotation = [port for port, _, _ in rows]
    first_round = rotation[:peers]
    if set(first_round) != set(upstream_ports) or rotation != first_round * (rounds + 2):
        raise AssertionError("Unexpected peer rotation")
    measured = rows[2 * peers:]
    new = sum(reqs == 1 for _, _, reqs in measured)
    return dict(peers=peers, keepalive=capacity if capacity is not None else "default",
                measured=len(measured), new_connections=new, reused=len(measured) - new,
                unique_connection_ids=len({connection for _, connection, _ in measured}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("nginx", type=Path, help="NGINX 1.31.8 binary")
    args = parser.parse_args()
    binary = args.nginx.resolve(strict=True)
    version = subprocess.run([binary, "-v"], capture_output=True, check=True, text=True).stderr.strip()
    cases = [(1, None), (8, None), (32, None), (33, None), (33, 64), (64, None), (64, 64)]
    print(json.dumps({"binary": version, "warmup_rounds": 2, "measured_rounds": 8,
                      "cases": [one_case(binary, p, c) for p, c in cases]}, indent=2))
