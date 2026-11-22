"""The four configurations the app serves, and where their frozen runs are.

Phase 1 is the base model alone (prompting), phase 2 the same base model with the epoch-2 LoRA
adapter chosen on dev (phase2-plan.md §5.3). Each runs in its own llama-server process, as its
frozen runs did (api/llama.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from eth_nl2sql import paths

MODELS_DIR = Path("models")
SPLITS = ("dev", "test")

# size -> (base GGUF stem, adapter stem chosen on dev, phase2-plan.md §5.3)
BASES = {
    "3b": ("qwen2.5-coder-3b-instruct-q4_k_m", "3b-e2"),
    "1.5b": ("qwen2.5-coder-1.5b-instruct-q4_k_m", "1.5b-e2"),
}
SIZE_LABELS = {"3b": "3B", "1.5b": "1.5B"}


@dataclass(frozen=True)
class ModelConfig:
    id: str
    size: str  # 3b or 1.5b
    phase: int  # 1: base model, prompting; 2: base model + LoRA adapter
    base: Path
    adapter: Path
    predictions_dir: Path

    @property
    def label(self) -> str:
        name = f"Qwen2.5-Coder {SIZE_LABELS[self.size]}"
        return name if self.phase == 1 else f"{name} + LoRA"

    @property
    def description(self) -> str:
        if self.phase == 1:
            return "Phase 1 · prompting, model gốc"
        return f"Phase 2 · fine-tune LoRA ({self.adapter.stem})"

    @property
    def run_tag(self) -> str:
        """The model part of the frozen run's file name (generate-sql's naming)."""
        return self.base.stem if self.phase == 1 else f"{self.base.stem}.{self.adapter.stem}"

    def run_file(self, split: str) -> Path:
        """The frozen run of the default pipeline on `split` (no ablation)."""
        folder = self.predictions_dir / f"phase{self.phase}" / split
        return folder / f"{split}.predictions.prompts.{self.run_tag}.jsonl"

    def report_file(self, split: str) -> Path:
        run = self.run_file(split)
        return run.with_name(run.name.removesuffix(".jsonl") + ".ex.json")

    def details_file(self, split: str) -> Path:
        run = self.run_file(split)
        return run.with_name(run.name.removesuffix(".jsonl") + ".ex-details.jsonl")

    def missing_files(self) -> list[str]:
        files = (self.base, self.adapter) if self.phase == 2 else (self.base,)
        return [str(p) for p in files if not p.is_file()]

    def ex(self, split: str) -> dict | None:
        """{ex, correct, questions} of the frozen run, or None without its report."""
        path = self.report_file(split)
        if not path.is_file():
            return None
        overall = json.loads(path.read_text())["overall"]
        return {k: overall[k] for k in ("ex", "correct", "questions")}

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "size": self.size,
            "phase": self.phase,
            "base": self.base.name,
            "adapter": self.adapter.stem if self.phase == 2 else None,
            "missing": self.missing_files(),
            "ex": {split: self.ex(split) for split in SPLITS},
        }


def registry(
    models_dir: Path = MODELS_DIR, predictions_dir: Path = paths.PREDICTIONS_DIR
) -> list[ModelConfig]:
    """3B + LoRA first: the best model on test (EX 0.814) is the default of the page."""
    out = []
    for size, (base, adapter) in BASES.items():
        for phase in (2, 1):
            out.append(
                ModelConfig(
                    id=f"{size}-lora" if phase == 2 else size,
                    size=size,
                    phase=phase,
                    base=models_dir / f"{base}.gguf",
                    adapter=models_dir / "lora" / f"{size}-run" / "gguf" / f"{adapter}.gguf",
                    predictions_dir=predictions_dir,
                )
            )
    return out
