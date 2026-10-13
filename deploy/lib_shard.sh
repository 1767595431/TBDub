#!/usr/bin/env bash
# Resumable sharded downloader shared by 03_weights_v11.sh and 04_weights_v10.sh.
#
# Why not plain curl: single-connection throughput to these mirrors is 300KB/s-4MB/s,
# while 8 parallel ranges reach 3.4-19MB/s. But CDNs sometimes ignore a Range header and
# return the whole object, so every shard is size-checked and truncated before assembly,
# and each shard resumes from however many bytes it already has.
#
#   shard_download <url> <out> <size> [conns]   # keeps *.partNN on failure for resume
#   shard_retry    <url> <out> <size> [conns]   # retries shard_download n times

_shard_part() {  # <url> <start> <end> <want> <out>
  local url="$1" start="$2" end="$3" want="$4" out="$5"
  local tmp="${out}.tmp" attempt have got
  # seed from an existing partial shard so older runs still resume
  if [ ! -f "$tmp" ] && [ -f "$out" ] && [ "$(stat -c%s "$out")" -lt "$want" ]; then
    cp -f "$out" "$tmp"
  fi
  for attempt in 1 2 3 4 5 6; do
    have=0
    [ -f "$tmp" ] && have=$(stat -c%s "$tmp")
    if [ "$have" -gt "$want" ]; then have=0; rm -f "$tmp"; fi
    if [ "$have" -lt "$want" ]; then
      curl -sL --retry 5 --retry-delay 3 --retry-all-errors \
        --max-filesize "$((want - have))" \
        -r "$((start + have))-$end" "$url" >> "$tmp" || true
    fi
    got=0
    [ -f "$tmp" ] && got=$(stat -c%s "$tmp")
    if [ "$got" -ge "$want" ]; then
      truncate -s "$want" "$tmp"
      mv -f "$tmp" "$out"
      return 0
    fi
    echo "    [part retry $attempt] $(basename "$out") ${got}/${want}"
    sleep 2
  done
  echo "    [part FAIL] $(basename "$out")" >&2
  return 1
}

shard_download() {  # <url> <out> <size> [conns]
  local url="$1" out="$2" size="$3" conns="${4:-8}"
  if [ -f "$out" ] && [ "$(stat -c%s "$out")" = "$size" ]; then
    echo "[skip] $out already complete"; return 0
  fi
  rm -f "$out"
  local chunk=$(( (size + conns - 1) / conns ))
  echo "[get ] $out  $size bytes  x$conns  $(date -u +%H:%M:%S)"
  local i start end want part pids=()
  for i in $(seq 0 $((conns - 1))); do
    start=$((i * chunk)); end=$((start + chunk - 1)); [ "$end" -ge "$size" ] && end=$((size - 1))
    want=$((end - start + 1))
    part=$(printf '%s.part%02d' "$out" "$i")
    if [ -f "$part" ] && [ "$(stat -c%s "$part")" = "$want" ]; then continue; fi
    _shard_part "$url" "$start" "$end" "$want" "$part" &
    pids+=($!)
  done

  local rc=0 p
  for p in "${pids[@]:-}"; do wait "$p" || rc=1; done
  [ "$rc" -eq 0 ] || { echo "[FAIL] $out incomplete shards (kept for resume)" >&2; return 1; }

  local names=""
  for i in $(seq 0 $((conns - 1))); do names="$names $(printf '%s.part%02d' "$out" "$i")"; done
  cat $names > "$out"
  local got; got=$(stat -c%s "$out")
  if [ "$got" != "$size" ]; then
    echo "[FAIL] $out got $got want $size" >&2
    return 1
  fi
  rm -f $names
  echo "[ ok ] $out  $(date -u +%H:%M:%S)"
}

shard_retry() {  # <url> <out> <size> [conns]
  local n=0
  until shard_download "$@"; do
    n=$((n + 1))
    if [ "$n" -ge 10 ]; then echo "[FAIL] giving up on $2" >&2; return 1; fi
    echo "[retry $n] $2"
    sleep 10
  done
}

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

shard_retry_verified() {  # <url> <out> <size> <sha256> [conns]
  # ALWAYS verify the digest: a CDN that ignores the Range header can hand back bytes from
  # the wrong offset, which yields a file of exactly the right SIZE and completely wrong
  # CONTENT. Size checks alone are not enough.
  local url="$1" out="$2" size="$3" want="$4" conns="${5:-8}" n=0
  while :; do
    if [ -f "$out" ] && [ "$(stat -c%s "$out")" = "$size" ] && [ "$(sha256_of "$out")" = "$want" ]; then
      echo "[skip] $out size+sha256 verified"; return 0
    fi
    if shard_download "$url" "$out" "$size" "$conns"; then
      local got; got=$(sha256_of "$out")
      if [ "$got" = "$want" ]; then echo "[ ok ] $out sha256 verified"; return 0; fi
      echo "[FAIL] $out sha256 mismatch" >&2
      echo "       got  $got" >&2
      echo "       want $want" >&2
      rm -f "$out"
    fi
    n=$((n + 1))
    if [ "$n" -ge 6 ]; then echo "[FAIL] giving up on $out" >&2; return 1; fi
    echo "[retry $n] $out"
    sleep 5
  done
}
