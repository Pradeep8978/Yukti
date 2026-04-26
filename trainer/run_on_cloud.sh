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
POSTGRES_URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --min-journals) MIN_JOURNALS="$2"; shift 2;;
    --min-quality) MIN_QUALITY="$2"; shift 2;;
    --postgres-url) POSTGRES_URL="$2"; shift 2;;
    --dry_run) DRY_RUN=1; shift 1;;
    *) echo "Unknown arg $1"; exit 1;;
  esac
done

mkdir -p "$OUTDIR"

# 1) Export latest journals from Postgres into JSONL
echo "Exporting journals to $DATA_OUT (since=$SINCE)"
if [ -n "$POSTGRES_URL" ]; then
  echo "Using external Postgres at POSTGRES_URL (supplied)"
  env POSTGRES_URL="$POSTGRES_URL" python3 scripts/export_training_data.py --out "$DATA_OUT" ${SINCE:+--since $SINCE}
else
  python3 scripts/export_training_data.py --out "$DATA_OUT" ${SINCE:+--since $SINCE}
fi

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
# If your Postgres is external, set POSTGRES_URL on the node before running the exporter:
#  export POSTGRES_URL="postgresql+psycopg://user:pass@host:5432/dbname"
python3 trainer/preprocess.py --input data/training/journal_export.jsonl --out_dir data/training/processed --val_frac 0.05 --test_frac 0.05 --dry_run

# If dry_run summary is OK, run full preprocess (on-node exporter will use POSTGRES_URL if set)
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

Estimated example costs (April 2026) and quickstarts:

Vast.ai (spot style):
  1) Create an account on Vast.ai and search for an "A100 40GB" instance with Ubuntu 22.04.
  2) Choose a 1–2 hour lease to control cost, set SSH key, and deploy.
  3) Upload `yukti_trainer_package.tar.gz` via `scp` or `curl` from object storage.
  4) Run the commands printed by this script (install deps, preprocess, train).

RunPod (on-demand):
  1) Create a RunPod account and spin up a pod with Ubuntu and the desired GPU (choose 4GB+ VRAM based on model).
  2) Use `scp` or upload the tarball to the pod. Run the same commands below.

E2E Networks (or similar providers):

Practical per-run budget guidance (aim: ≤ ₹1,000):

Quick safety notes:
- Avoid embedding secrets in the package. Set `POSTGRES_URL` as an environment variable on the GPU node instead of adding to the tarball.
- Run with `--dry_run` locally to validate preprocessing and counts before committing to a long/cloud run.


Cost-optimized plan examples:
- Low-cost fine-tune (recommended): `opt-125m` or `opt-350m` on a cheap 1-2 hour slot — expected ≈ ₹150–₹600.
- Medium: QLoRA on a 7B model for 1 hour on A100-40GB — expected ≈ ₹600–₹1,200 (spot-dependent).
- Heavy: 13B+ for several hours — likely > ₹1,500 and not recommended within the ₹2,000/month budget.

COST

echo "Run-on-cloud packaging complete."
chmod +x "$OUTDIR" || true

exit 0
