"""Paired summaries; per-run RPS is successful responses / elapsed time."""
import argparse, csv, json
from collections import defaultdict
from statistics import median
from pathlib import Path

def summary(path):
    groups=defaultdict(lambda:defaultdict(dict))
    with Path(path).open() as f:
        for row in csv.DictReader(f):
            groups[row['scenario']][int(row['round'])][row['variant']]=row
    out={}
    for sc,pairs in groups.items():
        pairs=[v for v in pairs.values() if 'base' in v and 'candidate' in v]
        if not pairs:continue
        d={}
        for k in ['rps','cpu_us_req','p99_us']:
            a=[float(p['base'][k]) for p in pairs]
            b=[float(p['candidate'][k]) for p in pairs]
            dif=[100*(y/x-1) for x,y in zip(a,b)]
            d[k]={'base':median(a),'candidate':median(b),'paired_delta_pct':median(dif),
                  'min_pair_pct':min(dif),'max_pair_pct':max(dif),'positive_pairs':sum(z>0 for z in dif)}
        out[sc]={'pairs':len(pairs),'n':sum(int(r['n']) for p in pairs for r in p.values()),**d}
    return out

if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('path');cli.add_argument('--json',action='store_true');args=cli.parse_args()
    result=summary(args.path)
    if args.json: print(json.dumps(result,indent=2))
    else:
        for k,s in result.items():
            r=s['rps'];p=s['p99_us']
            print(f"{k:20s} n={s['pairs']} {r['base']:9.0f} {r['candidate']:9.0f} ({r['paired_delta_pct']:+.1f}%, pairs [{r['min_pair_pct']:.1f},{r['max_pair_pct']:.1f}], positive {r['positive_pairs']}) p99 {p['base']:.0f}/{p['candidate']:.0f}")
