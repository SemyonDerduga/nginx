"""Replay the Linux HTTP/1.1 throughput comparisons from the November audit."""
import argparse,csv,json,os,socket,subprocess,tempfile,time
from pathlib import Path
ap=argparse.ArgumentParser()
mode=ap.add_mutually_exclusive_group()
mode.add_argument('--buffer-check',action='store_true')
mode.add_argument('--confirm',action='store_true')
ap.add_argument('--sources',type=Path,default=Path('review-src'))
ap.add_argument('--output',type=Path,required=True)
ap.add_argument('--client',type=Path,default=Path(__file__).resolve().parents[1]/'throughput/load')
ap.add_argument('--rounds',type=int,default=6)
ap.add_argument('--seconds',type=float,default=1.25)
args=ap.parse_args()
root=args.sources.resolve()
client=args.client.resolve()
if args.output.exists(): ap.error("choose a new --output path")
if args.rounds < 1 or args.seconds <= 0: ap.error("rounds and seconds must be positive")
args.output.parent.mkdir(parents=True,exist_ok=True)
cores=sorted(os.sched_getaffinity(0))
if len(cores)<4:raise SystemExit('Set affinity to at least four vCPUs')
clientcp=set(cores[1:min(5,len(cores)-1)])
spec=json.loads((root/'head/docs/performance/arm64/spec-universal.json').read_text())
def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]
def spawn(b,d,p,body,core):
    (d/'logs').mkdir(parents=True)
    cfg=d/'nginx.conf'
    cfg.write_text('daemon off; master_process off; error_log stderr error; pid nginx.pid; events {worker_connections 2048;} http {keepalive_requests 10000000; keepalive_timeout 60s; '+body.replace('PORT',str(p))+'}')
    proc=subprocess.Popen([root/b/'objs/nginx','-p',str(d),'-c',str(cfg)],preexec_fn=lambda:os.sched_setaffinity(0,{core}))
    for _ in range(300):
        if proc.poll() is not None:raise RuntimeError(f'{b} failed to start')
        try:
            with socket.create_connection(('127.0.0.1',p),timeout=.1):return proc
        except OSError:time.sleep(.01)
    raise RuntimeError('readiness timeout')
def ticks(p):
    st=Path(f'/proc/{p.pid}/stat').read_text().rsplit(')',1)[-1].split()
    return sum(map(int,st[11:13]))
builds={'upstream-O1':'base-default','upstream-O2':'base','old-O2':'old','head-O2':'head','head-O1':'head-O1','head-AA':'head'}
if args.confirm:
    builds={'old-O2':'old', 'head-O2':'head'}
if args.buffer_check:
    builds={'unbuffered':'head','buffered':'head'}
with tempfile.TemporaryDirectory() as temp:
    tmp=Path(temp); op=port(); processes=[]
    try:
        processes.append(spawn('base',tmp/'origin',op,'access_log off;server {listen 127.0.0.1:PORT;location / {return 200 "OK";}}',cores[-1]))
        mapping={}
        body=spec['variants'][0]['http']+f' upstream origin {{ server 127.0.0.1:{op}; keepalive 64; }} '+''' server {listen 127.0.0.1:PORT; location /api/ {proxy_pass http://origin; proxy_http_version 1.1; proxy_set_header Connection ""; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;} location /direct {return 200 "OK";} location /cookie {access_log off;return 200 "OK";} location /cookieproxy {access_log off;proxy_pass http://origin;proxy_http_version 1.1;proxy_set_header Connection "";} }'''
        for i,(label,b) in enumerate(builds.items()):
            p=port()
            opts=body.replace('buffer=64k flush=1s;', ';') if label=='unbuffered' else body
            pr=spawn(b,tmp/label,p,opts,cores[0]);processes.append(pr);mapping[label]=(pr,p,tmp/label/'logs/a.log')
        proxy,long,short=spec['scenarios']
        cases=[('host',short['path'],[]),('proxy-browser',proxy['path'],proxy['headers']),('long-query-jwt',long['path'],long['headers']),('query-only',long['path'],[]),('cookie-direct','/cookie',['Cookie: '+'A'*2048]),('cookie-proxy','/cookieproxy',['Cookie: '+'A'*2048])]
        if args.confirm:
            cases=[cases[1],cases[2],cases[3]]
        if args.buffer_check:
            cases=[cases[0],cases[1],cases[2]]
        for key,path,h in cases:
            (tmp/key).write_bytes(b''.join((v+'\r\n').encode('ascii') for v in h))
        def measure(sc,label,sec):
            key,path,h=sc; pr,p,log=mapping[label]
            t0=ticks(pr)
            out=subprocess.check_output([client,str(p),'12',str(sec),tmp/key,path],preexec_fn=lambda:os.sched_setaffinity(0,clientcp),text=True)
            used=(ticks(pr)-t0)/os.sysconf('SC_CLK_TCK')
            n,elapsed,rps,p99=out.strip().split(',')
            if log.exists():os.truncate(log,0)
            return [int(n),int(p99),float(rps),used,used*1e6/int(n),float(elapsed)]
        for sc in cases:
            for label in builds:measure(sc,label,.16)
        with args.output.open('x',newline='') as f:
            w=csv.writer(f);w.writerow(['round','order','case','variant','n','closed_p99_us','rps','cpu_s','cpu_us_per_req','elapsed'])
            for r in range(args.rounds):
                for idx in range(len(cases)):
                    j=(idx+r)%len(cases);sc=cases[j]
                    # First-order balanced Williams design; each build occupies every slot.
                    seq=[0,1] if len(builds)==2 else [0,1,5,2,4,3]
                    for k,i in enumerate(seq):
                        label=list(builds)[(i+r+j)%len(builds)]
                        w.writerow([r+1,k+1,sc[0],label,*measure(sc,label,args.seconds)]);f.flush()
                print('finished',r+1,flush=True)
    finally:
        for p in reversed(processes):p.terminate();p.wait()
