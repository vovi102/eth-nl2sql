#!/usr/bin/env bash
# S5/S6 of docs/phase2-plan.md §7: a trained model's two checkpoints on dev, unattended, on the
# local GTX 1650. Test is never read.
#
#   bash scripts/phase2_dev.sh 1.5b            # or 3b
#   bash scripts/phase2_dev.sh 1.5b --check    # only the preconditions (no GPU)
#
# Input: the output of the Kaggle run, downloaded with
#   kaggle kernels output aohkvd/eth-nl2sql-phase2-train -p models/lora/<key>-run
# i.e. models/lora/<key>-run/{run.json, gguf/<key>-e1.gguf, gguf/<key>-e2.gguf}; the SHA256 of
# the GGUF files must be the ones run.json records.
# 1. Preconditions: the S3 freeze still holds; the adapters are the trained ones; port free.
# 2. Runtime check (§5.2): the base GGUF of phase 1 with the epoch-2 adapter at scale 0 must
#    reply exactly as the frozen phase-1 run on dev. Otherwise it stops.
# 3. Dev with each adapter at scale 1 (same flags as phase 1: -c 8192 -ngl 99 -fa on -np 1
#    --cache-ram 0; fixed prefix, placeholder check, one repair).
# 4. Stops llama-server, scores every run (evaluate-sql --train-manifest), picks the
#    checkpoint by §5.3: the higher dev EX, epoch 2 on a tie (decision 23).
# A step whose output is complete is skipped, so running the script again resumes.
#
# Log: data/predictions/dev-phase2-<key>.log; summary: data/predictions/dev-phase2-<key>.summary.json
set -euo pipefail
cd "$(dirname "$0")/.."

KEY=${1:?usage: phase2_dev.sh 1.5b|3b [--check]}
case $KEY in
  1.5b) MODEL=qwen2.5-coder-1.5b-instruct-q4_k_m ;;
  3b) MODEL=qwen2.5-coder-3b-instruct-q4_k_m ;;
  *) echo "unknown model $KEY"; exit 1 ;;
esac
LLAMA=${LLAMA:-$HOME/opt/llama.cpp/src-b12032/build/bin/llama-server}
PORT=8080
P=data/predictions
L=models/lora/$KEY-run/gguf
LOG=$P/dev-phase2-$KEY.log
PROMPTS=data/prompts/dev.predictions.prompts.jsonl
FROZEN_RUN=$P/dev.predictions.prompts.$MODEL.jsonl
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
    --port $PORT > "$P/server-dev-phase2-$KEY-$tag.log" 2>&1 &
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
log "phase 2 on dev ($KEY): preconditions"
uv run python - "$KEY" <<'PY'
import hashlib, json, sys
from pathlib import Path

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

key = sys.argv[1]
freeze = json.loads(Path("data/train/freeze.json").read_text())
bad = [n for n, h in freeze["train"].items() if sha(Path("data/train") / n) != h]
bad += [n for n, h in freeze["training_config"].items() if sha(n) != h]
if bad:
    sys.exit(f"the S3 freeze no longer matches: {bad}")
run = json.loads(Path(f"models/lora/{key}-run/run.json").read_text())
trained = {c["name"]: c for c in run["checkpoints"]}
for epoch in (1, 2):
    name = f"{key}-e{epoch}"
    if name not in trained:
        sys.exit(f"run.json has no checkpoint {name}: {sorted(trained)}")
    if sha(f"models/lora/{key}-run/gguf/{name}.gguf") != trained[name]["gguf_sha256"]:
        sys.exit(f"{name}.gguf is not the GGUF of run.json")
print(f"S3 freeze holds; {key}-e1/e2.gguf are the adapters of run.json "
      f"({run['model']['hf_repo']}@{run['model']['revision'][:10]})")
PY
for f in "$L/$KEY-e1.gguf" "$L/$KEY-e2.gguf" "models/$MODEL.gguf" "$FROZEN_RUN"; do
  [ -f "$f" ] || { log "missing $f"; exit 1; }
done
[ -x "$LLAMA" ] || { log "no llama-server at $LLAMA"; exit 1; }
if healthy; then
  log "port $PORT is busy: stop the running llama-server first"
  exit 1
fi
if [ "${2:-}" = "--check" ]; then
  log "preconditions hold (--check: stopping here)"
  exit 0
fi
START=$(date +%s)

# ---- 2. runtime check: the adapter at scale 0 must change nothing --------------------------
CHECK=$P/dev.predictions.prompts.$MODEL.$KEY-e2.scale0.jsonl
if [ ! -s "$CHECK.meta.json" ]; then
  ensure_server scale0 --lora-scaled "$PWD/$L/$KEY-e2.gguf:0"
  log "runtime check: $KEY-e2 at scale 0 on dev"
  uv run eth-nl2sql generate-sql --prompts $PROMPTS --adapter "$L/$KEY-e2.gguf" \
    > "$P/dev.phase2-$KEY.scale0.log" 2>&1
fi
uv run python - "$FROZEN_RUN" "$CHECK" <<'PY' | tee -a "$LOG"
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
    print(f"runtime check FAILED: {len(differ)} questions reply differently: {differ[:10]}")
    sys.exit(1)
print(f"runtime check passed: the {len(frozen)} replies at scale 0 are identical to phase 1")
PY

# ---- 3. dev with each checkpoint ------------------------------------------------------------
for e in 1 2; do
  OUT=$P/dev.predictions.prompts.$MODEL.$KEY-e$e.jsonl
  if [ -s "$OUT.meta.json" ]; then
    log "$KEY-e$e: done already"
    continue
  fi
  ensure_server e$e --lora "$PWD/$L/$KEY-e$e.gguf"
  log "$KEY-e$e: generating -> $OUT"
  uv run eth-nl2sql generate-sql --prompts $PROMPTS --adapter "$L/$KEY-e$e.gguf" \
    > "$P/dev.phase2-$KEY-e$e.log" 2>&1
  log "$KEY-e$e: done"
done
stop_server

# ---- 4. scoring (llama-server stopped: DuckDB gets the memory), checkpoint choice ----------
for run in "$CHECK" "$P/dev.predictions.prompts.$MODEL.$KEY-e1.jsonl" \
    "$P/dev.predictions.prompts.$MODEL.$KEY-e2.jsonl"; do
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

uv run python - "$KEY" "$MODEL" <<'PY' | tee -a "$LOG"
import json, sys
from pathlib import Path

key, model = sys.argv[1:]
P = Path("data/predictions")

def overall(stem):
    rep = json.loads((P / f"{stem}.ex.json").read_text())
    return {"ex": rep["overall"]["ex"], "correct": rep["overall"]["correct"],
            "questions": rep["overall"]["questions"],
            "by_train_exposure": rep.get("by_train_exposure")}

stem = f"dev.predictions.prompts.{model}"
phase1 = json.loads((P / f"{stem}.ex.json").read_text())["overall"]
runs = {f"e{e}": overall(f"{stem}.{key}-e{e}") for e in (1, 2)}
scale0 = overall(f"{stem}.{key}-e2.scale0")
# §5.3, decision 23: the higher dev EX; a tie goes to epoch 2.
chosen = "e1" if runs["e1"]["correct"] > runs["e2"]["correct"] else "e2"
summary = {"model": model, "phase1": {"ex": phase1["ex"], "correct": phase1["correct"]},
           "scale0_check": scale0, **runs, "chosen": f"{key}-{chosen}",
           "rule": "higher dev EX, epoch 2 on a tie (phase2-plan §5.3)"}
(P / f"dev-phase2-{key}.summary.json").write_text(json.dumps(summary, indent=1) + "\n")
print(f"phase 1 {phase1['correct']}/177, scale 0 {scale0['correct']}/177, "
      f"e1 {runs['e1']['correct']}/177, e2 {runs['e2']['correct']}/177 -> {key}-{chosen}")
PY
log "finished in $(( ($(date +%s) - START) / 60 )) min"
