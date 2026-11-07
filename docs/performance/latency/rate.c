/* Constant offered rate over a fixed pool of keepalive connections. Each
 * request has its own planned send time, even when earlier requests stall. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <netinet/tcp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

enum { CONNS = 32, RESP_CAP = 1024 };
struct conn {
    int fd, request_id;
    size_t have;
    char response[RESP_CAP];
};
struct sample { uint64_t due, sent, completed; };

static uint64_t now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static void fail(const char *s) { perror(s); exit(2); }

int main(int argc, char **argv) {
    if (argc != 6) {
        fputs("rate PORT RPS REQUESTS HEADER_FILE BINARY_SAMPLE_OUTPUT\n", stderr);
        return 1;
    }
    int port = atoi(argv[1]), rps = atoi(argv[2]), n = atoi(argv[3]);
    if (port < 1 || port > 65535 || rps < 1 || n < 1) return 1;
    char req[8192] = "GET /direct HTTP/1.1\r\nHost: localhost\r\n";
    FILE *h = fopen(argv[4], "rb");
    if (!h) fail("header file");
    size_t req_size = strlen(req);
    req_size += fread(req+req_size, 1, sizeof(req)-req_size-2, h);
    if (!feof(h)) fail("header too large");
    fclose(h);
    memcpy(req+req_size, "\r\n", 2); req_size += 2;

    int ep = epoll_create1(EPOLL_CLOEXEC);
    if (ep < 0) fail("epoll_create");
    struct conn c[CONNS] = {0};
    int idle[CONNS], head = 0, idle_count = CONNS;
    struct sockaddr_in dest = {.sin_family = AF_INET, .sin_port = htons(port)};
    inet_pton(AF_INET, "127.0.0.1", &dest.sin_addr);
    for (int i = 0; i < CONNS; ++i) {
        int one = 1;
        c[i].fd = socket(AF_INET, SOCK_STREAM, 0);
        c[i].request_id = -1;
        idle[i] = i;
        if (c[i].fd < 0 || setsockopt(c[i].fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one))
            || connect(c[i].fd,(struct sockaddr *)&dest,sizeof(dest))) fail("connect");
        struct epoll_event event = {.events=EPOLLIN, .data.u32=(uint32_t)i};
        if (epoll_ctl(ep, EPOLL_CTL_ADD, c[i].fd, &event)) fail("epoll_ctl");
    }
    struct sample *s = calloc((size_t)n, sizeof(*s));
    if (!s) fail("calloc");
    uint64_t start = now_ns();
    int submitted = 0, completed = 0, peak = 0;
    int timed_out = 0;
    while (completed < n) {
        uint64_t now = now_ns();
        if (now-start > (uint64_t)n*1000000000/rps+3000000000ULL) { timed_out = 1; break; }
        uint64_t due = start + (uint64_t)submitted*1000000000/rps;
        if (submitted < n && now >= due && idle_count > 0) {
            int idx = idle[head]; head = (head+1)%CONNS; idle_count--;
            c[idx].request_id = submitted; c[idx].have = 0;
            s[submitted].due = due;
            s[submitted].sent = now_ns();
            ssize_t wrote = send(c[idx].fd, req, req_size, MSG_NOSIGNAL | MSG_DONTWAIT);
            if (wrote != (ssize_t)req_size) fail("short send");
            submitted++;
            if (submitted-completed > peak) peak = submitted-completed;
        }

        struct epoll_event ready[CONNS];
        int count = epoll_wait(ep, ready, CONNS, 0);
        if (count < 0) { if (errno == EINTR) continue; fail("epoll_wait"); }
        for (int k = 0; k < count; k++) {
            int i = (int)ready[k].data.u32;
            if (c[i].request_id < 0 || c[i].have == RESP_CAP) fail("unexpected response");
            ssize_t bytes = recv(c[i].fd, c[i].response + c[i].have, RESP_CAP - c[i].have, MSG_DONTWAIT);
            if (bytes < 0 && (errno == EWOULDBLOCK || errno == EINTR)) continue;
            if (bytes <= 0) fail("recv");
            uint64_t end = now_ns();
            c[i].have += (size_t)bytes;
            char *split = memmem(c[i].response, c[i].have, "\r\n\r\n", 4);
            if (!split || c[i].have < (size_t)(split-c[i].response)+6) continue;
            if (c[i].have != (size_t)(split - c[i].response)+6 ||
                memcmp(c[i].response,"HTTP/1.1 200 ",13) || memcmp(split+4,"OK",2) ||
                !memmem(c[i].response,c[i].have-2,"\r\nContent-Length: 2\r\n",21)) {
                fputs("unexpected status, headers, or response body\n",stderr); return 3;
            }
            s[c[i].request_id].completed = end;
            c[i].request_id = -1;
            idle[(head+idle_count)%CONNS] = i; idle_count++;
            completed++;
        }
    }
    uint64_t finish = now_ns();
    if (timed_out || submitted != n || completed != n) { fputs("timed out\n",stderr); return 4; }
    for (int i=0;i<CONNS;i++) close(c[i].fd);
    close(ep);
    FILE *out = fopen(argv[5],"wb");
    if (!out || fwrite(s,sizeof(*s),n,out)!=(size_t)n || fclose(out)) fail("write samples");
    printf("%d,%" PRIu64 ",%d\n", completed,finish-start,peak);
    free(s);
}
