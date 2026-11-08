#!/usr/bin/env bash
# Local runtime pre-check with a random adapter of the real 1.5B shapes (no training): scale 0
# must reproduce the frozen phase-1 1.5B dev replies; scale 1 must change some.
set -euo pipefail
S=$1
cd /home/khoavd/WORKSPACE/thesis/eth-nl2sql
M=qwen2.5-coder-1.5b-instruct-q4_k_m
A=$S/gguf/random-1.5b.gguf
~/opt/llama.cpp/src-b12032/build/bin/llama-server -m $PWD/models/$M.gguf --lora-scaled "$A:0" \
  -c 8192 -ngl 99 -fa on -np 1 --cache-ram 0 --port 8080 > $S/precheck-server.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null; wait $PID 2>/dev/null || true' EXIT
for _ in $(seq 1 300); do curl -s -m 2 http://127.0.0.1:8080/health | grep -q '"ok"' && break; sleep 1; done
curl -s http://127.0.0.1:8080/lora-adapters; echo
uv run eth-nl2sql generate-sql --prompts data/prompts/dev.predictions.prompts.jsonl \
  --adapter "$A" --output $S/precheck.scale0.jsonl > $S/precheck.scale0.log 2>&1
uv run python - data/predictions/dev.predictions.prompts.$M.jsonl $S/precheck.scale0.jsonl <<'PY'
import json, sys
def replies(p):
    return {r["question_id"]: (r["decision"], [a.get("reply") for a in r["attempts"]])
            for r in map(json.loads, open(p))}
a, b = replies(sys.argv[1]), replies(sys.argv[2])
d = [q for q in a if a[q] != b.get(q)]
print(f"scale 0: {len(a) - len(d)}/{len(a)} identical to phase 1; differ: {d[:10]}")
PY
curl -s -X POST http://127.0.0.1:8080/lora-adapters -H 'Content-Type: application/json' \
  -d '[{"id": 0, "scale": 1.0}]'; echo
curl -s http://127.0.0.1:8080/lora-adapters; echo
uv run eth-nl2sql generate-sql --prompts data/prompts/dev.predictions.prompts.jsonl \
  --adapter "$A" --limit 5 --output $S/precheck.scale1.jsonl > $S/precheck.scale1.log 2>&1
uv run python - data/predictions/dev.predictions.prompts.$M.jsonl $S/precheck.scale1.jsonl <<'PY'
import json, sys
def replies(p):
    return {r["question_id"]: [a.get("reply") for a in r["attempts"]]
            for r in map(json.loads, open(p))}
a, b = replies(sys.argv[1]), replies(sys.argv[2])
d = [q for q in b if a[q] != b[q]]
print(f"scale 1: {len(d)}/{len(b)} questions reply differently from phase 1")
for q in d[:2]:
    print(q, repr((b[q][0] or "")[:160]))
PY
grep -o '"adapter": {[^}]*' $S/precheck.scale0.jsonl.meta.json | head -1
