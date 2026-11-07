#!/usr/bin/env python3
"""Differentially check the real NGINX random lookup against the proposed patch."""
import argparse
import subprocess
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path, help="NGINX 1.31.8 source directory")
parser.add_argument("candidate", type=Path, help="NGINX source directory with both patches")
args = parser.parse_args()
path = Path("src/http/modules/ngx_http_upstream_random_module.c")
src = (args.source / path).read_text()

def extract(text):
    start = text.index('static ngx_uint_t\nngx_http_upstream_peek_random_peer(')
    end = text.index('\n}\n', start) + 3
    return text[start:end].replace('        k = (i + j) / 2;', '        comparisons++;\n        k = (i + j) / 2;')

ref = extract(src)
candidate = extract((args.candidate / path).read_text()).replace("ngx_http_upstream_peek_random_peer", "proposed")
prefix=r'''
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
typedef uintptr_t ngx_uint_t;
typedef struct {ngx_uint_t number,total_weight;} ngx_http_upstream_rr_peers_t;
typedef struct {ngx_uint_t range;} range_t;
typedef struct {range_t ranges[1024]; int unit_weights;} conf_t;
typedef struct {conf_t *conf;} ngx_http_upstream_random_peer_data_t;
static ngx_uint_t random_input;
static uint64_t comparisons, outputs;
#define ngx_random() (random_input)
'''
main=r'''
static void check(unsigned n,unsigned mode,int exhaustive) {
    conf_t conf = {.unit_weights=1}; ngx_http_upstream_rr_peers_t peers={n,0};
    ngx_http_upstream_random_peer_data_t rp={&conf};
    for (unsigned j=0; j<n; j++){
        unsigned w=exhaustive ? 1+mode%3 : 1;
        if(exhaustive)mode/=3;
        if(w!=1)conf.unit_weights=0;
        conf.ranges[j].range=peers.total_weight;
        peers.total_weight+=w;
    }
    uint64_t orig=0,prop=0;
    for(random_input=0;random_input<peers.total_weight;random_input++) {
        comparisons=0;
        ngx_uint_t a=ngx_http_upstream_peek_random_peer(&peers,&rp);
        orig+=comparisons;
        comparisons=0;
        ngx_uint_t b=proposed(&peers,&rp);
        prop+=comparisons;
        assert(a==b);outputs++;
    }
    if(!exhaustive)printf("unit weights n=%u mean comparisons original=%.1f candidate=%.1f\n",n,(double)orig/n,(double)prop/n);
}
int main(void){
    unsigned configs=0;
    for(unsigned n=1,p=3;n<=8;n++,p*=3)
        for(unsigned mode=0;mode<p;mode++){check(n,mode,1);configs++;}
    printf("weighted patterns=%u identical picks=%llu\n",configs,(unsigned long long)outputs);
    check(8,0,0);check(32,0,0);check(256,0,0);return 0;
}
'''
with tempfile.TemporaryDirectory() as temp:
    tmp = Path(temp)
    (tmp/'random_probe.c').write_text(prefix+ref+candidate+main)
    subprocess.run(['cc','-std=c11','-O2','-Wall','-Wextra','-Werror','random_probe.c','-o','random_probe'],cwd=tmp,check=True)
    subprocess.run([tmp/'random_probe'],check=True)
