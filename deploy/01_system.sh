#!/usr/bin/env bash
# Step 1: system packages for TBDub, through domestic mirrors.
#
# Two platform quirks are fixed here, both of them bit us on the 4090 pod:
#   1. DNS hands out IPv6 addresses that hang on connect -> prefer IPv4 via /etc/gai.conf
#   2. the cluster sets http_proxy globally, which makes domestic mirrors crawl
#      -> aliyun apt mirror + explicit DIRECT for that host
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "--- 1/3 prefer IPv4 (fixes hung pip/curl/apt connections) ---"
[ -f /etc/gai.conf ] && cp -n /etc/gai.conf /etc/gai.conf.orig || true
cp "$HERE/gai.conf" /etc/gai.conf
getent ahosts pypi.tuna.tsinghua.edu.cn | head -1

echo "--- 2/3 apt sources -> mirrors.aliyun.com (direct, no proxy) ---"
cp -n /etc/apt/sources.list /etc/apt/sources.list.orig || true
cp "$HERE/sources.list" /etc/apt/sources.list
cp "$HERE/99domestic.conf" /etc/apt/apt.conf.d/99domestic

echo "--- 3/3 install packages ---"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git ffmpeg build-essential libgl1 libglib2.0-0 libsm6 libxext6 libxrender1
ffmpeg -version | head -1
which ffprobe
echo "### SYSTEM_DONE"
