# nemotron-3000-omni

Experimental playground for **NVIDIA Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4**
on the DGX Spark. Multimodal (text + image + audio + video), MoE-31B/A3B, 4-bit NVFP4.
Coexists with `~/gary-backend-spark` — does **not** wire into it.

## Layout

```
nemotron-3000-omni/
  Dockerfile, docker-compose.yml, serve.sh, download.sh   # model server
  harness/                                                # Flask chat UI
    Dockerfile, app.py, requirements.txt
    static/app.{js,css}, templates/index.html
  sandbox/                                                # Nemo's playground container
    Dockerfile, server.py
    workspace/                                            # persistent home for Nemo
  media/                                                  # shared with model (rw harness, ro model)
  conversations/                                          # session snapshots
```

## Three services

| Service        | Host port | Internal name           | Role                                |
|----------------|-----------|-------------------------|-------------------------------------|
| nemotron-omni  | 30030     | `nemotron-omni:8000`    | vLLM model server (OpenAI API)      |
| harness        | 30031     | `harness:8000`          | Flask chat UI + tool dispatcher     |
| sandbox        | (none)    | `sandbox:8000`          | Nemo's playground (file ops + exec) |

The sandbox is **not exposed to the host** — only the harness reaches it on
the compose network.

## One-time setup

```bash
# 1. Pull the weights into the shared HF cache (~21 GB).
#    Runs inside the vllm/vllm-openai image — no host Python needed.
#    If the model is gated, `export HF_TOKEN=hf_xxx` first.
./download.sh

# 2. Build the image (vllm/vllm-openai:v0.20.0 + vllm[audio] baked in).
docker compose build
```

`download.sh` writes to `~/.cache/huggingface`, which is the same cache
gary-backend uses — no duplication.

## Run

```bash
docker compose up                   # both services, foreground
docker compose up -d                # detached
docker compose logs -f nemotron-omni
docker compose logs -f harness
docker compose down                 # stop + remove
```

Two services:
- **nemotron-omni** — the vLLM model server. OpenAI-compatible API at
  `http://localhost:30030/v1`. Model name: `nemotron-omni`.
- **harness** — Flask chat UI at `http://localhost:30031`. Talks to the
  model over the compose network at `http://nemotron-omni:8000`.

Both ports stay clear of gary-backend's 80xx range.

Quick liveness checks:
```bash
curl -s http://localhost:30030/v1/models | python3 -m json.tool   # model
curl -sI http://localhost:30031/                                  # harness
```

## Harness

`harness/` is a Flask + vanilla-JS chat UI. Single user, no auth, localhost only.

- **📁 workspace** opens Nemo's sandbox file tree on the left. Auto-refreshes
  after every tool call. Click a file to preview / edit / delete; toolbar has
  ＋ dir and ＋ file buttons.
- **🗂 files** browses your `$HOME` (mounted read-only at `/host` inside the
  container) and copies the picked file into `./media/` so the model can attach it.
- **⚙ settings** drawer holds the system prompt (with edit history) and
  generation knobs (temperature, top-p, max tokens, thinking on/off + budget,
  and a tools-enabled toggle).
- **Drag-and-drop or paperclip** uploads files from your PC into `./media/`.
- **Reasoning** streams as a collapsible "Thinking…" block above each round's
  answer. Each tool-call round gets its own thinking block.
- **Tool calls** render inline as cards (name, streaming args, collapsed result).
- Conversations are snapshotted to `./conversations/<sid>.json` per browser session.

The `BROWSE_ROOT` env var on the harness service controls what the file picker
can see — change it in `docker-compose.yml` if you want to narrow the surface.

## Sandbox

`sandbox/` is Nemo's playground container — a `python:3.12-slim` with `git`,
`curl`, `vim`, `build-essential`, etc. plus a small Flask API at `:8000`
(internal-only) exposing:

| Endpoint                    | Tool name                   |
|-----------------------------|-----------------------------|
| `GET /fs?path=`             | `list_files`                |
| `GET/POST /fs/file`         | `read_file`, `write_file`   |
| `POST /fs/edit`             | `edit_file` (exact replace) |
| `POST /fs/delete`           | `delete_file`               |
| `POST /fs/mkdir`            | `make_dir`                  |
| `POST /fs/move`             | `move`                      |
| `POST /exec`                | `run_shell` (30s, max 300s) |
| `POST /processes/start|stop`| `start_server`, `stop_server` |
| `GET /processes`            | `list_servers`              |
| `GET /processes/logs`       | `server_logs`               |

The harness exposes these to the model as OpenAI tool schemas; vLLM's
`qwen3_coder` parser handles call serialization. The harness loops up to
**MAX_TOOL_ROUNDS=15** times per turn, executing tool calls and feeding
results back to the model.

One more tool isn't a sandbox endpoint: **`remember`** — it appends markdown to
Nemo's persistent memory at `/workspace/.nemo/MEMORY.md` (created on first use),
which the harness auto-loads into his system prompt on every future
conversation. It's implemented harness-side via the `/fs/file` API. This is the
model's self-memory; for bigger reorganizations Nemo uses `read_file` +
`write_file` on that same file directly.

### Self-editing: what Nemo can and can't change (open question)

Today the model can edit its own **memory** (`remember`) and its **workspace**
files, but **not** its own system prompt. `update_system_prompt` is only the
`PATCH /api/session/system-prompt` HTTP route behind the ⚙ settings drawer — a
*human* edits the prompt there. It is **not** registered as a model tool, so
Nemo cannot rewrite his own system prompt. (An earlier version of this note
claimed it was a model tool; that was never actually wired in.)

**This is an open design question, not a settled decision.** The intent is to
make as much of the harness model-editable as is *sane*. Memory: in. Live
system-prompt self-editing: currently out — a model rewriting its own prompt
mid-session is a sharp edge worth gating carefully. If we revisit it, sane
guardrails might include append-only edits (not full overwrite), a
human-approval step, or leaning on the existing prompt-edit history (the route
already records `source` and keeps a `system_prompt_history` list) so any model
change stays reviewable and revertible.

### Workspace persistence

`./sandbox/workspace/` is bind-mounted into the container at `/workspace`.
You can `ls`, `cat`, edit it from your shell — same files Nemo sees.

System python packages installed via `pip install` are container-local and
disappear on `docker compose down`. To make installs persistent, Nemo should
create a venv inside `/workspace` (e.g. `python -m venv /workspace/.venv`).
The default system prompt instructs it to do this.

### Security boundary

The sandbox container has **only one host bind-mount: the workspace**. It
cannot see `~/`, `/etc`, `/var`, the gary-backend directory, or anything else
on the Spark. Network egress is enabled (default Docker bridge) so it can
`curl` your music APIs and `pip install` from PyPI, but it can't poke the
host filesystem.

If you ever want a tighter sandbox, options include:
- Drop network: add `network_mode: none` to the sandbox service
- Add a seccomp profile or `--cap-drop=ALL`
- Run as non-root (the image is currently root)

## Tunables (in `docker-compose.yml`)

Tuned for **coexistence with gary-backend on the 128 GB unified-memory Spark**,
not for max throughput.

| Flag | Value | Why |
|---|---|---|
| `--max-model-len` | 32768 | Down from 131k default. KV cache shrinks ~4×. |
| `--kv-cache-dtype` | fp8 | Halves KV cache vs BF16. |
| `--gpu-memory-utilization` | 0.35 | Caps vLLM at ~45 GB of unified memory; leaves room for gary-backend's ~30 GB working set. |
| `--max-num-seqs` | 8 | Default 384 is wildly oversized for a shared box. |
| `--max-num-batched-tokens` | 16384 | Matches the smaller batch. |
| `--video-pruning-rate` | 0.5 | Drops half the video tokens; cheap quality hit, big memory win. |

If gary-backend isn't running, you can push `--gpu-memory-utilization` to 0.7
and bump `--max-model-len` to 65536 or 131072 for headroom.

## Multimodal inputs

`--allowed-local-media-path /media` exposes `./media/` (read-only) inside the
container. Drop test audio/video/image files there and reference them as
`file:///media/your-file.mp4` in API calls.

Limit per prompt: 1 video + 1 image + 1 audio (`--limit-mm-per-prompt`).

## Memory budget on Spark

The Spark has 128 GB unified memory shared between CPU and GPU.
Watch with `free -h`, **not** `nvidia-smi` — the Memory-Usage column reads
"Not Supported" on GB10 and the per-process numbers undercount.

Rough footprint at the configured flags:
- Weights: ~21 GB
- KV cache (32k context, 8 seqs, fp8): ~6–8 GB
- Activations / overhead: ~3–5 GB
- **Total: ~30–35 GB**

## Gotchas

- **Image is multi-arch but verify ARM64 pulls.** `vllm/vllm-openai:v0.20.0`
  publishes aarch64 manifests; if pull fails on Spark, try `:v0.20.0-cu129`.
- **Mamba kernels.** vLLM 0.20.0+ ships `causal-conv1d` for the Mamba layer
  inside the image; if you see import errors at startup, you may need to
  rebuild a custom layer like `~/nemotron-3000/Dockerfile` does.
- **Port 30030.** Picked to stand out from gary-backend (which uses 80xx).
  Change `HOST_PORT` in `serve.sh` or the `ports:` mapping in compose if needed.
- **First container start is slow** even with weights pre-downloaded —
  vLLM compiles CUDA graphs and warms up the model.

## Stack reminder

This is **vLLM**, not TRT-LLM. The sibling `~/nemotron-3000/` runs the
text-only Nemotron-3-Nano on TRT-LLM via `trtllm-serve --backend _autodeploy`
with a hand-tuned `nano_v3.yaml`. Do **not** copy that YAML here — the omni
variant has additional vision/audio encoders the autodeploy plan doesn't cover.
