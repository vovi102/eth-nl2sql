"""Fixtures of the training checks: the real data, the pinned tokenizer, a tiny Qwen2 model.

Run from eth-nl2sql/:  uv run --project kaggle/phase2-train pytest kaggle/phase2-train/tests
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TRAIN_DIR = HERE.parent
REPO = TRAIN_DIR.parent.parent
sys.path.insert(0, str(TRAIN_DIR))

SFT = REPO / "data/train/sft.jsonl"
FREEZE = REPO / "data/train/freeze.json"
# The tokenizer and config of the pinned 1.5B revision (11 MB, no weights):
#   models/hf/qwen2.5-coder-1.5b-instruct/ from huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct
#   at train_lora.REVISIONS["1.5b"]; README "Fine-tune LoRA (phase 2)" has the command.
TOKENIZER_DIR = REPO / "models/hf/qwen2.5-coder-1.5b-instruct"
LLAMA_CPP = Path.home() / "opt/llama.cpp/src-b12032"
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
                   "generation_config.json")  # fmt: skip


def phase1_meta(size: str) -> Path:
    """The meta file of the phase-1 dev run of 1.5b or 3b: it keeps the GGUF's chat template."""
    name = f"dev.predictions.prompts.qwen2.5-coder-{size}-instruct-q4_k_m.jsonl.meta.json"
    return REPO / "data/predictions" / name


def need(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"{path} is missing")


@pytest.fixture(scope="session")
def tokenizer():
    need(TOKENIZER_DIR)
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(TOKENIZER_DIR)


@pytest.fixture(scope="session")
def tiny_base(tmp_path_factory) -> Path:
    """A 2-layer Qwen2 with random weights and the real tokenizer: the shapes and names of the
    real model, so that TRL, PEFT and convert_lora_to_gguf.py take the same paths."""
    need(TOKENIZER_DIR)
    import torch
    from transformers import Qwen2Config, Qwen2ForCausalLM

    config = Qwen2Config.from_pretrained(
        TOKENIZER_DIR, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=2, max_window_layers=2,
        layer_types=["full_attention"] * 2,
    )  # fmt: skip
    torch.manual_seed(0)
    out = tmp_path_factory.mktemp("tiny-qwen2")
    Qwen2ForCausalLM(config).save_pretrained(out)
    for name in TOKENIZER_FILES:
        shutil.copy2(TOKENIZER_DIR / name, out / name)
    return out
