#!/usr/bin/env bash
# S4 of docs/phase2-plan.md §7: phase 1 on the test split, once, unattended. About 4 hours on
# the GTX 1650; nothing leaves the machine.
#
#   bash eth-nl2sql/scripts/phase1_test.sh           # the whole run
#   bash eth-nl2sql/scripts/phase1_test.sh --check   # only the preconditions (no GPU, no test)
#
# 1. Preconditions: the S3 freeze (data/train/freeze.json) still matches the train data and the
#    training configuration; port 8080 is free.
# 2. Dev check: the default 3B configuration on dev must give exactly the replies of the frozen
#    phase-1 run (data/predictions/phase1-freeze.json). Otherwise it stops before reading test.
# 3. Links test and builds its 5 prompt files: linker (default), linked schema, no evidence,
#    literal, oracle (gold linking).
# 4. Generates with 3B (6 configurations), then 1.5B (default). A configuration whose
#    predictions are complete is skipped, so running the script again after a crash resumes.
# 5. Stops llama-server, scores every run with evaluate-sql, writes a summary.
#
# Log: data/predictions/test-phase1.log; summary: data/predictions/test-phase1.summary.json.
set -euo pipefail
cd "$(dirname "$0")/.."

LLAMA=${LLAMA:-$HOME/opt/llama.cpp/src-b12032/build/bin/llama-server}
PORT=8080
P=data/predictions
LOG=$P/test-phase1.log
M3=qwen2.5-coder-3b-instruct-q4_k_m
M15=qwen2.5-coder-1.5b-instruct-q4_k_m
SERVER_PID=""
SERVER_MODEL=""
FAILED=()

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

healthy() { curl -s -m 2 "http://127.0.0.1:$PORT/health" | grep -q '"ok"'; }

stop_server() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID"
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
  SERVER_MODEL=""
}
trap stop_server EXIT

start_server() {  # model
  stop_server
  "$LLAMA" -m "$PWD/models/$1.gguf" -c 8192 -ngl 99 -fa on -np 1 --cache-ram 0 --port $PORT \
    > "$P/server-test-$1.log" 2>&1 &
  SERVER_PID=$!
  SERVER_MODEL=$1
  for _ in $(seq 1 300); do
    if healthy; then log "llama-server ready: $1"; return 0; fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then log "llama-server died: $1"; return 1; fi
    sleep 1
  done
  log "llama-server not ready after 300 s: $1"
  return 1
}

ensure_server() {  # model: (re)start it when it is not the one serving
  if [ "$SERVER_MODEL" = "$1" ] && healthy; then return 0; fi
  start_server "$1"
}

# ---- 1. preconditions --------------------------------------------------------------------
mkdir -p "$P"
log "phase 1 on test (S4): preconditions"
uv run python - <<'PY'
import hashlib, json, sys
from pathlib import Path

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

freeze = json.loads(Path("data/train/freeze.json").read_text())
bad = [n for n, h in freeze["train"].items() if sha(Path("data/train") / n) != h]
bad += [n for n, h in freeze["training_config"].items() if sha(n) != h]
if bad:
    sys.exit(f"the S3 freeze no longer matches: {bad}")
print("S3 freeze matches the train data and the training configuration")
PY
if healthy; then
  log "port $PORT is busy: stop the running llama-server first"
  exit 1
fi
[ -x "$LLAMA" ] || { log "no llama-server at $LLAMA"; exit 1; }
for m in $M3 $M15; do [ -f "models/$m.gguf" ] || { log "missing models/$m.gguf"; exit 1; }; done
if [ "${1:-}" = "--check" ]; then
  log "preconditions hold (--check: stopping here)"
  exit 0
fi
START=$(date +%s)

# ---- 2. dev check -------------------------------------------------------------------------
if [ -f "$P/test-phase1.started" ]; then
  log "resuming a run started on $(cat "$P/test-phase1.started"): dev check passed then"
else
  ensure_server $M3
  mkdir -p "$P/recheck"
  RECHECK=$P/recheck/dev.predictions.prompts.$M3.jsonl
  log "dev check: default 3B configuration on dev (about 13 min)"
  uv run eth-nl2sql generate-sql --prompts data/prompts/dev.predictions.prompts.jsonl \
    --output "$RECHECK" > "$P/recheck/dev.3b.log" 2>&1
  uv run python - "$P/dev.predictions.prompts.$M3.jsonl" "$RECHECK" <<'PY' | tee -a "$LOG"
import json, sys

def replies(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        out[r["question_id"]] = (r["decision"], [a.get("reply") for a in r.get("attempts", [])])
    return out

frozen, now = replies(sys.argv[1]), replies(sys.argv[2])
differ = sorted(q for q in frozen if frozen[q] != now.get(q))
if differ or set(now) != set(frozen):
    print(f"dev check FAILED: {len(differ)} questions reply differently: {differ[:10]}")
    sys.exit(1)
print(f"dev check passed: the {len(frozen)} replies are identical to the frozen phase-1 run")
PY
  date '+%F %T' > "$P/test-phase1.started"
fi

# ---- 3. link test, build its prompts -----------------------------------------------------
if [ ! -s data/linking/test.predictions.jsonl ]; then
  log "linking test"
  uv run eth-nl2sql evaluate-linking --allow-test --gold data/benchmark/test.jsonl \
    --output data/linking/test.predictions.jsonl --report data/linking/test.report.json \
    > data/linking/test.report.log 2>&1
fi
build() {  # output, then build-prompts arguments
  local out=$1
  shift
  if [ ! -s "$out" ]; then
    log "prompts: $out"
    uv run eth-nl2sql build-prompts --allow-test "$@" --output "$out" >> "$LOG" 2>&1
  fi
}
LINKED=data/linking/test.predictions.jsonl
build data/prompts/test.predictions.prompts.jsonl --input $LINKED
build data/prompts/test.predictions.prompts.linked-schema.jsonl --input $LINKED --schema linked
build data/prompts/test.predictions.prompts.no-evidence.jsonl --input $LINKED --no-evidence
build data/prompts/test.predictions.prompts.literal.jsonl --input $LINKED --literal
build data/prompts/test.prompts.jsonl --input data/benchmark/test.jsonl

# ---- 4. generation --------------------------------------------------------------------------
generate() {  # model, tag, prompts, output, extra generate-sql arguments
  local model=$1 tag=$2 prompts=$3 out=$4
  shift 4
  if [ -s "$out.meta.json" ]; then
    log "$tag: done already"
    return 0
  fi
  ensure_server "$model" || { FAILED+=("$tag"); return 0; }
  log "$tag: generating -> $out"
  if uv run eth-nl2sql generate-sql --allow-test --prompts "$prompts" --output "$out" "$@" \
    > "$P/test.$tag.log" 2>&1; then
    log "$tag: done"
  else
    log "$tag: FAILED, see $P/test.$tag.log"
    FAILED+=("$tag")
  fi
}
ensure_server $M3
log "prompt tokens"
uv run eth-nl2sql count-prompt-tokens --allow-test data/prompts/test.predictions.prompts.jsonl \
  data/prompts/test.predictions.prompts.linked-schema.jsonl \
  data/prompts/test.predictions.prompts.no-evidence.jsonl \
  data/prompts/test.predictions.prompts.literal.jsonl data/prompts/test.prompts.jsonl \
  --report "$P/test.prompt-tokens.json" >> "$LOG" 2>&1 || log "prompt tokens: FAILED"

TP=data/prompts/test.predictions.prompts
generate $M3 3b $TP.jsonl "$P/test.predictions.prompts.$M3.jsonl"
generate $M3 3b.no-check $TP.jsonl "$P/test.predictions.prompts.$M3.no-check.jsonl" \
  --no-placeholder-check
generate $M3 3b.oracle data/prompts/test.prompts.jsonl "$P/test.prompts.$M3.jsonl"
generate $M3 3b.linked-schema $TP.linked-schema.jsonl \
  "$P/test.predictions.prompts.linked-schema.$M3.jsonl"
generate $M3 3b.no-evidence $TP.no-evidence.jsonl \
  "$P/test.predictions.prompts.no-evidence.$M3.jsonl"
generate $M3 3b.literal $TP.literal.jsonl "$P/test.predictions.prompts.literal.$M3.jsonl"
generate $M15 1.5b $TP.jsonl "$P/test.predictions.prompts.$M15.jsonl"
stop_server

# ---- 5. scoring (llama-server stopped: DuckDB gets the memory) -----------------------------
score() {  # predictions, extra evaluate-sql arguments
  local pred=$1
  shift
  local base=${pred%.jsonl}
  if [ ! -s "$pred.meta.json" ]; then
    log "score: $pred has no complete run, skipped"
    return 0
  fi
  if [ -s "$base.ex.json" ]; then
    log "score: $base.ex.json exists"
    return 0
  fi
  log "score: $pred"
  uv run eth-nl2sql evaluate-sql --allow-test --gold data/benchmark/test.jsonl \
    --predictions "$pred" --report "$base.ex.json" --details "$base.ex-details.jsonl" "$@" \
    > "$base.ex.log" 2>&1 || { log "score: FAILED, see $base.ex.log"; FAILED+=("score $pred"); }
}
score "$P/test.predictions.prompts.$M3.jsonl"
score "$P/test.predictions.prompts.$M3.no-check.jsonl"
score "$P/test.prompts.$M3.jsonl" --linking data/benchmark/test.jsonl
score "$P/test.predictions.prompts.linked-schema.$M3.jsonl"
score "$P/test.predictions.prompts.no-evidence.$M3.jsonl"
score "$P/test.predictions.prompts.literal.$M3.jsonl" --only-predicted
score "$P/test.predictions.prompts.$M15.jsonl"

# ---- summary --------------------------------------------------------------------------------
uv run python - <<'PY' | tee -a "$LOG"
import json
from pathlib import Path

P = Path("data/predictions")
M3, M15 = "qwen2.5-coder-3b-instruct-q4_k_m", "qwen2.5-coder-1.5b-instruct-q4_k_m"
runs = {
    "3b": f"test.predictions.prompts.{M3}",
    "3b_no_placeholder_check": f"test.predictions.prompts.{M3}.no-check",
    "3b_oracle_linking": f"test.prompts.{M3}",
    "3b_linked_schema": f"test.predictions.prompts.linked-schema.{M3}",
    "3b_no_evidence": f"test.predictions.prompts.no-evidence.{M3}",
    "3b_literal (only predicted)": f"test.predictions.prompts.literal.{M3}",
    "1.5b": f"test.predictions.prompts.{M15}",
}
summary = {}
for name, stem in runs.items():
    path = P / f"{stem}.ex.json"
    if path.is_file():
        o = json.loads(path.read_text())["overall"]
        summary[name] = {"ex": o["ex"], "correct": o["correct"], "questions": o["questions"]}
        print(f"{name:<28} EX {o['ex']:.3f}  {o['correct']}/{o['questions']}")
    else:
        summary[name] = None
        print(f"{name:<28} no result")
(P / "test-phase1.summary.json").write_text(json.dumps(summary, indent=1) + "\n")
PY
log "finished in $(( ($(date +%s) - START) / 60 )) min; failed: ${FAILED[*]:-none}"
