#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo "Usage: bash check.sh /path/to/nginx-tests /path/to/nginx" >&2
  exit 2
fi
suite=$(cd "$1" && pwd)
binary=$(realpath "$2")
test -x "$binary"
rev=16964ee752509eb6271c4e8760e7899274240755
actual=$(git -C "$suite" rev-parse HEAD)
if [[ "$actual" != "$rev" ]]; then
  echo "Expected nginx-tests $rev; found $actual" >&2
  exit 2
fi
cd "$suite"
export TEST_NGINX_BINARY="$binary"
# Exercise both optional large-buffer boundary cases on local, disposable servers.
export TEST_NGINX_UNSAFE=1
exec prove -v -j1 http_header_buffers.t http_headers_multi.t http_host.t \
  http_max_headers.t ignore_invalid_headers.t proxy.t proxy_connection_headers.t \
  proxy_duplicate_headers.t proxy_merge_headers.t proxy_keepalive.t \
  upstream_random.t upstream_keepalive.t
