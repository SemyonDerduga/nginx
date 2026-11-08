"""Compare real upstream routing at the total-weight wraparound boundary."""
import argparse
import http.client
from contextlib import ExitStack
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def routing(binary, directory):
    directory.mkdir()
    (directory / "logs").mkdir()
    with ExitStack() as stack:
        ports = []
        for _ in range(4):
            s = stack.enter_context(socket.socket())
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
    front, *peers = ports
    weights = (sys.maxsize, sys.maxsize, 5)
    upstream = "\n".join(f"server 127.0.0.1:{p} weight={w};"
                         for p, w in zip(peers, weights))
    origins = "\n".join(f'server {{ listen 127.0.0.1:{p}; return 200 "{i}"; }}'
                       for i, p in enumerate(peers))
    conf = directory / "nginx.conf"
    conf.write_text(f"""daemon off; master_process off; pid nginx.pid;
error_log stderr error; events {{ worker_connections 128; }}
http {{
    access_log off;
    upstream backend {{ random; {upstream} }}
    server {{ listen 127.0.0.1:{front}; location / {{ proxy_pass http://backend; }} }}
    {origins}
}}
""")
    cmd = [str(binary.resolve()), "-p", str(directory), "-c", str(conf)]
    subprocess.run(cmd + ["-t"], check=True, timeout=5)
    proc = subprocess.Popen(cmd)
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("NGINX exited before accepting connections")
            try:
                with socket.create_connection(("127.0.0.1", front), timeout=.2):
                    break
            except OSError:
                time.sleep(.02)
        else:
            raise RuntimeError("NGINX readiness timed out")
        conn = http.client.HTTPConnection("127.0.0.1", front, timeout=3)
        counts = [0] * len(peers)
        try:
            for _ in range(120):
                conn.request("GET", "/")
                response = conn.getresponse()
                body = response.read()
                if response.status != 200 or body not in (b"0", b"1", b"2"):
                    raise RuntimeError(f"Unexpected upstream response: {response.status}, {body!r}")
                counts[int(body)] += 1
        finally:
            conn.close()
        return counts
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("baseline", type=Path)
    cli.add_argument("candidate", type=Path)
    args = cli.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        expected = routing(args.baseline, root / "base")
        actual = routing(args.candidate, root / "candidate")
    if expected != [120, 0, 0] or actual != expected:
        raise SystemExit(f"routing mismatch: baseline={expected}, candidate={actual}")
    print(f"routing matches baseline: {actual}")
