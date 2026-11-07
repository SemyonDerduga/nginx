#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L
#ifdef __APPLE__
#define _DARWIN_C_SOURCE
#endif
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>
#include <stdint.h>
#include <inttypes.h>
#include <stddef.h>
#include <sys/mman.h>
#include <unistd.h>
#if defined(__SSE2__) && (defined(__GNUC__) || defined(__clang__))
#include <emmintrin.h>
#endif
#ifdef USE_NGINX_HEADERS
#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>
#else

typedef unsigned char u_char;
typedef long ngx_int_t;
typedef unsigned long ngx_uint_t;
#define ngx_inline inline
#define ngx_hash(key,c) ((key)*31+(c))
#define ngx_strncmp(a,b,n) strncmp((const char *)(a),(const char *)(b),(n))
#define CR '\r'
#define LF '\n'
#define NGX_HTTP_LC_HEADER_LEN 32
#define NGX_AGAIN -2
#define NGX_OK 0
#define NGX_HTTP_PARSE_HEADER_DONE 1
#define NGX_HTTP_PARSE_INVALID_HEADER 14
typedef struct {
    unsigned state, invalid_header;
    ngx_uint_t header_hash, lowcase_index;
    u_char lowcase_header[NGX_HTTP_LC_HEADER_LEN];
    u_char *header_name_start, *header_name_end, *header_start, *header_end, *upstream;
} ngx_http_request_t;
typedef struct { u_char *pos, *last; } ngx_buf_t;
#endif
#define ngx_http_parse_header_line __attribute__((noinline)) original
#include "reference.inc"
#undef ngx_http_parse_header_line
#define ngx_http_parse_header_line __attribute__((noinline)) candidate
#include "candidate.inc"

static uint32_t rng = 1;
static uint32_t rnd(void) {
    rng ^= rng << 13; rng ^= rng >> 17; return rng ^= rng << 5;
}
static size_t comparisons;
static int equal_fields(const ngx_http_request_t *a, const ngx_http_request_t *b) {
    return a->state == b->state && a->invalid_header == b->invalid_header
        && a->header_hash == b->header_hash && a->lowcase_index == b->lowcase_index
        && !memcmp(a->lowcase_header, b->lowcase_header, NGX_HTTP_LC_HEADER_LEN)
        && a->header_name_start == b->header_name_start && a->header_name_end == b->header_name_end
        && a->header_start == b->header_start && a->header_end == b->header_end;
}
static void compare(u_char *s, size_t n, int chunking, int flags) {
    ngx_http_request_t x = {0}, y = {0};
    ngx_buf_t a = {0}, b = {0};
    a.pos = a.last = b.pos = b.last = s;
    x.upstream = y.upstream = (flags & 2) ? (void *)s : NULL;
    while (a.last < s + n) {
        size_t rem = (size_t)(s + n - a.last);
        size_t take = chunking ? (size_t)chunking : rem;
        if (take > rem) take = rem;
        a.last += take; b.last += take;
        do {
            ngx_int_t rx = original(&x,&a,flags&1), ry = candidate(&y,&b,flags&1);
            comparisons++;
            if (rx != ry || a.pos != b.pos || !equal_fields(&x,&y)) {
                fprintf(stderr,"mismatch n=%zu chunk=%d flags=%d\n",n,chunking,flags);
                exit(1);
            }
            if (rx == NGX_HTTP_PARSE_INVALID_HEADER || rx == NGX_HTTP_PARSE_HEADER_DONE) return;
            if (rx == NGX_AGAIN) break;
        } while (a.pos < a.last);
    }
}
typedef ngx_int_t (*parser)(ngx_http_request_t *, ngx_buf_t *, ngx_uint_t);
static unsigned long long ns(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec*1000000000ULL + t.tv_nsec; }
static volatile ngx_uint_t sink;
static double measure(parser f, u_char *s, size_t n, int iterations) {
    ngx_uint_t check = 0;
    ngx_buf_t b = {0};
    ngx_http_request_t r = {0};
    unsigned long long t = ns();
    for (int i = 0; i < iterations; ++i) {
        b.pos = s; b.last = s+n;
        do {
            ngx_int_t status = f(&r,&b,0);
            if (status != NGX_OK) abort();
            check += r.header_hash + (ngx_uint_t)(r.header_end - r.header_start);
        } while (b.pos < b.last);
    }
    sink = check;
    return (ns()-t) / (double)iterations;
}
static void bench(const char *name, u_char *s, size_t n, int k) {
    for (int i=0; i<10; i++) {
        double a,b;
        if (i & 1) { b=measure(candidate,s,n,k); a=measure(original,s,n,k); }
        else { a=measure(original,s,n,k); b=measure(candidate,s,n,k); }
        printf("%s,%d,%.2f,%.2f\n",name,i,a,b);
    }
}
int main(int argc, char **argv) {
    u_char buf[1024+32];
    if (argc>1 && strcmp(argv[1],"bench")==0) {
        char *mix = "Host: www.example.org\r\nAccept: */*\r\nAccept-Encoding: gzip, br\r\nConnection: keep-alive\r\nUser-Agent: curl/8.0\r\n";
        char *short1 = "Host: a.io\r\n";
        bench("host",(u_char*)short1,strlen(short1),120000);
        bench("mixed",(u_char*)mix,strlen(mix),30000);
        memcpy(buf,"Cookie: ",8);
        for (int i=0;i<512;++i) buf[8+i]=(u_char)"x=0123456789abcdef"[i%18];
        memcpy(buf+520,"\r\n",2); bench("cookie", buf,522,20000);
        return 0;
    }
    size_t cases = 0;
    for (int off=0;off<16;off++) for (int pos=0;pos<40;pos++) for (int byte=0;byte<256;byte++) {
        u_char *s=buf+off;
        memcpy(s,"X-a: ",5); memset(s+5,'A',52); s[5+pos]=byte;
        memcpy(s+57," \r\nY_y: x\n\r\n",12);
        compare(s,69,pos%22,byte%4); cases++;
    }
    for (int i=0;i<20000;i++) {
        u_char *s=buf+(rnd()%16);
        size_t len=16+rnd()%990;
        memcpy(s, "X-test: ",8);
        for(size_t j=8;j<len-2;j++) s[j]=(rnd()%5)?('!'+rnd()%93):(rnd() & 255);
        s[len-2]='\r'; s[len-1]='\n';
        int chunk = rnd()%72;
        int flags = rnd()%4;
        compare(s,len,chunk,flags); cases++;
    }
    long page = sysconf(_SC_PAGESIZE);
    if (page < 1024) abort();
    u_char *mapping = mmap(NULL, (size_t)page * 2, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (mapping == MAP_FAILED || mprotect(mapping+page, (size_t)page, PROT_NONE)) abort();
    size_t guarded = 0;
    for (int len = 5; len < 150; len++) {
        u_char *s = mapping+page-len;
        for (int chunk = 0; chunk <= 33; chunk++) for (int term = 0; term < 6; term++) {
            memcpy(s,"X: a",4); memset(s+4,'z',len-4);
            s[len-1] = ((const u_char *) "z \r\n\0\t")[term];
            compare(s,(size_t)len,chunk,0);
            guarded++;
        }
    }
    if (munmap(mapping, (size_t)page*2)) abort();
    printf("%zu guard-page cases, ",guarded);
    printf("%zu other cases; %zu after-call state comparisons\n",cases,comparisons);
}
