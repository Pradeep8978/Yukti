#!/usr/bin/env bash
# ============================================================
# run_on_cloud.sh — Yukti Cloud Training Packager
# ============================================================
# Packages trainer code + high-quality journals, runs safety
# checks, and prints copy-paste commands for GPU cloud runs.
#
# Usage:
#   ./trainer/run_on_cloud.sh [OPTIONS]
#
# Options:
#   --since DATE        Export journals created after DATE (YYYY-MM-DD)
#   --model MODEL_ID    HuggingFace model ID for training
#                       Default: unsloth/Phi-3-mini-4k-instruct  (~3.8B, fits 8GB VRAM)
#   --provider PROV     Cloud provider hint in output: vastai | runpod | e2e
#   --min-journals N    Minimum high-quality journals required (default: 50)
#   --min-quality Q     Minimum quality_score to count (default: 6)
#   --postgres-url URL  External Postgres URL (or set POSTGRES_URL env var)
#   --dry-run           Validate only — do not write tarball
#
# Examples:
#   ./trainer/run_on_cloud.sh --since 2026-01-01 --dry-run
#   ./trainer/run_on_cloud.sh --since 2026-01-01 --provider runpod
# ============================================================

set -euo pipefail

SCRIPTDIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPTDIR/.." && pwd)
OUTDIR="${ROOT}/tmp_cloud_package"
DATA_OUT="${ROOT}/data/training/journal_export.jsonl"

SINCE=""
# Phi-3-mini: 3.8B params, 4-bit QLoRA fits on 8 GB VRAM (cheapest GPU tier)
MODEL="unsloth/Phi-3-mini-4k-instruct"
PROVIDER="vastai"
MIN_JOURNALS=50
MIN_QUALITY=6
DRY_RUN=0
POSTGRES_URL="${POSTGRES_URL:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)        SINCE="$2";        shift 2 ;;
    --model)        MODEL="$2";        shift 2 ;;
    --provider)     PROVIDER="$2";     shift 2 ;;
    --min-journals) MIN_JOURNALS="$2"; shift 2 ;;
    --min-quality)  MIN_QUALITY="$2";  shift 2 ;;
    --postgres-url) POSTGRES_URL="$2"; shift 2 ;;
    --dry-run)      DRY_RUN=1;         shift 1 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

echo "========================================================"
echo " Yukti Cloud Training Packager"
echo "  Model    : $MODEL"
echo "  Provider : $PROVIDER"
echo "  Since    : ${SINCE:-all time}"
echo "  Dry run  : $DRY_RUN"
echo "========================================================"
echo

mkdir -p "$OUTDIR"
mkdir -p "$(dirname "$DATA_OUT")"

# ── Step 1: Export high-quality journals from Postgres ───────
echo "[1/4] Exporting journals -> $DATA_OUT"
if [[ -n "$POSTGRES_URL" ]]; then
  echo "      Using POSTGRES_URL from environment / --postgres-url flag"
else
  echo "      Using DATABASE_URL / .env settings"
fi

SINCE_FLAG=""
[[ -n "$SINCE" ]] && SINCE_FLAG="--since $SINCE"

POSTGRES_URL="$POSTGRES_URL" python3 scripts/export_training_data.py \
    --out "$DATA_OUT" \
    --min-quality "$MIN_QUALITY" \
    ${SINCE_FLAG}

# ── Step 2: Quality gate ──────────────────────────────────────
echo
echo "[2/4] Checking journal quality (need >= $MIN_JOURNALS entries with score >= $MIN_QUALITY)"

QUALITY_COUNT=$(MIN_QUALITY="$MIN_QUALITY" python3 - <<'PY'
import json, os
min_q = float(os.environ.get("MIN_QUALITY", "6"))
path  = "data/training/journal_export.jsonl"
cnt   = 0
try:
    with open(path, "r", encoding="utf-8") as fh:
        for ln in fh:
            try:
                j = json.loads(ln)
            except Exception:
                continue
            q = j.get("quality_score")
            if q is None:
                sd = j.get("structured_data") or {}
                q  = sd.get("quality_score") if isinstance(sd, dict) else None
            try:
                if q is not None and float(q) >= min_q:
                    cnt += 1
            except Exception:
                continue
except FileNotFoundError:
    pass
print(cnt)
PY
)

echo "      High-quality journals found: $QUALITY_COUNT (need: $MIN_JOURNALS)"

if [[ "$QUALITY_COUNT" -lt "$MIN_JOURNALS" ]]; then
  echo
  echo "ERROR: Not enough high-quality journals for a meaningful training run." >&2
  echo "       Have $QUALITY_COUNT, need $MIN_JOURNALS." >&2
  echo "       Suggestions:" >&2
  echo "         - Keep trading in paper mode for more closed trades" >&2
  echo "         - Lower --min-journals (not recommended below 30)" >&2
  echo "         - Lower --min-quality (not recommended below 5)" >&2
  echo "         - Expand --since range to include older data" >&2
  exit 2
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "DRY RUN complete -- $QUALITY_COUNT journals pass quality gate."
  echo "Run without --dry-run to create the upload package."
  exit 0
fi

# ── Step 3: Package code + data ───────────────────────────────
echo
echo "[3/4] Creating upload package -> $OUTDIR/yukti_trainer_package.tar.gz"
PKG="$OUTDIR/yukti_trainer_package.tar.gz"
rm -f "$PKG"

tar -czf "$PKG" \
  trainer/ \
  scripts/export_training_data.py \
  data/training/journal_export.jsonl \
  --exclude="trainer/__pycache__" \
  --exclude="trainer/*.pyc"

PKG_SIZE=$(du -sh "$PKG" | cut -f1)
echo "      Package: $PKG ($PKG_SIZE)"

# ── Step 4: Print provider-specific + common instructions ────
echo
echo "[4/4] Cloud run instructions for: $PROVIDER"
echo

case "$PROVIDER" in
vastai)
cat <<'VASTAI'

=== VAST.AI QUICK-START (April 2026 pricing) ===

COST ESTIMATES (Spot / On-Demand):
  GPU           | VRAM  | Spot INR/hr | Est. cost (total)
  --------------|-------|-------------|-------------------
  RTX 3090      | 24 GB | INR 50-80   | INR 125-300   <- recommended
  RTX 4090      | 24 GB | INR 80-120  | INR 120-270
  A100 40 GB    | 40 GB | INR 180-260 | INR 180-360
  H100 80 GB    | 80 GB | INR 320-480 | INR 215-480

HOW TO START:
  1. Register at https://vast.ai (top up $5 = ~INR 415)
  2. Click "Find" -> filter:
       Template : PyTorch (Ubuntu 22.04 + CUDA 12.x)
       GPU type : RTX 3090 or RTX 4090
       Disk     : 40 GB+
       Sort by  : $/hr ascending (pick spot instances)
  3. "Rent" -> add SSH public key -> "Create"
  4. SSH in:  ssh -p <PORT> root@<HOST>
  5. Upload package:
       scp -P <PORT> /path/to/yukti_trainer_package.tar.gz root@<HOST>:/workspace/
VASTAI
  ;;
runpod)
cat <<'RUNPOD'

=== RUNPOD QUICK-START (April 2026 pricing) ===

COST ESTIMATES (Secure / Community Cloud):
  GPU           | VRAM  | Community INR/hr | Est. cost
  --------------|-------|------------------|----------
  RTX 3090      | 24 GB | INR 55-80        | INR 140-200  <- recommended
  RTX 4090      | 24 GB | INR 80-110       | INR 120-240
  A100 SXM 80G  | 80 GB | INR 400-500      | INR 400-500

HOW TO START:
  1. Register at https://runpod.io
  2. "Deploy" -> "GPU Pod"
       Template : RunPod PyTorch (latest CUDA)
       GPU      : RTX 3090 Community (cheapest)
       Volume   : 40 GB network storage
  3. "Deploy On-Demand" -> wait ~1 min for pod to start
  4. "Connect" -> Web Terminal  OR copy SSH command
  5. Upload via runpodctl CLI:
       pip install runpod
       runpodctl send /path/to/yukti_trainer_package.tar.gz
       # OR: store on Cloudflare R2 / S3 and wget inside pod
RUNPOD
  ;;
e2e)
cat <<'E2E'

=== E2E NETWORKS QUICK-START — India Native (April 2026) ===

WHY E2E: INR billing + GST invoice, data stays in India,
         low latency to your prod DB if hosted in same region.

COST ESTIMATES:
  GPU Instance    | VRAM  | INR/hr | Est. cost
  ----------------|-------|--------|----------
  GPU.A100.40G    | 40 GB | INR 140 | INR 140-180  <- recommended
  GPU.A100.80G    | 80 GB | INR 220 | INR 165-220
  GPU.V100.16G    | 16 GB | INR 65  | INR 230-280
  GPU.RTX3090.24G | 24 GB | INR 70  | INR 175-225

HOW TO START:
  1. Login: https://myaccount.e2enetworks.com
     -> Compute -> GPU Instances -> Create Instance
  2. Choose:
       Image   : Ubuntu 22.04 CUDA 12.x (from catalog)
       Flavor  : GPU.A100.40G
       Disk    : 50 GB SSD
       Region  : Delhi / Mumbai (closest to prod DB)
  3. Add SSH public key during provisioning
  4. SSH: ssh ubuntu@<INSTANCE-IP>
  5. Upload:
       scp /path/to/yukti_trainer_package.tar.gz ubuntu@<INSTANCE-IP>:/home/ubuntu/

  After training, archive GGUF to E2E Object Storage:
    aws s3 cp --endpoint-url https://s3.e2enetworks.com \
        models/yukti-journal-q4_k_m.gguf \
        s3://your-bucket/yukti-models/
E2E
  ;;
esac

cat <<'STEPS'

=== COMMON STEPS — run on the GPU node after SSH login ===

# 1. Install system deps
sudo apt-get update -qq
sudo apt-get install -y python3-pip git wget aria2

# 2. Extract the package
cd /workspace   # E2E: use /home/ubuntu instead
tar -xzf yukti_trainer_package.tar.gz

# 3. Install Python deps
pip install --upgrade pip uv
uv pip install -r trainer/requirements.txt

# 4. [Optional] Re-export fresh journals from prod DB
#    (needed only if packaged data is > 1 week old)
# export POSTGRES_URL="postgresql+psycopg://user:pass@your-db-host:5432/yukti"
# python3 scripts/export_training_data.py \
#     --out data/training/journal_export.jsonl \
#     --min-quality 6

# 5. Preprocess (tokenize + split)
python3 trainer/preprocess.py \
    --input  data/training/journal_export.jsonl \
    --out_dir data/training/processed \
    --val_frac 0.05 --test_frac 0.05 \
    --dry_run
# Review summary, then run without --dry_run:
python3 trainer/preprocess.py \
    --input  data/training/journal_export.jsonl \
    --out_dir data/training/processed \
    --val_frac 0.05 --test_frac 0.05

# 6. Train — QLoRA 4-bit, ~1-2 hr on 24 GB GPU
python3 trainer/train_adapter.py \
    --data data/training/processed/train.jsonl \
    --model "unsloth/Phi-3-mini-4k-instruct" \
    --out_dir models/lora-journal \
    --epochs 3 --batch_size 4 \
    --use_peft auto --fp16 --gradient_checkpointing
# OOM on small GPU? Add: --batch_size 2 --gradient_checkpointing

# 7. Evaluate adapter vs base model
python3 trainer/evaluate_vs_baseline.py \
    --adapter_dir models/lora-journal \
    --base_model  "unsloth/Phi-3-mini-4k-instruct" \
    --out_dir     artifacts/eval
# Check artifacts/eval/report.json — only continue if metrics improved

# 8. Merge LoRA adapter into base model weights
python3 - <<'PY'
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer
import torch

MODEL_ID = "unsloth/Phi-3-mini-4k-instruct"
print("Loading base model...")
base = AutoModelForCausalLM.from_pretrained(
    MODEL_ID, torch_dtype=torch.float16, device_map="cpu"
)
tok  = AutoTokenizer.from_pretrained(MODEL_ID)
print("Merging LoRA adapter...")
merged = PeftModel.from_pretrained(base, "models/lora-journal").merge_and_unload()
merged.save_pretrained("models/merged")
tok.save_pretrained("models/merged")
print("Merged model saved -> models/merged")
PY

# 9. Convert to GGUF for Ollama deployment
#    (llama.cpp is pre-installed on most ML images; build if missing)
if [ ! -d llama.cpp ]; then
    git clone --depth 1 https://github.com/ggerganov/llama.cpp
    cmake -B llama.cpp/build llama.cpp -DLLAMA_CUBLAS=OFF
    cmake --build llama.cpp/build --config Release -j4 --target llama-quantize
fi

# Convert HF -> GGUF (F16 intermediate)
python3 llama.cpp/convert_hf_to_gguf.py models/merged \
    --outfile models/yukti-journal-f16.gguf \
    --outtype f16

# Quantize to Q4_K_M (~2.2 GB for Phi-3-mini; good quality/size balance for Ollama)
llama.cpp/build/bin/llama-quantize \
    models/yukti-journal-f16.gguf \
    models/yukti-journal-q4_k_m.gguf \
    Q4_K_M

echo "GGUF ready: models/yukti-journal-q4_k_m.gguf"
ls -lh models/yukti-journal-q4_k_m.gguf

# 10. Copy GGUF back to your VPS (run from LOCAL machine):
#     scp <user>@<node-ip>:/workspace/models/yukti-journal-q4_k_m.gguf .
#
# Load into Ollama on your VPS:
#     echo "FROM ./yukti-journal-q4_k_m.gguf" > Modelfile
#     ollama create yukti-journal -f Modelfile
#     ollama run yukti-journal "Test prompt"

# !! TERMINATE the GPU instance now to stop billing !!
STEPS

echo
echo "========================================================"
echo " Package  : $PKG ($PKG_SIZE)"
echo " Journals : $QUALITY_COUNT high-quality entries"
echo " Model    : $MODEL"
echo
echo " COST GUIDE (April 2026, budget target: < INR 1000/run)"
echo "   RTX 3090 spot (Vast.ai)       : ~INR 150-300  <- recommended"
echo "   A100 40G (E2E Networks, Delhi): ~INR 160-200  <- India-friendly"
echo "   RTX 4090 community (RunPod)   : ~INR 120-240"
echo "   H100 80G                      : ~INR 400-500  (overkill for 3.8B)"
echo "========================================================"
echo
echo "SECURITY CHECKLIST:"
echo "  - Package contains NO secrets (no .env, no API keys)"
echo "  - Set POSTGRES_URL / API keys as env vars on the GPU node only"
echo "  - Terminate instance immediately after downloading the GGUF"
echo "  - Verify eval metrics beat baseline BEFORE deploying to prod"
echo
echo "Done. Good luck with training!"
exit 0
