import os
import sys

import pytest

# Ensure repository root on sys.path for test discovery environments
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# `trainer/` depends on the heavyweight HuggingFace `datasets` package, which
# is an optional training-time dep, not declared in pyproject. Skip the whole
# module when it's not installed so CI (and contributors who haven't synced
# the trainer extras) don't see a collection error on every push.
pytest.importorskip("datasets")

from trainer.train_adapter import build_examples


class FakeTokenizer:
    """A tiny fake tokenizer to validate masking behavior without network calls."""

    def __init__(self):
        self.eos_token = "<|eos|>"

    def __call__(self, text, truncation=True, max_length=None):
        # simple whitespace tokenization -> token id list
        toks = text.split()
        ids = list(range(1, len(toks) + 1))
        return {"input_ids": ids}


def test_build_examples_masks_prompt_tokens():
    tokenizer = FakeTokenizer()
    prompt = "why buy now"
    target = {"outcome": "WIN"}
    batch = {"prompt": [prompt], "target": [target]}

    out = build_examples(batch, tokenizer, max_input_length=16, max_target_length=8)
    assert "input_ids" in out and "labels" in out
    input_ids = out["input_ids"][0]
    labels = out["labels"][0]
    assert len(input_ids) == len(labels)

    prompt_text = "CONTEXT:\n" + prompt.strip() + "\n\nRESPONSE: "
    prompt_ids = tokenizer(prompt_text)["input_ids"]
    # first len(prompt_ids) tokens in labels must be masked (-100)
    for i in range(len(prompt_ids)):
        assert labels[i] == -100
    # remaining tokens should not be masked
    assert any(l != -100 for l in labels[len(prompt_ids):])
