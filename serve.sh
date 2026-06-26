#!/usr/bin/env bash
# Standalone `docker run` alternative to `docker compose up`.
# Useful when you want to tweak flags interactively without editing compose.
set -euo pipefail

MODEL_ID="${MODEL_ID:-nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4}"
HOST_PORT="${HOST_PORT:-30030}"
HF_CACHE="${HF_CACHE:-${HOME}/.cache/huggingface}"
MEDIA_DIR="${MEDIA_DIR:-$(pwd)/media}"

mkdir -p "${MEDIA_DIR}"

docker run --rm -it \
  --gpus all \
  --ipc=host \
  --shm-size=16g \
  -p "${HOST_PORT}:8000" \
  --name nemotron-omni \
  -v "${HF_CACHE}:/root/.cache/huggingface" \
  -v "${MEDIA_DIR}:/media:ro" \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  -e HF_HOME=/root/.cache/huggingface \
  -e HUGGINGFACE_HUB_CACHE=/root/.cache/huggingface \
  --entrypoint /bin/bash \
  vllm/vllm-openai:v0.20.0 -c \
  "pip install -q vllm[audio] && vllm serve ${MODEL_ID} \
    --host 0.0.0.0 --port 8000 \
    --trust-remote-code \
    --tensor-parallel-size 1 \
    --max-model-len 32768 \
    --max-num-seqs 8 \
    --max-num-batched-tokens 16384 \
    --gpu-memory-utilization 0.35 \
    --kv-cache-dtype fp8 \
    --enable-prefix-caching \
    --video-pruning-rate 0.5 \
    --limit-mm-per-prompt '{\"video\":1,\"image\":1,\"audio\":1}' \
    --media-io-kwargs '{\"video\":{\"fps\":2,\"num_frames\":256}}' \
    --allowed-local-media-path /media \
    --reasoning-parser nemotron_v3 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder \
    --served-model-name nemotron-omni"
