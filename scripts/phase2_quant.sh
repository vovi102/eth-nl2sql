#!/usr/bin/env bash
# S7 of docs/phase2-plan.md §7: the quantization ablation of §5.4, on dev, with 3B only,
# unattended on the local GTX 1650. Test is never read.
#
#   bash scripts/phase2_quant.sh            # after the fp16 kernel output is downloaded
#   bash scripts/phase2_quant.sh --check    # only the preconditions (no GPU)
#
# Input: the output of the fp16 kernel (`train_lora.py generate`), downloaded with
#   kaggle kernels output aohkvd/eth-nl2sql-phase2-fp16 -p models/lora/3b-fp16
# i.e. models/lora/3b-fp16/{generate.json, replies.<model>-fp16[.3b-e2].jsonl(.meta.json)}.
# Four runs of dev, one attempt per question and no placeholder check (§5.4):
#   Q4_K_M base and Q4_K_M + 3b-e2: llama-server, the flags of phase 1;
#   fp16 base and fp16 + 3b-e2: the Kaggle replies played back (generate-sql --replies).
# All four go through the same B8 and scoring (evaluate-sql --train-manifest). Before the fp16
# output is there, the script runs the two Q4_K_M runs only and stops.
# A step whose output is complete is skipped, so running the script again resumes.
#
# Log: data/predictions/dev-phase2-quant.log; summary: data/predictions/dev-phase2-quant.summary.json
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL=qwen2.5-coder-3b-instruct-q4_k_m
FP16=qwen2.5-coder-3b-instruct-fp16
ADAPTER=3b-e2
LLAMA=${LLAMA:-$HOME/opt/llama.cpp/src-b12032/build/bin/llama-server}
PORT=8080
P=data/predictions
L=models/lora/3b-run/gguf
F=models/lora/3b-fp16
LOG=$P/dev-phase2-quant.log
PROMPTS=data/prompts/dev.predictions.prompts.jsonl
FLAGS=(--no-placeholder-check --no-repair)
STEM=$P/dev.predictions.prompts
OUT_Q4_BASE=$STEM.$MODEL.no-check.no-repair.jsonl
OUT_Q4_LORA=$STEM.$MODEL.$ADAPTER.no-check.no-repair.jsonl
OUT_FP_BASE=$STEM.$FP16.no-check.no-repair.jsonl
OUT_FP_LORA=$STEM.$FP16.$ADAPTER.no-check.no-repair.jsonl
SERVER_PID=""
SERVER_ARGS=""

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

healthy() { curl -s -m 2 "http://127.0.0.1:$PORT/health" | grep -q '"ok"'; }

stop_server() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID"
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
  SERVER_ARGS=""
}
trap stop_server EXIT

ensure_server() {  # tag, then the LoRA flags; (re)starts the server unless it serves exactly them
  local tag=$1
  shift
  if [ "$SERVER_ARGS" = "$*" ] && healthy; then return 0; fi
  stop_server
  "$LLAMA" -m "$PWD/models/$MODEL.gguf" "$@" -c 8192 -ngl 99 -fa on -np 1 --cache-ram 0 \
    --port $PORT > "$P/server-dev-phase2-quant-$tag.log" 2>&1 &
  SERVER_PID=$!
  SERVER_ARGS="$*"
  for _ in $(seq 1 300); do
    if healthy; then log "llama-server ready: $MODEL $*"; return 0; fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then log "llama-server died: $tag"; return 1; fi
    sleep 1
  done
  log "llama-server not ready after 300 s: $tag"
  return 1
}

# ---- 1. preconditions --------------------------------------------------------------------
mkdir -p "$P"
log "quantization ablation on dev (3B): preconditions"
FP16_READY=1
if uv run python - "$PROMPTS" "$F" "$ADAPTER" "$FP16" <<'PY'
import hashlib, json, sys
from pathlib import Path

prompts, fp16_dir, adapter, fp16 = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

run = json.loads(Path("models/lora/3b-run/run.json").read_text())
trained = {c["name"]: c for c in run["checkpoints"]}[adapter]
if sha(f"models/lora/3b-run/gguf/{adapter}.gguf") != trained["gguf_sha256"]:
    sys.exit(f"{adapter}.gguf is not the GGUF of run.json")
if not (fp16_dir / "generate.json").is_file():
    print(f"no {fp16_dir}/generate.json yet: only the Q4_K_M runs")
    sys.exit(3)
gen = json.loads((fp16_dir / "generate.json").read_text())
if gen["inputs"]["adapter_sha256"] != trained["adapter_sha256"]:
    sys.exit("the fp16 run used another adapter than the trained " + adapter)
if gen["model"]["revision"] != run["model"]["revision"]:
    sys.exit("the fp16 run used another base revision than the training")
# The uploaded prompts are the sql prompts of this file (train_lora.py prepare-dev-prompts).
rows = [json.loads(line) for line in Path(prompts).read_text(encoding="utf-8").splitlines()]
text = "".join(json.dumps({"question_id": r["question_id"], "messages": r["messages"]},
                          ensure_ascii=False) + "\n" for r in rows if r["decision"] == "sql")
if hashlib.sha256(text.encode()).hexdigest() != gen["inputs"]["prompts_sha256"]:
    sys.exit(f"the fp16 run did not answer the sql prompts of {prompts}")
for name in (fp16, f"{fp16}.{adapter}"):
    replies = fp16_dir / f"replies.{name}.jsonl"
    meta = json.loads(replies.with_name(replies.name + ".meta.json").read_text())
    if sha(replies) != meta["replies_sha256"] or meta["summary"]["prompts"] != len(text.splitlines()):
        sys.exit(f"{replies} is incomplete or changed")
print(f"{adapter}: GGUF and fp16 adapter are the trained ones; fp16 replies of "
      f"{len(text.splitlines())} sql prompts for base and + {adapter}")
PY
then
  :
else
  code=$?
  [ "$code" -eq 3 ] || exit "$code"
  FP16_READY=0
fi
for f in "$L/$ADAPTER.gguf" "models/$MODEL.gguf"; do
  [ -f "$f" ] || { log "missing $f"; exit 1; }
done
[ -x "$LLAMA" ] || { log "no llama-server at $LLAMA"; exit 1; }
if healthy; then
  log "port $PORT is busy: stop the running llama-server first"
  exit 1
fi
if [ "${1:-}" = "--check" ]; then
  log "preconditions hold (--check: stopping here)"
  exit 0
fi
START=$(date +%s)

# ---- 2. Q4_K_M with llama-server ---------------------------------------------------------
if [ ! -s "$OUT_Q4_BASE.meta.json" ]; then
  ensure_server base
  log "Q4_K_M base -> $OUT_Q4_BASE"
  uv run eth-nl2sql generate-sql --prompts $PROMPTS "${FLAGS[@]}" --output "$OUT_Q4_BASE" \
    > "$P/dev.phase2-quant.q4-base.log" 2>&1
fi
if [ ! -s "$OUT_Q4_LORA.meta.json" ]; then
  ensure_server lora --lora "$PWD/$L/$ADAPTER.gguf"
  log "Q4_K_M + $ADAPTER -> $OUT_Q4_LORA"
  uv run eth-nl2sql generate-sql --prompts $PROMPTS --adapter "$L/$ADAPTER.gguf" "${FLAGS[@]}" \
    --output "$OUT_Q4_LORA" > "$P/dev.phase2-quant.q4-lora.log" 2>&1
fi
stop_server
if [ "$FP16_READY" = 0 ]; then
  log "Q4_K_M runs done; run again after: kaggle kernels output aohkvd/eth-nl2sql-phase2-fp16 -p $F"
  exit 0
fi

# ---- 3. fp16 replies played back through B8 ------------------------------------------------
for pair in "$FP16:$OUT_FP_BASE" "$FP16.$ADAPTER:$OUT_FP_LORA"; do
  name=${pair%%:*}
  out=${pair#*:}
  if [ ! -s "$out.meta.json" ]; then
    log "fp16 $name -> $out"
    uv run eth-nl2sql generate-sql --prompts $PROMPTS --replies "$F/replies.$name.jsonl" \
      "${FLAGS[@]}" --output "$out" > "$P/dev.phase2-quant.$name.log" 2>&1
  fi
done

# ---- 4. scoring and summary --------------------------------------------------------------
for run in "$OUT_Q4_BASE" "$OUT_Q4_LORA" "$OUT_FP_BASE" "$OUT_FP_LORA"; do
  base=${run%.jsonl}
  if [ -s "$base.ex.json" ]; then
    log "score: $base.ex.json exists"
    continue
  fi
  log "score: $run"
  uv run eth-nl2sql evaluate-sql --gold data/benchmark/dev.jsonl --predictions "$run" \
    --train-manifest data/train/manifest.json --report "$base.ex.json" \
    --details "$base.ex-details.jsonl" > "$base.ex.log" 2>&1
done

uv run python - "$OUT_Q4_BASE" "$OUT_Q4_LORA" "$OUT_FP_BASE" "$OUT_FP_LORA" <<'PY' | tee -a "$LOG"
import json, math, sys
from pathlib import Path

names = ["q4_base", "q4_lora", "fp16_base", "fp16_lora"]
runs = dict(zip(names, sys.argv[1:], strict=True))

def stem(path):
    return path.removesuffix(".jsonl")

def correct(path):
    return {json.loads(line)["question_id"]: json.loads(line)["correct"]
            for line in open(stem(path) + ".ex-details.jsonl")}

def sqls(path):
    return {json.loads(line)["question_id"]: json.loads(line)["sql"] for line in open(path)}

def mcnemar(a, b):
    better = sum(1 for q in a if not a[q] and b[q])
    worse = sum(1 for q in a if a[q] and not b[q])
    n, k = better + worse, min(better, worse)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
    return {"better": better, "worse": worse, "p": round(p, 4)}

ok = {n: correct(p) for n, p in runs.items()}
sql = {n: sqls(p) for n, p in runs.items()}
summary = {"runs": {}, "pairs": {}, "conditions": "dev, one attempt, no placeholder check (§5.4)"}
for n, path in runs.items():
    rep = json.loads(Path(stem(path) + ".ex.json").read_text())
    summary["runs"][n] = {"file": path, "ex": rep["overall"]["ex"],
                          "correct": rep["overall"]["correct"], "kinds": rep["kinds"],
                          "by_train_exposure": rep.get("by_train_exposure")}
for label, a, b in [("quantization, base: fp16 vs Q4", "q4_base", "fp16_base"),
                    ("quantization, + LoRA: fp16 vs Q4", "q4_lora", "fp16_lora"),
                    ("LoRA at Q4", "q4_base", "q4_lora"),
                    ("LoRA at fp16", "fp16_base", "fp16_lora")]:
    same = sum(sql[a][q] == sql[b][q] for q in sql[a] if sql[a][q] is not None)
    asked = sum(sql[a][q] is not None for q in sql[a])
    summary["pairs"][label] = mcnemar(ok[a], ok[b]) | {"same_sql": f"{same}/{asked}"}
Path("data/predictions/dev-phase2-quant.summary.json").write_text(
    json.dumps(summary, indent=1) + "\n")
print(" ".join(f"{n} {r['correct']}/177" for n, r in summary["runs"].items()))
for label, pair in summary["pairs"].items():
    print(f"  {label}: {pair}")
PY
log "finished in $(( ($(date +%s) - START) / 60 )) min"
