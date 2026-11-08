#!/usr/bin/env python3
"""Differential fuzz of HTTP/1 header parsing: base vs candidate nginx.

Each case sends the same request (random URI and query string, random
header values, random TCP split points) through both binaries proxying to
an echo backend. The backend returns the raw request it got plus a random
response header, so request-line, request-header and upstream-response-
header parsing are compared. A second pass queues each complete request
before either worker can read it, and compares the entire default and
JSON logs. An early 400 can otherwise log a timing-dependent prefix.
Requires POSIX signals and Python 3.11 or newer.
"""
import http.client
import io
import os, random, signal, socket, subprocess, sys, tempfile, threading, time
from contextlib import ExitStack
from pathlib import Path

base, cand = sys.argv[1], sys.argv[2]
cases = int(sys.argv[3]) if len(sys.argv) > 3 else 3000
arch = sys.argv[4] if len(sys.argv) > 4 else None
seed = int(os.environ.get("SEED", "1"))
rnd = random.Random(seed)
if cases < 1:
    raise SystemExit("cases must be positive")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- echo backend -------------------------------------------------------
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(256)
BP = listener.getsockname()[1]


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
    while True:
        c, _ = listener.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


threading.Thread(target=backend, daemon=True).start()


# --- nginx --------------------------------------------------------------
def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def launch(binary, d, stack, queued):
    p = free_port()
    d.mkdir(parents=True)
    (d / "logs").mkdir()
    conf = d / "nginx.conf"
    logs = ("access_log logs/plain.log plain; access_log logs/json.log json;"
            if queued else "access_log off;")
    conf.write_text(f"""daemon off; master_process off; error_log stderr crit;
pid nginx.pid; events {{ worker_connections 512; }}
http {{ client_header_buffer_size 64;
log_format plain '$request|$args|$http_user_agent|$http_cookie|$http_x_a|$upstream_http_x_r';
log_format json escape=json '$request|$args|$http_user_agent|$http_cookie|$http_x_a|$upstream_http_x_r';
{logs}
large_client_header_buffers 4 16k; proxy_buffer_size 4k;
upstream b {{ server 127.0.0.1:{BP}; }}
server {{ listen 127.0.0.1:{p}; location / {{ proxy_pass http://b; }} }} }}""")
    cmd = [binary, "-p", str(d), "-c", str(conf)]
    if arch:
        cmd = ["arch", "-" + arch] + cmd
    proc = subprocess.Popen(cmd)
    stack.callback(stop, proc)
    for _ in range(200):
        if proc.poll() is not None:
            break
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


class ResponseStream(io.BytesIO):
    """Let HTTPResponse consume pipelined messages from the same byte stream."""
    def makefile(self, mode):
        return self

    def close(self):
        pass  # HTTPResponse closes its file after each message


def responses(data, ended):
    if not data or ended == "timeout":
        raise ValueError(f"{ended} after {len(data)} response bytes: {data[:120]!r}")

    messages = []
    stream = ResponseStream(data)
    while stream.tell() < len(data):
        start = stream.tell()
        if not data.startswith(b"HTTP/", start):
            # A generated LF can terminate a GET as HTTP/0.9. Its response
            # has no headers, so require a complete echo or nginx error page.
            body = data[start:]
            if body.startswith(b"47455420"):
                complete = bytes.fromhex(body.decode("ascii")).endswith(b"\r\n\r\n")
            else:
                complete = body.startswith(b"<html>") and body.endswith(b"</html>\r\n")
            if ended != "eof" or not complete:
                raise ValueError(f"incomplete or unexpected HTTP/0.9 response: {body[:120]!r}")
            messages.append((b"HTTP/0.9", body))
            break

        reply = http.client.HTTPResponse(stream)
        reply.begin()
        header = data[start:stream.tell()]
        if not header.endswith(b"\r\n\r\n"):
            raise ValueError("incomplete response headers")
        if reply.length is None and not reply.chunked and ended != "eof":
            raise ValueError("reset during close-delimited response")
        body_start = stream.tell()
        reply.read()  # validates Content-Length and chunk lengths
        body = data[body_start:stream.tell()]  # keep wire bytes, including trailers
        if reply.chunked and not body.endswith(b"\r\n\r\n"):
            raise ValueError("incomplete chunked trailer")
        lines = [l for l in header[:-4].split(b"\r\n")
                 if not l.lower().startswith((b"date:", b"server:"))]
        messages.append((b"\r\n".join(lines), body))
    return messages


def send(server, parts, queued):
    proc, port = server
    data = bytearray()
    ended = "eof"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if queued:
            # master_process off means proc is the only reader. Wait for
            # SIGSTOP to take effect before queuing the whole request locally.
            os.kill(proc.pid, signal.SIGSTOP)
            try:
                _, status = os.waitpid(proc.pid, os.WUNTRACED)
                if not os.WIFSTOPPED(status):
                    raise RuntimeError("nginx exited before request delivery")
                s.sendall(b"".join(parts))
            finally:
                if proc.poll() is None:
                    os.kill(proc.pid, signal.SIGCONT)
        else:
            try:
                for i, part in enumerate(parts):
                    s.sendall(part)
                    if i + 1 < len(parts):
                        time.sleep(0.002)
            except (BrokenPipeError, ConnectionResetError):
                pass  # an early rejection can still have a complete response
        try:
            while chunk := s.recv(65536):
                data.extend(chunk)
        except ConnectionResetError:
            ended = "reset"
        except socket.timeout:
            ended = "timeout"
    try:
        return responses(bytes(data), ended)
    except (http.client.HTTPException, ValueError) as e:
        raise RuntimeError(f"{ended} after {len(data)} bytes, {bytes(data[:160])!r}: {e}") from e


probe = b"GET /probe HTTP/1.1\r\nHost: x\r\nX-Probe: fuzz-ready\r\nConnection: close\r\n\r\n"


def verify(server, queued):
    if server[0].poll() is not None:
        raise RuntimeError("nginx exited")
    reply = send(server, [probe], queued)
    if (len(reply) != 1 or not reply[0][0].startswith(b"HTTP/1.1 200 ")
            or b"X-Probe: fuzz-ready".hex().encode() not in reply[0][1]):
        raise RuntimeError(f"echo backend probe failed: {reply!r}")


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    bad = 0
    for queued in (False, True):
        mode = "queued" if queued else "fragmented"
        dirs = [td / mode / name for name in ("base", "cand")]
        statuses = {}
        rnd.seed(seed)
        with ExitStack() as stack:
            servers = [launch(binary, d, stack, queued) for binary, d in zip((base, cand), dirs)]
            for server in servers:
                verify(server, queued)
            for i in range(cases):
                req = build_request()
                parts = splits(req)
                try:
                    a, b = [send(server, parts, queued) for server in servers]
                except (OSError, RuntimeError) as e:
                    raise SystemExit(f"{mode} seed={seed} case={i} request={req[:160]!r}: {e}") from e
                st = a[0][0].split(b"\r\n", 1)[0][:12]
                statuses[st] = statuses.get(st, 0) + 1
                if a != b:
                    bad += 1
                    if bad <= 3:
                        print("MISMATCH", mode, "case", i, req[:300], repr(a)[:300], repr(b)[:300])
            for server in servers:
                verify(server, queued)
        print(f"{mode} cases={cases} statuses={statuses}")
        if cases >= 100 and not (statuses.get(b"HTTP/1.1 200") and statuses.get(b"HTTP/1.1 400")):
            raise SystemExit(f"{mode} seed={seed}: expected both accepted and rejected fuzz inputs")
        if queued:
            for log in ("plain.log", "json.log"):
                la, lb = [(d / "logs" / log).read_bytes() for d in dirs]
                n = la.count(b"\n")
                same = la == lb and n >= cases + 2  # two echo probes
                print(f"{log}: {n} lines, {'identical' if same else 'DIFFERENT or incomplete'}")
                if not same:
                    bad += 1
                    for j, (x, y) in enumerate(zip(la.split(b"\n"), lb.split(b"\n")), 1):
                        if x != y:
                            print("line", j, "A:", x[:200], "B:", y[:200])
                            break
    print(f"seed={seed} mismatches={bad}")
    sys.exit(1 if bad else 0)
