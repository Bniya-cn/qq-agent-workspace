#!/bin/bash
# usage: dl_resume.sh URL DEST EXPECTED_SIZE
URL=$1; DST=$2; EXP=$3
for i in $(seq 1 200); do
  cur=$(stat -f %z "$DST" 2>/dev/null || echo 0)
  if [ "$cur" -ge "$EXP" ]; then echo "OK $DST $cur"; exit 0; fi
  curl -sSL --speed-limit 2000 --speed-time 30 -C - -o "$DST" "$URL" || echo "retry $i $DST at $cur"
  sleep 2
done
echo "FAIL $DST"; exit 1
