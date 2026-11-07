"""Compare HTTP/1.1 loopback latency at constant offered rate (Linux x86-64)."""
import argparse
import csv
import gzip
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time

here = Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('baseline', type=Path)
p.add_argument('candidate', type=Path)
p.add_argument('--rps', type=int, default=25000)
p.add_argument('--seconds', type=float, default=1.25)
p.add_argument('--rounds', type=int, default=6)
p.add_argument('--after-aa', type=Path, help='Require all five round-4 A/A pairs in this CSV before starting')
p.add_argument('--out', type=Path, default=here/'results')
a = p.parse_args()
if a.after_aa:
    need = {('4', profile, variant) for profile in (
        'short/direct','typical/direct','cookie2048/direct','typical/proxy','cookie2048/proxy')
        for variant in ('base','candidate')}
    with a.after_aa.open() as f:
        seen = {(r['round'], r['scenario'], r['variant']) for r in csv.DictReader(f)}
    if need-seen:
        raise SystemExit('Round 4 still missing ' + str(sorted(need-seen)))
cores = sorted(os.sched_getaffinity(0))
if len(cores)<2: raise SystemExit('At least two logical cores required')
a.out.mkdir(exist_ok=True, parents=True)
(a.out/'samples').mkdir(exist_ok=True)

def server(binary, prefix):
    prefix.mkdir(); (prefix/'logs').mkdir()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    cfg = prefix/'nginx.conf'
    cfg.write_text('daemon off; master_process off; error_log stderr error; pid nginx.pid;\n'
        'events {worker_connections 2048;} http {access_log off; keepalive_timeout 60s; '
        'keepalive_requests 10000000; server { listen 127.0.0.1:%d; '
        'location /direct {return 200 "OK";} } }\n' % port)
    proc = subprocess.Popen([str(binary.resolve()),'-p',str(prefix),'-c',str(cfg)],
                            preexec_fn=lambda:os.sched_setaffinity(0,{cores[0]}))
    for _ in range(100):
        if proc.poll() is not None: raise RuntimeError('NGINX exited')
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.1): return proc,port
        except OSError: time.sleep(.015)
    proc.terminate(); proc.wait()
    raise RuntimeError('NGINX readiness failed')

def query(port, hdr, n, out):
    buf = subprocess.check_output([str(here/'rate'),str(port),str(a.rps),str(n),
                  str(hdr),str(out)], text=True,preexec_fn=lambda:os.sched_setaffinity(0,{cores[-1]}))
    finished,elapsed,peak = map(int,buf.strip().split(','))
    if finished!=n or out.stat().st_size!=24*n: raise ValueError('Incomplete data')
    return finished,elapsed,peak

def percentiles(numbers):
    s = sorted(numbers)
    return [s[math.ceil(len(s)*q)-1]/1000 for q in (.5,.95,.99)]
typical = (b'User-Agent: Mozilla/5.0 (X11; Linux x86_64) TestBrowser/128.0\r\n'
           b'Accept: text/html,application/xhtml+xml,application/json;q=0.9\r\n'
           b'Accept-Encoding: gzip, deflate, br\r\nCookie: session='+b'a'*80+b'; pref=en\r\n')
fields=['round','order','scenario','variant','target_rps','n','elapsed_ns','completed_rps',
        'peak_inflight'] + [kind+'_'+p+'_us' for kind in ('sched','lag','wire') for p in ('p50','p95','p99')]
with tempfile.TemporaryDirectory() as tmp, (a.out/'fixed.csv').open('w',newline='') as log:
    d=Path(tmp); servers={}; writer=csv.writer(log); writer.writerow(fields)
    try:
        for label,binary in [('base',a.baseline),('candidate',a.candidate)]:
            servers[label]=server(binary,d/label)
        profiles={'typical':typical, 'cookie2048':b'Cookie: '+b'A'*2048+b'\r\n'}
        for profile,h in profiles.items():
            (d/profile).write_bytes(h)
            for proc,port in servers.values(): query(port,d/profile,a.rps//4,d/'warm')
        for r in range(a.rounds):
            for j,profile in enumerate(list(profiles)[::1 if r%2==0 else -1]):
                order=['base','candidate']
                if (r+list(profiles).index(profile))%2: order.reverse()
                for k,v in enumerate(order):
                    n=int(a.seconds*a.rps)
                    finished,duration,peak=query(servers[v][1], d/profile,n,d/'samples')
                    data=(d/'samples').read_bytes()
                    samples=list(struct.iter_unpack('=QQQ',data))
                    sched=percentiles(end-due for due,send,end in samples)
                    lag=percentiles(send-due for due,send,end in samples)
                    wire=percentiles(end-send for due,send,end in samples)
                    dest=a.out/'samples'/f'{r+1}_{profile}_{v}.bin.gz'
                    dest.write_bytes(gzip.compress(data,compresslevel=1,mtime=0))
                    writer.writerow([r+1,k+1,profile,v,a.rps,finished,duration,
                            finished*1e9/duration,peak,*sched,*lag,*wire]); log.flush()
            print('fixed-rate round',r+1,flush=True)
    finally:
        for proc,port in servers.values(): proc.terminate(); proc.wait(timeout=5)
