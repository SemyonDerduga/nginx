#!/usr/bin/env python3
"""Differential fuzz of HTTP/1 header parsing: base vs candidate nginx.

Each case sends the same request (random URI and query string, random
header values, random TCP split points) through both binaries proxying to
an echo backend. The backend returns the raw request it got plus a random
response header, so request-line, request-header and upstream-response-
header parsing are compared. Both binaries also write the same variables
to access logs with default and JSON escaping; the logs must match.
"""
import os, random, socket, subprocess, sys, tempfile, threading, time
from pathlib import Path

base, cand = sys.argv[1], sys.argv[2]
cases = int(sys.argv[3]) if len(sys.argv) > 3 else 3000
arch = sys.argv[4] if len(sys.argv) > 4 else None
rnd = random.Random(int(os.environ.get("SEED", "1")))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- echo backend -------------------------------------------------------
BP = free_port()


def handle(c):
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = c.recv(65536)
            if not chunk:
                return
            data += chunk
        want = b""
        for line in data.split(b"\r\n"):
            if line.lower().startswith(b"x-want: "):
                want = bytes.fromhex(line[8:].decode())
        body = data.hex().encode()
        resp = (b"HTTP/1.1 200 OK\r\nX-R: " + want + b"\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        c.sendall(resp)
    finally:
        c.close()


def backend():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", BP))
    s.listen(256)
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


threading.Thread(target=backend, daemon=True).start()


# --- nginx --------------------------------------------------------------
def launch(binary, d):
    p = free_port()
    d.mkdir()
    (d / "logs").mkdir()
    conf = d / "nginx.conf"
    conf.write_text(f"""daemon off; master_process off; error_log stderr crit;
pid nginx.pid; events {{ worker_connections 512; }}
http {{ client_header_buffer_size 64;
log_format plain '$request|$args|$http_user_agent|$http_cookie|$http_x_a|$upstream_http_x_r';
log_format json escape=json '$request|$args|$http_user_agent|$http_cookie|$http_x_a|$upstream_http_x_r';
access_log logs/plain.log plain; access_log logs/json.log json;
large_client_header_buffers 4 16k; proxy_buffer_size 4k;
upstream b {{ server 127.0.0.1:{BP}; }}
server {{ listen 127.0.0.1:{p}; location / {{ proxy_pass http://b; }} }} }}""")
    cmd = [binary, "-p", str(d), "-c", str(conf)]
    if arch:
        cmd = ["arch", "-" + arch] + cmd
    proc = subprocess.Popen(cmd)
    for _ in range(200):
        try:
            socket.create_connection(("127.0.0.1", p), timeout=0.1).close()
            return proc, p
        except OSError:
            time.sleep(0.05)
    raise SystemExit("nginx did not start: " + binary)


ALPHA = [bytes([b]) for b in range(256)]
INTERESTING = [b" ", b"\t", b"\r", b"\n", b"\0", b"\x7f", b"\x80", b"\xff",
               b"\x1f", b"\x21", b":", b"a", b"A" * 16, b"z" * 31, b" " * 3]


def value():
    n = rnd.choice([0, 1, 5, 15, 16, 17, 31, 32, 33, 47, 63, 64, 100, 300, 2048,
                    rnd.randrange(0, 600)])
    mode = rnd.random()
    out = bytearray()
    while len(out) < n:
        r = rnd.random()
        if mode < 0.4:          # mostly clean tokens with rare specials
            out += rnd.choice([b"a", b"Z", b"9", b"=", b";", b"/"]) if r < 0.97 \
                else rnd.choice(INTERESTING)
        elif mode < 0.7:
            out += rnd.choice(INTERESTING) if r < 0.3 else bytes([rnd.randrange(0x21, 0x7f)])
        else:
            out += rnd.choice(ALPHA)
    return bytes(out[:n]) if mode >= 0.7 else bytes(out)


def uri():
    u = b"/" + rnd.choice([b"", b"p", b"api/v1/items", b"a/./b", b"a//b", b"%41x"])
    if rnd.random() < 0.8:
        if rnd.random() < 0.7:     # mostly well-formed query strings
            n = rnd.choice([5, 15, 16, 17, 31, 32, 33, 64, 200, rnd.randrange(0, 900)])
            q = bytes(rnd.choice(b"abcXYZ019=&_-.%/+?:@~") if rnd.random() > 0.02
                      else rnd.choice(b"\x7f\x80\xff#\x01\t\"\\")
                      for _ in range(n))
        else:
            q = value().replace(b" ", b"+").replace(b"\r", b"").replace(b"\n", b"")
        u += b"?" + q
        if rnd.random() < 0.1:
            u += rnd.choice([b"#frag", b"\x7f", b"\x01", b" ", b"\t"]) + value()[:20]
    return u


def build_request():
    hdrs = [b"Host: x"]
    for _ in range(rnd.randrange(0, 6)):
        name = rnd.choice([b"Cookie", b"X-A", b"User-Agent", b"X-Long-Header-Name"])
        hdrs.append(name + b":" + rnd.choice([b"", b" ", b"  "]) + value()
                    + rnd.choice([b"", b"", b" ", b"  \t"]))
    want = value().replace(b"\r", b"").replace(b"\n", b"")
    if rnd.random() < 0.1:
        want += rnd.choice([b"\r", b"\n", b"\0"]) + value()
    hdrs.append(b"X-Want: " + want.hex().encode())
    eol = rnd.choice([b"\r\n", b"\r\n", b"\n"])
    return b"GET " + uri() + b" HTTP/1.1" + eol + eol.join(hdrs) + eol + b"Connection: close" + eol + eol


def splits(req):
    k = rnd.randrange(0, 5)
    pts = sorted(rnd.sample(range(1, len(req)), min(k, len(req) - 1)))
    return [req[i:j] for i, j in zip([0] + pts, pts + [len(req)])]


def send(port, parts):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        try:
            for i, part in enumerate(parts):
                s.sendall(part)
                if i + 1 < len(parts):
                    time.sleep(0.002)
        except BrokenPipeError:
            pass               # nginx rejected early; read what it sent
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except (ConnectionResetError, socket.timeout) as e:
        data = b"<" + type(e).__name__.encode() + b">"
    finally:
        s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = [l for l in head.split(b"\r\n")
             if not l.lower().startswith((b"date:", b"server:"))]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    procs = [launch(base, td / "base"), launch(cand, td / "cand")]
    bad = 0
    statuses = {}
    try:
        for i in range(cases):
            req = build_request()
            parts = splits(req)
            a = send(procs[0][1], parts)
            b = send(procs[1][1], parts)
            st = a.split(b"\r\n", 1)[0][:12]
            statuses[st] = statuses.get(st, 0) + 1
            if a != b:
                bad += 1
                if bad <= 3:
                    print("MISMATCH", req[:300], "\n A:", a[:300], "\n B:", b[:300])
    finally:
        for p, _ in procs:
            p.terminate()
            p.wait()
    for log in ("plain.log", "json.log"):
        la = (td / "base" / "logs" / log).read_bytes()
        lb = (td / "cand" / "logs" / log).read_bytes()
        same = la == lb
        print(f"{log}: {la.count(b'\n')} lines, {'identical' if same else 'DIFFERENT'}")
        if not same:
            bad += 1
            for x, y in zip(la.split(b"\n"), lb.split(b"\n")):
                if x != y:
                    print(" A:", x[:200], "\n B:", y[:200])
                    break
    print(f"cases={cases} mismatches={bad} statuses={statuses}")
    sys.exit(1 if bad else 0)
