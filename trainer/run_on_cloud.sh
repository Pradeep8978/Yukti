#!/usr/bin/env bash
# Run training on a cheap cloud GPU (Vast.ai / RunPod / E2E)
# Usage: ./trainer/run_on_cloud.sh --since 2025-01-01 --model "decapoda-research/llama-7b-hf" --dry_run
# This script packages code + data and prints exact commands to run on a cloud instance.
# It does NOT perform cloud instance creation for you — follow provider UI or CLI to start a GPU node.

set -euo pipefail
SCRIPTDIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPTDIR/.." && pwd)
OUTDIR=${ROOT}/tmp_cloud_package
DATA_OUT=${ROOT}/data/training/journal_export.jsonl
SINCE=""
MODEL="facebook/opt-125m"
MIN_JOURNALS=50
MIN_QUALITY=6
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --min-journals) MIN_JOURNALS="$2"; shift 2;;
    --min-quality) MIN_QUALITY="$2"; shift 2;;
    --dry_run) DRY_RUN=1; shift 1;;
    *) echo "Unknown arg $1"; exit 1;;
  esac
done

mkdir -p "$OUTDIR"

# 1) Export latest journals from Postgres into JSONL
echo "Exporting journals to $DATA_OUT (since=$SINCE)"
python3 scripts/export_training_data.py --out "$DATA_OUT" ${SINCE:+--since $SINCE}

# Quick sanity check: count high-quality journals
echo "Checking exported quality (min_journals=${MIN_JOURNALS}, min_quality=${MIN_QUALITY})"
QUALITY_COUNT=$(env MIN_QUALITY="$MIN_QUALITY" python3 - <<'PY'
import json,os
min_q=float(os.environ.get('MIN_QUALITY', '6'))
cnt=0
path='data/training/journal_export.jsonl'
try:
  with open(path, 'r', encoding='utf-8') as fh:
    for ln in fh:
      try:
        j=json.loads(ln)
      except Exception:
        continue
      q=j.get('quality_score')
      if q is None:
        # support nested structures for legacy exports
        q=j.get('structured_data', {}) and j.get('structured_data', {}).get('quality_score')
      try:
        if q is not None and float(q) >= min_q:
          cnt+=1
      except Exception:
        continue
except Exception:
  pass
print(cnt)
PY
)
echo "High-quality journal rows: $QUALITY_COUNT"
if [ "$QUALITY_COUNT" -lt "$MIN_JOURNALS" ]; then
  echo "ERROR: Not enough high-quality journals for training (have=$QUALITY_COUNT, need=$MIN_JOURNALS). Aborting." >&2
  exit 2
fi

# 2) Package code + trainer into tarball for upload
PKG="$OUTDIR/yukti_trainer_package.tar.gz"
rm -f "$PKG"

# Include trainer, scripts, requirements, and export
tar -czf "$PKG" \
  trainer scripts data/training/journal_export.jsonl trainer/requirements.txt trainer/README.md || true

echo "Created package: $PKG"

echo
echo "=== CLOUD RUN INSTRUCTIONS (copy-paste on your GPU node) ==="
cat <<'EOF'
# On the GPU instance (Ubuntu 22.04 / 24.04), run:

sudo apt update && sudo apt install -y python3-pip git wget build-essential libsndfile1
python3 -m pip install --upgrade pip
python3 -m pip install -r trainer/requirements.txt

# Optional: create venv
# python3 -m venv venv; . venv/bin/activate

# Preprocess dataset (tokenize optional)
python3 trainer/preprocess.py --input data/training/journal_export.jsonl --out_dir data/training/processed --val_frac 0.05 --test_frac 0.05 --dry_run

# If dry_run summary is OK, run full preprocess
python3 trainer/preprocess.py --input data/training/journal_export.jsonl --out_dir data/training/processed --val_frac 0.05 --test_frac 0.05

# Run training (example: QLoRA on 7B-like using bitsandbytes + PEFT)
# Adjust --model to a 7B/8B QLoRA-friendly HF model available on the instance
python3 trainer/train_adapter.py --data data/training/processed/train.jsonl --model "<MODEL_ID>" --out_dir models/lora-journal --epochs 3 --batch_size 4 --use_peft auto --fp16 --gradient_checkpointing

# After training: evaluate vs baseline
python3 trainer/evaluate_vs_baseline.py --adapter_dir models/lora-journal --base_model "<MODEL_ID>" --out_dir artifacts/eval

# Merge PEFT adapter with base model (if PEFT used) for deployment
python - <<PY
from peft import PeftModel
from transformers import AutoModelForCausalLM
model = AutoModelForCausalLM.from_pretrained("<MODEL_ID>")
peft = PeftModel.from_pretrained(model, "models/lora-journal")
merged = peft.merge_and_unload()
merged.save_pretrained("models/merged")
PY

# Convert to GGUF for Ollama / local deployment using recommended converters.
# Example: use the 'gguf' converter from the llama.cpp or guidance projects (install separately)
# The exact tool depends on base model family — follow its converter README.

# Example GGUF conversion steps (model-family specific):
# 1) Ensure merged HF model is saved to models/merged (see merge step above)
# 2) For LLaMA-family models, use llama.cpp converter:
#    git clone https://github.com/ggerganov/llama.cpp && cd llama.cpp
#    make
#    python3 llama.cpp/python/convert.py --model_type llama --input_dir ../models/merged --output ../models/merged.gguf
# 3) For other families, follow their recommended GGUF conversion toolchain.

EOF

echo
if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY RUN: packaged and printed cloud commands. Upload $PKG to your cloud node and follow the printed steps."
else
  echo "Package ready: $PKG"
  echo "Upload this tarball to your cloud node (scp/s3) and extract before running the commands above."
fi

# Print estimated cost guidance
cat <<COST

Estimated example costs (April 2026):
- Vast.ai (spot-style): 1× A100 40GB ~ ₹600-₹1200 / hour (spot), good for QLoRA on 7B/13B - expect ₹600-₹1800 per 1-3 hour job depending on model + data.
- RunPod (on-demand GPU): 1× A100 40GB ~ ₹900-₹2000 / hour.
- E2E Networks: similar pricing; check hourly spot offerings.

Practical plan within budget (₹2000/month):
- Run QLoRA/QLoRA-style fine-tune 1-2× month on a 7B model: use 1-2 hours per run on A100 40GB (₹700-₹1500/hr) → ₹1400-₹3000 (may exceed budget if run often).
- To stay < ₹2000: prefer smaller models (opt-125m / 350m) or use shorter runs (1 hour), and only train when you have >=50 high-quality journals.

COST

echo "Run-on-cloud packaging complete."
chmod +x "$OUTDIR" || true

exit 0
