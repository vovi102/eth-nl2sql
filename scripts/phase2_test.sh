#!/usr/bin/env bash
# S8 of docs/phase2-plan.md §7: phase 2 on the test split, once, unattended, with the
# checkpoints chosen on dev (§5.3). About 2 hours on the GTX 1650; nothing leaves the machine.
#
#   bash scripts/phase2_test.sh           # the whole run
#   bash scripts/phase2_test.sh --check   # only the preconditions (no GPU, no test)
#
# 1. Preconditions: the S3 freeze still holds; the chosen checkpoints (dev-phase2-<model>.summary
#    .json) are the GGUF adapters of run.json; the test linking and prompt files are byte for byte
#    those of the phase-1 test run (S4), so that both phases answer the same prompts; port free.
# 2. Dev check: 3B + its chosen adapter on dev must give exactly the replies of the S6 dev run.
#    Otherwise it stops before reading test.
# 3. Generates the configurations of §5.5 on test: 3B + LoRA (default, --no-placeholder-check,
#    oracle linking), then 1.5B + LoRA (default). A configuration whose predictions are complete is
#    skipped, so running the script again after a crash resumes.
# 4. Stops llama-server, scores every run (evaluate-sql --train-manifest), writes a summary that
#    sets each run beside its phase-1 counterpart.
#
# Log: data/predictions/test-phase2.log; summary: data/predictions/test-phase2.summary.json.
set -euo pipefail
cd "$(dirname "$0")/.."

LLAMA=${LLAMA:-$HOME/opt/llama.cpp/src-b12032/build/bin/llama-server}
PORT=8080
P=data/predictions
LOG=$P/test-phase2.log
M3=qwen2.5-coder-3b-instruct-q4_k_m
M15=qwen2.5-coder-1.5b-instruct-q4_k_m
SERVER_PID=""
SERVER_ARGS=""
FAILED=()

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

ensure_server() {  # tag, model, then the LoRA flags; (re)starts unless it serves exactly them
  local tag=$1 model=$2
  shift 2
  if [ "$SERVER_ARGS" = "$model $*" ] && healthy; then return 0; fi
  stop_server
  "$LLAMA" -m "$PWD/models/$model.gguf" "$@" -c 8192 -ngl 99 -fa on -np 1 --cache-ram 0 \
    --port $PORT > "$P/server-test-phase2-$tag.log" 2>&1 &
  SERVER_PID=$!
  SERVER_ARGS="$model $*"
  for _ in $(seq 1 300); do
    if healthy; then log "llama-server ready: $model $*"; return 0; fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then log "llama-server died: $tag"; return 1; fi
    sleep 1
  done
  log "llama-server not ready after 300 s: $tag"
  return 1
}

# ---- 1. preconditions --------------------------------------------------------------------
mkdir -p "$P"
log "phase 2 on test (S8): preconditions"
CHOSEN=$(uv run python - <<'PY'
import hashlib, json, sys
from pathlib import Path

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

freeze = json.loads(Path("data/train/freeze.json").read_text())
bad = [n for n, h in freeze["train"].items() if sha(Path("data/train") / n) != h]
bad += [n for n, h in freeze["training_config"].items() if sha(n) != h]
if bad:
    sys.exit(f"the S3 freeze no longer matches: {bad}")
chosen = []
for key in ("3b", "1.5b"):
    name = json.loads(Path(f"data/predictions/dev-phase2-{key}.summary.json").read_text())["chosen"]
    run = json.loads(Path(f"models/lora/{key}-run/run.json").read_text())
    trained = {c["name"]: c for c in run["checkpoints"]}[name]
    if sha(f"models/lora/{key}-run/gguf/{name}.gguf") != trained["gguf_sha256"]:
        sys.exit(f"{name}.gguf is not the GGUF of run.json")
    chosen.append(name)
# The same test inputs as the phase-1 run (S4).
P = Path("data/predictions")
for stem in ("test.predictions.prompts.qwen2.5-coder-3b-instruct-q4_k_m",
             "test.prompts.qwen2.5-coder-3b-instruct-q4_k_m"):
    meta = json.loads((P / f"{stem}.jsonl.meta.json").read_text())
    if sha(meta["prompts"]) != meta["prompts_sha256"] or sha(meta["linking"]) != meta["linking_sha256"]:
        sys.exit(f"{meta['prompts']} or {meta['linking']} changed since the phase-1 test run")
print(f"S3 freeze holds; chosen on dev: {', '.join(chosen)}; test inputs as in S4",
      file=sys.stderr)
print(" ".join(chosen))
PY
)
read -r A3 A15 <<< "$CHOSEN"
log "chosen checkpoints: 3B $A3, 1.5B $A15"
L3=models/lora/3b-run/gguf/$A3.gguf
L15=models/lora/1.5b-run/gguf/$A15.gguf
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
if [ -f "$P/test-phase2.started" ]; then
  log "resuming a run started on $(cat "$P/test-phase2.started"): dev check passed then"
else
  ensure_server dev $M3 --lora "$PWD/$L3"
  mkdir -p "$P/recheck-phase2"
  RECHECK=$P/recheck-phase2/dev.predictions.prompts.$M3.$A3.jsonl
  log "dev check: 3B + $A3 on dev (about 14 min)"
  uv run eth-nl2sql generate-sql --prompts data/prompts/dev.predictions.prompts.jsonl \
    --adapter "$L3" --output "$RECHECK" > "$P/recheck-phase2/dev.$A3.log" 2>&1
  uv run python - "$P/dev.predictions.prompts.$M3.$A3.jsonl" "$RECHECK" <<'PY' | tee -a "$LOG"
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
print(f"dev check passed: the {len(frozen)} replies are identical to the S6 dev run")
PY
  date '+%F %T' > "$P/test-phase2.started"
fi

# ---- 3. generation --------------------------------------------------------------------------
generate() {  # tag, server tag, model, adapter, prompts, output, extra generate-sql arguments
  local tag=$1 server=$2 model=$3 adapter=$4 prompts=$5 out=$6
  shift 6
  if [ -s "$out.meta.json" ]; then
    log "$tag: done already"
    return 0
  fi
  ensure_server "$server" "$model" --lora "$PWD/$adapter" || { FAILED+=("$tag"); return 0; }
  log "$tag: generating -> $out"
  if uv run eth-nl2sql generate-sql --allow-test --prompts "$prompts" --adapter "$adapter" \
    --output "$out" "$@" > "$P/test.phase2-$tag.log" 2>&1; then
    log "$tag: done"
  else
    log "$tag: FAILED, see $P/test.phase2-$tag.log"
    FAILED+=("$tag")
  fi
}
TP=data/prompts/test.predictions.prompts.jsonl
generate $A3 3b $M3 "$L3" $TP "$P/test.predictions.prompts.$M3.$A3.jsonl"
generate $A3.no-check 3b $M3 "$L3" $TP "$P/test.predictions.prompts.$M3.$A3.no-check.jsonl" \
  --no-placeholder-check
generate $A3.oracle 3b $M3 "$L3" data/prompts/test.prompts.jsonl \
  "$P/test.prompts.$M3.$A3.jsonl"
generate $A15 1.5b $M15 "$L15" $TP "$P/test.predictions.prompts.$M15.$A15.jsonl"
stop_server

# ---- 4. scoring (llama-server stopped: DuckDB gets the memory) -----------------------------
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
    --predictions "$pred" --train-manifest data/train/manifest.json --report "$base.ex.json" \
    --details "$base.ex-details.jsonl" "$@" > "$base.ex.log" 2>&1 \
    || { log "score: FAILED, see $base.ex.log"; FAILED+=("score $pred"); }
}
score "$P/test.predictions.prompts.$M3.$A3.jsonl"
score "$P/test.predictions.prompts.$M3.$A3.no-check.jsonl"
score "$P/test.prompts.$M3.$A3.jsonl" --linking data/benchmark/test.jsonl
score "$P/test.predictions.prompts.$M15.$A15.jsonl"

# ---- summary: each run beside its phase-1 counterpart (same prompts, same scorer) -----------
uv run python - "$A3" "$A15" <<'PY' | tee -a "$LOG"
import json, math, sys
from pathlib import Path

a3, a15 = sys.argv[1:]
P = Path("data/predictions")
M3, M15 = "qwen2.5-coder-3b-instruct-q4_k_m", "qwen2.5-coder-1.5b-instruct-q4_k_m"
runs = {  # name: (phase 2, phase 1)
    "3b": (f"test.predictions.prompts.{M3}.{a3}", f"test.predictions.prompts.{M3}"),
    "3b_no_placeholder_check": (f"test.predictions.prompts.{M3}.{a3}.no-check",
                                f"test.predictions.prompts.{M3}.no-check"),
    "3b_oracle_linking": (f"test.prompts.{M3}.{a3}", f"test.prompts.{M3}"),
    "1.5b": (f"test.predictions.prompts.{M15}.{a15}", f"test.predictions.prompts.{M15}"),
}

def correct(stem):
    return {json.loads(line)["question_id"]: json.loads(line)["correct"]
            for line in open(P / f"{stem}.ex-details.jsonl")}

def mcnemar(a, b):
    better = sum(1 for q in a if not a[q] and b[q])
    worse = sum(1 for q in a if a[q] and not b[q])
    n, k = better + worse, min(better, worse)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
    return {"better": better, "worse": worse, "p": round(p, 6)}

summary = {}
for name, (two, one) in runs.items():
    path = P / f"{two}.ex.json"
    if not path.is_file():
        summary[name] = None
        print(f"{name:<26} no result")
        continue
    rep = json.loads(path.read_text())
    old = json.loads((P / f"{one}.ex.json").read_text())["overall"]
    o = rep["overall"]
    summary[name] = {"ex": o["ex"], "correct": o["correct"], "questions": o["questions"],
                     "phase1": {"ex": old["ex"], "correct": old["correct"]},
                     "vs_phase1": mcnemar(correct(one), correct(two)),
                     "by_train_exposure": rep.get("by_train_exposure")}
    print(f"{name:<26} EX {o['ex']:.3f}  {o['correct']}/{o['questions']}  "
          f"(phase 1 {old['correct']}; {summary[name]['vs_phase1']})")
(P / "test-phase2.summary.json").write_text(json.dumps(summary, indent=1) + "\n")
PY
log "finished in $(( ($(date +%s) - START) / 60 )) min; failed: ${FAILED[*]:-none}"
