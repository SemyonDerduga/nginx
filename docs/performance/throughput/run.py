"""Compare two NGINX binaries on a synthetic HTTP/1.1 workload (Linux x86-64)."""
import argparse, csv, os, socket, subprocess, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("base",type=Path)
ap.add_argument("candidate",type=Path)
ap.add_argument("--seconds",type=float,default=1.5)
ap.add_argument("--rounds",type=int,default=8)
ap.add_argument("--output",type=Path,default=HERE/"results"/"e2e.csv")
ap.add_argument("--ctrl",action="store_true",help="use a second unmodified binary as candidate")
args=ap.parse_args()
cores=sorted(os.sched_getaffinity(0))
if len(cores)<3: raise SystemExit("The test requires at least 3 available logical CPUs")
def pin(ids):
    def set_mask(): os.sched_setaffinity(0,ids)
    return set_mask
def counters(pid):
    f=Path(f"/proc/{pid}/stat").read_text().rsplit(")",1)[1].split()
    return int(f[11])+int(f[12])
def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1",0)); return s.getsockname()[1]
def launch(binary,location,run,core,http_extra=""):
    run.mkdir(); (run/"logs").mkdir(); p=port()
    config=run/"nginx.conf"
    config.write_text(f"""daemon off; master_process off; error_log stderr error;
pid nginx.pid; events {{worker_connections 2048;}} http {{access_log off;
keepalive_requests 10000000; keepalive_timeout 60s; {http_extra}
server {{listen 127.0.0.1:{p}; {location} }} }}""")
    proc=subprocess.Popen([str(binary.resolve()),"-p",str(run),"-c",str(config)],
                          stderr=subprocess.PIPE,preexec_fn=pin({core}))
    for _ in range(100):
        if proc.poll() is not None: raise RuntimeError(proc.stderr.read().decode())
        try:
            with socket.create_connection(("127.0.0.1",p),timeout=.1): pass
            return proc,p
        except OSError: time.sleep(.02)
    raise RuntimeError("NGINX readiness timeout")
def run_load(server, header, path, seconds):
    proc,p=server
    before=counters(proc.pid)
    raw=subprocess.check_output([str(HERE/"load"),str(p),str(min(12,2*(len(cores)-2))),
                      str(seconds),str(header),path],preexec_fn=pin(set(cores[1:-1])),text=True)
    ticks=counters(proc.pid)-before
    requests,dur,rps,p99=raw.strip().split(",")
    cpu=ticks/os.sysconf("SC_CLK_TCK")
    return int(requests),float(dur),float(rps),int(p99),cpu,cpu*1e6/int(requests)
args.output.parent.mkdir(exist_ok=True,parents=True)
with tempfile.TemporaryDirectory(prefix="ngx-bench-") as td:
    tmp=Path(td)
    headers={"short":b"","typical":b"User-Agent: Mozilla/5.0 (X11; Linux x86_64) TestBrowser/128.0\r\nAccept: text/html,application/xhtml+xml,application/json;q=0.9\r\nAccept-Encoding: gzip, deflate, br\r\nCookie: session="+b"a"*80+b"; pref=en\r\n","cookie2048":b"Cookie: "+b"A"*2048+b"\r\n"}
    for h,v in headers.items(): (tmp/h).write_bytes(v)
    servers=[]
    try:
        origin=launch(args.base,'location / {return 200 "OK";}',tmp/"origin",cores[-1]); servers.append(origin)
        loc=f'location /direct {{ return 200 "OK"; }} location /proxy {{proxy_http_version 1.1; proxy_set_header Connection ""; proxy_pass http://study_origin;}}'
        for name,binary in [("base",args.base),("candidate",args.base if args.ctrl else args.candidate)]:
            servers.append(launch(binary,loc,tmp/name,cores[0],f"upstream study_origin {{server 127.0.0.1:{origin[1]}; keepalive 64; keepalive_requests 10000000;}}"))
        csvfile=args.output.open('w',newline='')
        with csvfile:
            w=csv.writer(csvfile)
            w.writerow(["round","order","scenario","variant","n","sec","rps","p99_us","cpu_s","cpu_us_req"])
            scenarios=[("short","/direct"),("typical","/direct"),("cookie2048","/direct"),("typical","/proxy"),("cookie2048","/proxy")]
            for header,path in scenarios:
                for server in servers[1:]: run_load(server,tmp/header,path,.3)
            for i in range(args.rounds):
                ordered=scenarios[i%len(scenarios):]+scenarios[:i%len(scenarios)]
                for j,(header,path) in enumerate(ordered):
                    for k,idx in enumerate(([1,2],[2,1])[(i+scenarios.index((header,path)))%2]):
                        metrics=run_load(servers[idx],tmp/header,path,args.seconds)
                        w.writerow([i+1,k+1,header+path,("base","candidate")[idx-1],*metrics]); csvfile.flush()
                print("round",i+1,flush=True)
    finally:
        for proc,_ in reversed(servers):
            proc.terminate(); proc.wait(timeout=5)
