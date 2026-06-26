#!/usr/bin/env bash
# Pre-download weights to ~/.cache/huggingface so the container doesn't pull
# on first start. ~21 GB on disk for the NVFP4 quant.
#
# Runs inside the vLLM image we'll serve from — no host-side Python install
# needed. If the model is gated, export HF_TOKEN before running.
set -euo pipefail

MODEL_ID="${MODEL_ID:-nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4}"
HF_CACHE="${HF_CACHE:-${HOME}/.cache/huggingface}"
IMAGE="${IMAGE:-vllm/vllm-openai:v0.20.0}"

mkdir -p "${HF_CACHE}"

docker run --rm \
  --entrypoint python3 \
  -v "${HF_CACHE}:/root/.cache/huggingface" \
  -e HF_HOME=/root/.cache/huggingface \
  -e HF_TOKEN="${HF_TOKEN:-}" \
  "${IMAGE}" \
  -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_ID}', max_workers=8)"
