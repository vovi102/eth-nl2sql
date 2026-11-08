"""LoRA fine-tuning of Qwen2.5-Coder for phase 2 (docs/phase2-plan.md §4; decisions 18, 19, 23).

Subcommands:

- `prepare-dataset` (locally): the folder of the private Kaggle dataset, sft.jsonl only (R6).
- `prepare` (locally): the folder of a Kaggle kernel. A script kernel is one file that runs
  without arguments, so the kernel is this file with the run baked into `KERNEL`: its
  arguments, the frozen config.json, requirements.lock and the SHA256 of the S3 freeze.
- `train` (on Kaggle; locally on CPU with a tiny model, for the checks):
  1. checks sft.jsonl and config.json against data/train/freeze.json (S3);
  2. on Kaggle, installs requirements.lock (`--install`) and keeps one of the two T4;
  3. downloads the base model at its pinned revision (`REVISIONS`, recorded in run.json);
  4. trains with TRL's SFTTrainer on prompt-completion examples: the prompt is the system and
     user messages, the completion the assistant message, so the loss covers the reply and
     the end of turn only (`check_tokenized` proves it on every example), and TRL's fused LM
     head projects only those positions, without the (sequence x vocabulary) logits
     (`check_fused_head` compares it with the full logits once, on the GPU it runs on);
  5. keeps the PEFT adapter of each epoch (or of the last step with --max-steps), converts it
     to a GGUF f16 adapter with convert_lora_to_gguf.py of llama.cpp b12032, the runtime of
     the evaluation, and writes run.json: versions, hashes, timings, memory, losses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Kaggle may run a script kernel through IPython, where __file__ is not set.
HERE = Path(globals().get("__file__", "train_lora.py")).resolve().parent
REPO = HERE.parent.parent  # eth-nl2sql/, for the local subcommands

# Pinned at S5 (2026-11-07) from the Hugging Face API; config.json leaves them to this step.
REVISIONS = {
    "1.5b": "2e1fd397ee46e1388853d2af2c993145b0f1098a",
    "3b": "488639f1ff808d1d3d0ba301aef8c11461451ec5",
}
# The chat template of both revisions; byte-identical to TRL's qwen2_5.jinja. It renders our
# messages (system, user, assistant; no tools) exactly as the template inside the phase-1 GGUF.
CHAT_TEMPLATE_SHA256 = "cd8e9439f0570856fd70470bf8889ebd8b5d1107207f67a5efb46e342330527f"
END_OF_TURN = "<|im_end|>"
LLAMA_CPP = {
    "url": "https://github.com/ggml-org/llama.cpp",
    "tag": "b12032",
    "commit": "dde879bc4f954b809892fd0d660515f42536394a",  # build_info of the local llama-server
    "paths": ["conversion", "gguf-py"],  # with the root files, what convert_lora_to_gguf.py needs
}
KAGGLE_INPUT = Path("/kaggle/input")
KAGGLE_WORKING = Path("/kaggle/working")

# Set by `prepare` in the kernel's copy of this file.
KERNEL: dict | None = None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(1 << 24):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


# ---- data ------------------------------------------------------------------------------------


def load_examples(path: Path, limit: int | None = None) -> tuple[list[dict], list[dict]]:
    """The train and val rows of sft.jsonl as prompt-completion examples (decision 19).

    `limit` keeps the first `limit` rows of each split (local checks only).
    """
    splits: dict[str, list[dict]] = {"train": [], "val": []}
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        record = json.loads(line)
        roles = [m["role"] for m in record["messages"]]
        if record["decision"] != "sql" or roles != ["system", "user", "assistant"]:
            raise ValueError(f"line {n}: expected a sql example of system, user, assistant")
        if record["split"] not in splits:
            raise ValueError(f"line {n}: split {record['split']!r}")
        splits[record["split"]].append({
            "question_id": record["question_id"],
            "prompt": record["messages"][:2],
            "completion": record["messages"][2:],
        })  # fmt: skip
    if limit:
        splits = {k: rows[:limit] for k, rows in splits.items()}
    return splits["train"], splits["val"]


def check_tokenized(rows: list[dict], dataset, tokenizer, max_length: int) -> dict:
    """Every example as the trainer will see it, against what the runtime sees.

    - input_ids are the chat template of the prompt with the generation prompt (exactly what
      llama-server renders for generate-sql), followed by the reply;
    - the labels mask everything up to the generation prompt and keep every later token: the
      reply, the end of turn and the newline the template writes after it;
    - nothing is truncated (TRL cuts at max_length without a word).
    Raises ValueError on the first example that breaks a rule; returns length statistics.
    """
    lengths, kept = [], []
    for row, example in zip(rows, dataset, strict=True):
        ids, labels = list(example["input_ids"]), list(example["labels"])
        prompt_ids = tokenizer.apply_chat_template(
            row["prompt"], add_generation_prompt=True, tokenize=True, return_dict=True
        )["input_ids"]
        full = tokenizer.apply_chat_template(
            row["prompt"] + row["completion"], tokenize=True, return_dict=True
        )["input_ids"]
        n = len(prompt_ids)
        qid = row["question_id"]
        if ids != list(full):
            raise ValueError(f"{qid}: input_ids are not the chat template of the example")
        if len(ids) > max_length:
            raise ValueError(f"{qid}: {len(ids)} tokens > max_length {max_length}")
        if ids[:n] != list(prompt_ids):
            raise ValueError(f"{qid}: the prompt is not a prefix of prompt + completion")
        if any(label != -100 for label in labels[:n]):
            raise ValueError(f"{qid}: a prompt token is in the loss")
        if labels[n:] != ids[n:]:
            raise ValueError(f"{qid}: a token after the prompt is left out of the loss")
        expected = row["completion"][0]["content"] + END_OF_TURN + "\n"
        if tokenizer.decode(ids[n:]) != expected:
            raise ValueError(f"{qid}: the trained tokens are not the reply and its end of turn")
        lengths.append(len(ids))
        kept.append(len(ids) - n)
    return {
        "examples": len(lengths),
        "tokens": {"min": min(lengths), "median": statistics.median(lengths),
                   "max": max(lengths)},
        "trained_tokens": {"min": min(kept), "median": statistics.median(kept),
                           "max": max(kept), "total": sum(kept)},
    }  # fmt: skip


# ---- freeze ----------------------------------------------------------------------------------


def frozen_hashes(freeze: dict) -> dict[str, str]:
    """The SHA256 that sft.jsonl and config.json must have (data/train/freeze.json, S3)."""
    return {
        "sft.jsonl": freeze["train"]["sft.jsonl"],
        "config.json": freeze["training_config"]["kaggle/phase2-train/config.json"],
    }


def check_freeze(data: Path, config_text: str, frozen: dict[str, str]) -> None:
    found = {"sft.jsonl": sha256(data), "config.json": sha256_text(config_text)}
    bad = [name for name, digest in found.items() if digest != frozen[name]]
    if bad:
        raise SystemExit(f"the S3 freeze no longer matches: {bad}")


def find_kaggle_data() -> Path:
    found = sorted(KAGGLE_INPUT.rglob("sft.jsonl"))
    if len(found) != 1:
        raise SystemExit(f"expected one sft.jsonl under {KAGGLE_INPUT}, found {found}")
    return found[0]


# ---- llama.cpp conversion ---------------------------------------------------------------------


def fetch_llama_cpp(dest: Path) -> dict:
    """A sparse clone of llama.cpp at the tag of the local runtime; checks the commit."""
    if not dest.exists():
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", LLAMA_CPP["tag"],
                        "--filter=blob:none", "--sparse", LLAMA_CPP["url"], str(dest)],
                       check=True)  # fmt: skip
        subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", *LLAMA_CPP["paths"]],
                       check=True)  # fmt: skip
    commit = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()  # fmt: skip
    if commit != LLAMA_CPP["commit"]:
        raise SystemExit(f"llama.cpp {LLAMA_CPP['tag']} is {commit}, not {LLAMA_CPP['commit']}")
    return {"tag": LLAMA_CPP["tag"], "commit": commit, "path": str(dest)}


def convert_to_gguf(adapter_dir: Path, base_dir: Path, out_file: Path, llama_dir: Path) -> None:
    """convert_lora_to_gguf.py: f16 adapter; the base directory only lends its config.json."""
    env = {k: v for k, v in os.environ.items() if k != "NO_LOCAL_GGUF"}
    subprocess.run([sys.executable, str(llama_dir / "convert_lora_to_gguf.py"),
                    "--base", str(base_dir), "--outtype", "f16", "--outfile", str(out_file),
                    str(adapter_dir)], check=True, env=env)  # fmt: skip


# ---- training --------------------------------------------------------------------------------


def install(lock_text: str) -> None:
    lock = Path("/tmp/requirements.lock")
    lock.write_text(lock_text)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
                    "--require-hashes", "-r", str(lock)], check=True)  # fmt: skip


# PEFT 0.21.2 raises ImportError, instead of skipping the backend, when one of these optional
# packages is installed but too old; Kaggle's image has torchao 0.10.0 (pilot 1 stopped on it).
# LoRA on fp16 weights uses none of them.
OPTIONAL_BACKENDS = {"torchao": "is_torchao_available", "gptqmodel": "is_gptqmodel_available"}


def remove_incompatible_backends(run=subprocess.run) -> list[dict]:
    """Uninstall each optional backend whose PEFT check raises, then check again."""
    removed = []
    for package, check in OPTIONAL_BACKENDS.items():
        probe = [sys.executable, "-c", f"from peft.import_utils import {check}; {check}()"]
        result = run(probe, capture_output=True, text=True)
        if result.returncode == 0:
            continue
        if "ImportError" not in result.stderr:
            raise SystemExit(f"{check} failed: {result.stderr[-500:]}")
        run([sys.executable, "-m", "pip", "uninstall", "--yes", package], check=True)
        if run(probe, capture_output=True, text=True).returncode != 0:
            raise SystemExit(f"{check} still fails after uninstalling {package}")
        removed.append({"package": package, "reason": result.stderr.strip().splitlines()[-1]})
    return removed


def locked_versions(lock_text: str) -> dict[str, str]:
    out = {}
    for line in lock_text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#") and "==" in line:
            name, version = line.split(" ")[0].split(";")[0].split("==")
            out[name.lower()] = version
    return out


def check_versions(lock_text: str) -> dict[str, str]:
    """The versions that matter, as installed; they must be the locked ones."""
    from importlib.metadata import version

    locked = locked_versions(lock_text)
    found = {}
    for name in ("trl", "peft", "transformers", "accelerate", "datasets", "tokenizers"):
        found[name] = version(name)
        if found[name] != locked[name]:
            raise SystemExit(f"{name} {found[name]} is installed, the lock has {locked[name]}")
    found["torch"] = version("torch")
    return found


def download_base(repo: str, revision: str, dest: Path) -> dict[str, str]:
    from huggingface_hub import snapshot_download

    patterns = ["*.json", "*.safetensors", "merges.txt", "vocab.json"]
    snapshot_download(repo, revision=revision, local_dir=dest, allow_patterns=patterns)
    return {p.name: sha256(p) for p in sorted(dest.iterdir()) if p.is_file()}


class FusedHeadError(RuntimeError):
    pass


def repeat_kv_in_sdpa(on: bool) -> None:
    """With `on`, transformers' sdpa attention expands the key/value heads itself (`repeat_kv`)
    instead of passing `enable_gqa=True` to PyTorch.

    Qwen2.5 uses grouped-query attention (1.5B: 12 query heads, 2 key/value heads). Below sm80 (a
    T4 is sm75), PyTorch 2.11 has neither its flash nor its cuDNN kernel, and its
    memory-efficient kernel takes no GQA, so `enable_gqa` falls back to the math kernel, which
    builds the (heads x sequence x sequence) attention: pilot 2 ran at 162 s per step with 13.8 GB
    reserved. Repeated heads are the same attention, on the memory-efficient kernel.
    """
    import transformers.integrations.sdpa_attention as sdpa

    if not hasattr(sdpa, "_original_use_gqa_in_sdpa"):
        sdpa._original_use_gqa_in_sdpa = sdpa.use_gqa_in_sdpa
    sdpa.use_gqa_in_sdpa = _never if on else sdpa._original_use_gqa_in_sdpa


def _never(*args, **kwargs) -> bool:
    return False


def check_attention(model, example: dict, device, backend=None) -> dict:
    """One train example through both attention paths, under the autocast of training: the loss
    must agree. The repeated-heads path runs restricted to `backend` (the memory-efficient kernel
    on a GPU), so it fails if that kernel cannot take it. Seconds of the second forward of each
    path (the first one warms up). Leaves the repeated heads on."""
    import torch

    ids = torch.tensor([example["input_ids"]], device=device)
    labels = torch.tensor([example["labels"]], device=device)
    mask = torch.ones_like(ids)
    cuda = device.type == "cuda"

    def run(repeat: bool) -> tuple[float, float]:
        from contextlib import nullcontext

        from torch.nn.attention import sdpa_kernel

        repeat_kv_in_sdpa(repeat)
        for _ in range(2):
            # fresh context managers each time: sdpa_kernel's cannot be entered twice
            kernels = sdpa_kernel([backend]) if repeat and backend is not None else nullcontext()
            autocast = torch.autocast(device.type, dtype=torch.float16, enabled=cuda)
            if cuda:
                torch.cuda.synchronize()
            started = time.monotonic()
            with torch.no_grad(), autocast, kernels:
                out = model(input_ids=ids, attention_mask=mask, labels=labels, fused_lm_head=True)
            if cuda:
                torch.cuda.synchronize()
        loss = float(-out.log_probs[out.label_mask].mean())
        return loss, round(time.monotonic() - started, 3)

    was_training = model.training
    model.eval()
    gqa_loss, gqa_seconds = run(False)
    repeat_loss, repeat_seconds = run(True)
    model.train(was_training)
    out = {"gqa_loss": gqa_loss, "repeat_kv_loss": repeat_loss, "gqa_seconds": gqa_seconds,
           "repeat_kv_seconds": repeat_seconds, "backend": str(backend)}  # fmt: skip
    if not math.isclose(gqa_loss, repeat_loss, rel_tol=1e-2, abs_tol=1e-3):
        raise SystemExit(f"repeated key/value heads change the loss: {out}")
    return out


def check_fused_head(model, example: dict, device) -> dict:
    """TRL's fused LM head against the full logits on one train example, under the autocast of
    training: the mean loss on the reply must agree. Catches a kernel that runs but is wrong on
    this GPU (the Triton tiles on a T4)."""
    import torch
    import torch.nn.functional as F  # noqa: N812

    ids = torch.tensor([example["input_ids"]], device=device)
    labels = torch.tensor([example["labels"]], device=device)
    mask = torch.ones_like(ids)
    autocast = torch.autocast(device.type, dtype=torch.float16, enabled=device.type == "cuda")
    was_training = model.training
    model.eval()
    with torch.no_grad(), autocast:
        fused = model(input_ids=ids, attention_mask=mask, labels=labels, fused_lm_head=True)
        logits = model(input_ids=ids, attention_mask=mask).logits[0, :-1]
        target = labels[0, 1:]
        keep = target != -100
        full = F.cross_entropy(logits[keep].float(), target[keep])
    model.train(was_training)
    fused_loss = float(-fused.log_probs[fused.label_mask].mean())
    full_loss = float(full)
    out = {"fused_loss": fused_loss, "full_logits_loss": full_loss,
           "tokens": int(keep.sum())}  # fmt: skip
    if not math.isclose(fused_loss, full_loss, rel_tol=1e-2, abs_tol=1e-3):
        raise FusedHeadError(f"fused LM head disagrees with the full logits: {out}")
    return out


def checked_fused_head(model, example: dict, device, check=check_fused_head) -> dict:
    """`check_fused_head`, and when the Triton tiles fail or disagree on this GPU, TRL's own
    torch tiles instead (`trl.kernels.chunked_logprob._TRITON_AVAILABLE`), checked again. Still
    no full logits: every position is scored, a vocabulary tile at a time."""
    import trl.kernels.chunked_logprob as chunked

    triton = bool(chunked._TRITON_AVAILABLE) and device.type == "cuda"
    try:
        return check(model, example, device) | {"triton": triton}
    except Exception as e:  # a Triton compile or launch error, or FusedHeadError
        if not triton:
            raise
        log(f"fused LM head with Triton failed, falling back to the torch tiles: {e!r}")
        chunked._TRITON_AVAILABLE = False
        return check(model, example, device) | {"triton": False, "triton_error": repr(e)[:1000]}


def make_callback(record: dict, out_dir: Path):
    import torch
    from transformers import TrainerCallback

    class Recorder(TrainerCallback):
        """Seconds and peak GPU memory of every optimizer step; stops on a NaN loss (§9)."""

        def __init__(self) -> None:
            self.started = None

        def on_step_begin(self, args, state, control, **kwargs):
            self.started = time.monotonic()
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()

        def on_step_end(self, args, state, control, **kwargs):
            step = {"step": state.global_step,
                    "seconds": round(time.monotonic() - self.started, 3)}  # fmt: skip
            if torch.cuda.is_available():
                step["max_allocated_gb"] = round(torch.cuda.max_memory_allocated() / 2**30, 3)
                step["max_reserved_gb"] = round(torch.cuda.max_memory_reserved() / 2**30, 3)
            record["steps"].append(step)

        def on_log(self, args, state, control, logs=None, **kwargs):
            logs = dict(logs or {})
            record["logs"].append({"step": state.global_step, **logs})
            loss = logs.get("loss", logs.get("eval_loss"))
            if loss is not None and not math.isfinite(loss):
                record["stopped_early"] = f"loss {loss} at step {state.global_step}"
                control.should_training_stop = True
            last = record["steps"][-1] if record["steps"] else {}
            log(f"step {state.global_step} {json.dumps(logs)} "
                f"{last.get('seconds')} s {last.get('max_allocated_gb')} GB")  # fmt: skip
            write_json(out_dir / "run.json", record)

    return Recorder()


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str) + "\n")
    tmp.replace(path)


def environment() -> dict:
    import torch

    gpus = []
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            p = torch.cuda.get_device_properties(i)
            gpus.append({"name": p.name, "capability": f"{p.major}.{p.minor}",
                         "memory_gb": round(p.total_memory / 2**30, 2)})  # fmt: skip
    smi = shutil.which("nvidia-smi")
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "torch_cuda": torch.version.cuda,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "gpus": gpus,
        "nvidia_smi": subprocess.run([smi], capture_output=True, text=True).stdout if smi else None,
    }


def make_trainer(model, tokenizer, config: dict, train_rows: list[dict], val_rows: list[dict],
                 output_dir: Path, *, gpu: bool, max_steps: int | None = None,
                 callbacks: list | None = None):  # fmt: skip
    """SFTTrainer with the frozen configuration (config.json, decision 18) on prompt-completion
    examples; the trainer tokenizes them and wraps the model with LoRA."""
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import set_seed
    from trl import SFTConfig, SFTTrainer

    set_seed(config["seed"])  # before PEFT draws the initial LoRA A matrices
    lora = config["lora"]
    peft_config = LoraConfig(r=lora["r"], lora_alpha=lora["alpha"], lora_dropout=lora["dropout"],
                             target_modules=lora["target_modules"], bias="none",
                             task_type="CAUSAL_LM")  # fmt: skip
    optimizer = config["optimizer"]
    sft_config = SFTConfig(
        output_dir=str(output_dir),
        num_train_epochs=config["epochs"],
        max_steps=max_steps or -1,
        per_device_train_batch_size=config["per_device_batch_size"],
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=config["gradient_accumulation_steps"],
        learning_rate=optimizer["learning_rate"],
        lr_scheduler_type=optimizer["scheduler"],
        warmup_steps=optimizer["warmup_ratio"],  # a float < 1 is a ratio in transformers 5
        max_length=config["max_seq_length"],
        packing=False,
        completion_only_loss=True,
        assistant_only_loss=False,
        gradient_checkpointing=config["gradient_checkpointing"],
        gradient_checkpointing_kwargs={"use_reentrant": False},
        fp16=gpu,
        bf16=False,
        use_cpu=not gpu,
        seed=config["seed"],
        data_seed=config["seed"],
        logging_steps=1,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_only_model=True,
        report_to="none",
        dataloader_num_workers=0,
    )
    columns = ("prompt", "completion")
    return SFTTrainer(
        model=model,
        args=sft_config,
        train_dataset=Dataset.from_list([{k: r[k] for k in columns} for r in train_rows]),
        eval_dataset=Dataset.from_list([{k: r[k] for k in columns} for r in val_rows]),
        processing_class=tokenizer,
        peft_config=peft_config,
        callbacks=callbacks or [],
    )


def train(args: argparse.Namespace, kernel: dict | None) -> int:
    started = time.time()
    on_kaggle = kernel is not None
    if args.out:
        out = Path(args.out).resolve()
    elif on_kaggle:
        out = KAGGLE_WORKING
    else:
        raise SystemExit("give --out")
    out.mkdir(parents=True, exist_ok=True)
    work = Path(args.work or ("/tmp/phase2" if on_kaggle else out / "work")).resolve()
    work.mkdir(parents=True, exist_ok=True)

    # 1. The S3 freeze, before anything else.
    if kernel:
        config_text, lock_text, frozen = kernel["config"], kernel["requirements_lock"], \
            kernel["frozen"]  # fmt: skip
    else:
        config_text = Path(args.config).read_text()
        lock_text = (HERE / "requirements.lock").read_text()
        frozen = frozen_hashes(json.loads(Path(args.freeze).read_text()))
    data = Path(args.data) if args.data else find_kaggle_data()
    check_freeze(data, config_text, frozen)
    config = json.loads(config_text)
    model_config = config["models"][args.model]
    log(f"S3 freeze holds: {data} and config.json")

    # 2. Environment: one GPU (Kaggle's T4 shape has two), the locked packages.
    if on_kaggle:
        os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    removed = []
    if args.install:
        log("pip install requirements.lock")
        install(lock_text)
        removed = remove_incompatible_backends()
        log(f"removed incompatible optional backends: {removed}")
    versions = check_versions(lock_text)

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    gpu = torch.cuda.is_available() and not args.cpu
    if gpu and torch.cuda.device_count() != 1:
        raise SystemExit(f"{torch.cuda.device_count()} GPUs visible, one expected")
    record = {
        "script_sha256": kernel["source_sha256"] if kernel else sha256(HERE / "train_lora.py"),
        "kernel": None if kernel is None else {k: v for k, v in kernel.items()
                                               if k not in ("config", "requirements_lock")},
        "args": vars(args),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "versions": versions,
        "removed_packages": removed,
        "environment": environment(),
        "data": {"path": str(data), "sha256": frozen["sft.jsonl"]},
        "config_sha256": frozen["config.json"],
        "config": config,
        "steps": [],
        "logs": [],
        "stopped_early": None,
    }  # fmt: skip
    write_json(out / "run.json", record)

    # 3. Data and base model.
    train_rows, val_rows = load_examples(data, args.limit)
    record["data"] |= {"train": len(train_rows), "val": len(val_rows)}
    revision = args.revision or REVISIONS[args.model]
    if args.base:
        base = Path(args.base)
        files = {p.name: sha256(p) for p in sorted(base.iterdir()) if p.is_file()}
    else:
        base = work / f"base-{args.model}"
        log(f"downloading {model_config['hf_repo']}@{revision}")
        files = download_base(model_config["hf_repo"], revision, base)
    record["model"] = {"key": args.model, "hf_repo": model_config["hf_repo"],
                       "revision": None if args.base else revision, "path": str(base),
                       "files": files}  # fmt: skip
    tokenizer = AutoTokenizer.from_pretrained(base)
    template_sha = sha256_text(tokenizer.chat_template)
    if template_sha != CHAT_TEMPLATE_SHA256:
        raise SystemExit(f"chat template {template_sha} is not the pinned one")
    dtype = torch.float16 if gpu else torch.float32
    model = AutoModelForCausalLM.from_pretrained(base, dtype=dtype, attn_implementation="sdpa")
    if gpu:
        model.to("cuda")

    trainer = make_trainer(model, tokenizer, config, train_rows, val_rows, work / "trainer",
                           gpu=gpu, max_steps=args.max_steps,
                           callbacks=[make_callback(record, out)])  # fmt: skip
    sft_config = trainer.args
    trainable = sum(p.numel() for p in trainer.model.parameters() if p.requires_grad)
    record["parameters"] = {
        "trainable": trainable,
        "total": sum(p.numel() for p in trainer.model.parameters()),
        "trainable_dtypes": sorted({str(p.dtype) for p in trainer.model.parameters()
                                    if p.requires_grad}),
    }  # fmt: skip
    record["sft_config"] = sft_config.to_dict()

    # 4. Checks of what the trainer will see, then train.
    record["tokenized"] = {
        "train": check_tokenized(train_rows, trainer.train_dataset, tokenizer,
                                 config["max_seq_length"]),
        "val": check_tokenized(val_rows, trainer.eval_dataset, tokenizer,
                               config["max_seq_length"]),
    }  # fmt: skip
    log(f"tokenized: {json.dumps(record['tokenized'])}")
    device = torch.device("cuda" if gpu else "cpu")
    if gpu and torch.cuda.get_device_capability() < (8, 0):
        from torch.nn.attention import SDPBackend

        record["attention"] = check_attention(trainer.model, trainer.train_dataset[0], device,
                                              SDPBackend.EFFICIENT_ATTENTION)  # fmt: skip
        log(f"attention with repeated key/value heads: {record['attention']}")
    else:
        record["attention"] = None  # PyTorch's own choice of kernel, enable_gqa included
    record["fused_head_check"] = checked_fused_head(trainer.model, trainer.train_dataset[0],
                                                    device)  # fmt: skip
    log(f"fused LM head check: {record['fused_head_check']}")
    record["eval_before"] = trainer.evaluate()
    write_json(out / "run.json", record)
    log(f"eval before training: {record['eval_before']}")
    result = trainer.train()
    record["train_result"] = result.metrics
    if args.max_steps:
        record["eval_after"] = trainer.evaluate()
    record["log_history"] = trainer.state.log_history
    write_json(out / "run.json", record)

    # 5. Adapters: one per epoch, or the last step of a short run.
    adapters = out / "adapters"
    adapters.mkdir(exist_ok=True)
    kept = []
    if args.max_steps:
        name = f"{args.model}-s{trainer.state.global_step}"
        trainer.save_model(str(adapters / name))
        kept.append({"name": name, "step": trainer.state.global_step,
                     "epoch": trainer.state.epoch})  # fmt: skip
    else:
        checkpoints = sorted((work / "trainer").glob("checkpoint-*"),
                             key=lambda p: int(p.name.split("-")[1]))  # fmt: skip
        for checkpoint in checkpoints:
            state = json.loads((checkpoint / "trainer_state.json").read_text())
            name = f"{args.model}-e{round(state['epoch'])}"
            (adapters / name).mkdir(exist_ok=True)
            for f in ("adapter_config.json", "adapter_model.safetensors"):
                shutil.copy2(checkpoint / f, adapters / name / f)
            kept.append({"name": name, "step": state["global_step"], "epoch": state["epoch"]})
    for k in kept:
        k["adapter_sha256"] = sha256(adapters / k["name"] / "adapter_model.safetensors")
    record["checkpoints"] = kept
    write_json(out / "run.json", record)

    if not args.no_convert:
        llama = fetch_llama_cpp(Path(args.llama_cpp) if args.llama_cpp else work / "llama.cpp")
        record["llama_cpp"] = llama
        gguf_dir = out / "gguf"
        gguf_dir.mkdir(exist_ok=True)
        for k in kept:
            target = gguf_dir / f"{k['name']}.gguf"
            convert_to_gguf(adapters / k["name"], base, target, Path(llama["path"]))
            k["gguf"] = f"gguf/{target.name}"
            k["gguf_sha256"] = sha256(target)
            k["gguf_bytes"] = target.stat().st_size

    freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True,
                            text=True).stdout  # fmt: skip
    (out / "pip-freeze.txt").write_text(freeze)
    record["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    record["wall_seconds"] = round(time.time() - started, 1)
    write_json(out / "run.json", record)
    log(f"done in {record['wall_seconds'] / 60:.1f} min: {[k['name'] for k in kept]}")
    return 0


# ---- local preparation -----------------------------------------------------------------------


def prepare_dataset(args: argparse.Namespace) -> int:
    """sft.jsonl (train and val only: R6) and dataset-metadata.json, for `kaggle datasets
    create`."""
    frozen = frozen_hashes(json.loads(Path(args.freeze).read_text()))
    if sha256(args.data) != frozen["sft.jsonl"]:
        raise SystemExit(f"{args.data} does not match the S3 freeze")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.data, out / "sft.jsonl")
    meta = {"title": args.title, "id": args.dataset, "licenses": [{"name": "unknown"}]}
    (out / "dataset-metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {out}: sft.jsonl ({frozen['sft.jsonl'][:12]}…), dataset-metadata.json")
    return 0


def prepare(args: argparse.Namespace) -> int:
    """The kernel folder: this file with KERNEL set, and kernel-metadata.json."""
    config_text = (HERE / "config.json").read_text()
    lock_text = (HERE / "requirements.lock").read_text()
    frozen = frozen_hashes(json.loads(Path(args.freeze).read_text()))
    check_freeze(Path(args.data), config_text, frozen)
    argv = ["train", "--model", args.model, "--install"]
    if args.max_steps:
        argv += ["--max-steps", str(args.max_steps)]
    source = (HERE / "train_lora.py").read_text()
    kernel = {
        "argv": argv,
        "source_sha256": sha256_text(source),
        "prepared": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "frozen": frozen,
        "config": config_text,
        "requirements_lock": lock_text,
        "requirements_lock_sha256": sha256_text(lock_text),
    }
    marker = "KERNEL: dict | None = None\n"
    if source.count(marker) != 1:
        raise SystemExit("the KERNEL line is not found once")
    baked = source.replace(marker, f"KERNEL = json.loads({json.dumps(kernel)!r})\n")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "train_lora.py").write_text(baked)
    meta = json.loads((HERE / "kernel-metadata.json").read_text())
    (out / "kernel-metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {out}: train_lora.py ({' '.join(argv)}), kernel-metadata.json ({meta['id']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None and KERNEL is not None:
        argv = KERNEL["argv"]
    parser = argparse.ArgumentParser(prog="train_lora.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("train", help="LoRA training (Kaggle; local CPU checks)")
    p.add_argument("--model", choices=sorted(REVISIONS), required=True)
    p.add_argument("--max-steps", type=int, help="a short run (pilot): stop after N steps")
    p.add_argument("--data", type=Path, help="sft.jsonl (default: the one under /kaggle/input)")
    p.add_argument("--config", type=Path, default=HERE / "config.json")
    p.add_argument("--freeze", type=Path, default=REPO / "data/train/freeze.json")
    p.add_argument("--base", type=Path, help="local base model (default: the pinned revision)")
    p.add_argument("--revision", help="override the pinned revision (never for the real runs)")
    p.add_argument("--out", type=Path, help="default: /kaggle/working on Kaggle")
    p.add_argument("--work", type=Path, help="scratch: base model, checkpoints, llama.cpp")
    p.add_argument("--limit", type=int, help="first N examples of each split (local checks)")
    p.add_argument("--install", action="store_true", help="pip install requirements.lock first")
    p.add_argument("--llama-cpp", type=Path, help="a llama.cpp b12032 tree (default: clone)")
    p.add_argument("--no-convert", action="store_true", help="no GGUF conversion")
    p.add_argument("--cpu", action="store_true", help="train on CPU even with a GPU")

    for name in ("prepare", "prepare-dataset"):
        p = sub.add_parser(name)
        p.add_argument("--data", type=Path, default=REPO / "data/train/sft.jsonl")
        p.add_argument("--freeze", type=Path, default=REPO / "data/train/freeze.json")
        p.add_argument("--out", type=Path, required=True)
    p = sub.choices["prepare"]
    p.add_argument("--model", choices=sorted(REVISIONS), required=True)
    p.add_argument("--max-steps", type=int)
    p = sub.choices["prepare-dataset"]
    p.add_argument("--dataset", default="aohkvd/eth-nl2sql-phase2-sft")
    p.add_argument("--title", default="eth-nl2sql phase2 sft")

    args = parser.parse_args(argv)
    if args.command == "prepare":
        return prepare(args)
    if args.command == "prepare-dataset":
        return prepare_dataset(args)
    return train(args, KERNEL)


if __name__ == "__main__":
    # No SystemExit on success: IPython (a Kaggle kernel) would report it as an exception.
    if code := main():
        sys.exit(code)
