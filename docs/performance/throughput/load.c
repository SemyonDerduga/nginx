#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define BINS 25002
#define MAX_THREADS 64
struct result { unsigned long long requests, hist[BINS]; };
static struct result results[MAX_THREADS];
static struct sockaddr_in dest;
static char request[8192];
static size_t request_size;
static unsigned long long deadline;
static pthread_barrier_t gate;
static unsigned long long now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return 1000000000ULL * t.tv_sec + t.tv_nsec;
}
static void fail(const char *s) { perror(s); exit(2); }
static void *run(void *arg) {
    struct result *res = arg;
    int fd=socket(AF_INET,SOCK_STREAM,0), one=1;
    struct timeval timeout = {5, 0};
    if(fd<0 || setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one))<0 ||
       setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout))<0 ||
       connect(fd,(struct sockaddr*)&dest,sizeof(dest))<0) fail("connect");
    pthread_barrier_wait(&gate);
    unsigned long long start;
    while((start=now_ns())<deadline){
        size_t off=0;
        while(off<request_size){
            ssize_t n=send(fd,request+off,request_size-off,MSG_NOSIGNAL);
            if(n<=0)fail("send");
            off+=(size_t)n;
        }
        char response[2048];
        size_t have=0, total=0;
        do {
            if(have>=sizeof(response))fail("response overflow");
            ssize_t n=recv(fd,response+have,sizeof(response)-have,0);
            if(n<=0)fail("recv");
            have+=(size_t)n;
            if(!total){
                char *end=memmem(response,have,"\r\n\r\n",4);
                if(end) total=(size_t)(end-response)+4+2;
            }
        } while(!total || have<total);
        if(have!=total || memcmp(response,"HTTP/1.1 200 ",13) ||
           memcmp(response+total-2,"OK",2) ||
           !memmem(response,total-2,"\r\nContent-Length: 2\r\n",21)) {
            fprintf(stderr,"Unexpected HTTP response\n");exit(3);
        }
        unsigned long long us=(now_ns()-start)/1000;
        res->hist[us<BINS-1?us:BINS-1]++;
        res->requests++;
    }
    close(fd); return NULL;
}
int main(int argc,char**argv){
    if(argc!=6){fprintf(stderr,"load PORT THREADS SECONDS HEADERFILE PATH\n");return 2;}
    int threads=atoi(argv[2]); if(threads<1 || threads>MAX_THREADS)return 2;
    FILE*f=fopen(argv[4],"rb"); if(!f)fail("headerfile");
    int n=snprintf(request,sizeof(request),"GET %s HTTP/1.1\r\nHost: localhost\r\n",argv[5]);
    size_t h=fread(request+n,1,sizeof(request)-n-2,f);
    if(!feof(f))fail("headerfile too large");
    fclose(f);request_size=n+h;memcpy(request+request_size,"\r\n",2);request_size+=2;
    dest.sin_family=AF_INET;dest.sin_port=htons(atoi(argv[1]));
    inet_pton(AF_INET,"127.0.0.1",&dest.sin_addr);
    pthread_t ids[MAX_THREADS];pthread_barrier_init(&gate,NULL,threads+1);
    for(int i=0;i<threads;i++)if(pthread_create(&ids[i],NULL,run,&results[i]))fail("pthread_create");
    unsigned long long start=now_ns();deadline=start+(unsigned long long)(atof(argv[3])*1e9);
    pthread_barrier_wait(&gate);
    for(int i=0;i<threads;i++)pthread_join(ids[i],NULL);
    double sec=(now_ns()-start)/1e9;
    unsigned long long all=0,hist[BINS]={0};for(int i=0;i<threads;i++){
        all+=results[i].requests;for(int j=0;j<BINS;j++)hist[j]+=results[i].hist[j];
    }
    if(!all)return 4;
    int p99=0;unsigned long long sum=0;
    for(int j=0;j<BINS;j++){sum+=hist[j];if(sum>=all-all/100){p99=j;break;}}
    printf("%llu,%.8f,%.3f,%d\n",all,sec,all/sec,p99);
    return 0;
}
