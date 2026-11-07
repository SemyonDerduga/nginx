"""Report run medians and paired differences of run quantiles; unit is microseconds."""
import csv
from pathlib import Path
import statistics as st
import sys
path = Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).parent/'results/fixed.csv'
with path.open() as f: rows = list(csv.DictReader(f))
for profile in sorted({r['scenario'] for r in rows}):
    p=[r for r in rows if r['scenario']==profile]
    variants = {v:{int(r['round']):r for r in p if r['variant']==v} for v in ['base','candidate']}
    ids = sorted(variants['base'].keys() & variants['candidate'].keys())
    print(profile, len(ids), 'pairs')
    for metric in ('sched_p50_us','sched_p95_us','sched_p99_us','lag_p99_us','wire_p99_us','completed_rps','peak_inflight'):
        base,alt = [[float(variants[v][i][metric]) for i in ids] for v in ['base','candidate']]
        delta = [b-a for a,b in zip(base,alt)]
        print(metric, 'medians:', round(st.median(base),3),round(st.median(alt),3),
              'medianΔ:',round(st.median(delta),3),'pairedΔ range:',
              [round(min(delta),3),round(max(delta),3)],'improved:',sum(x<0 for x in delta))
