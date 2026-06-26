# Running the shared Nemo (read this first, ath)

This repo is checked out in two places on the Spark:

| Checkout | Who runs it | What it is |
|---|---|---|
| `~kev/nemotron-3000-omni` | kev only | kev's **private** instance |
| `/var/local/shared/nemo-sparky` | kev **or** ath | the **shared/team** instance |

Both checkouts share **one Docker daemon**. Image tags, container names, host
ports, and the sandbox `:current` / `:freeze-*` tags are a single global
namespace on that daemon. To keep the two instances from clobbering each other,
the shared checkout carries a `.env` that gives it its own names, network, and
ports. **You don't edit any tracked file to get this** — the `.env` is
per-checkout and untracked.

## Golden rule: one instance at a time

The vLLM model server alone is ~30–35 GB and shares the Spark's 128 GB unified
memory with `gary-backend`. **Do not** run the shared stack and a private stack
at the same time. Coordinate (a Slack ping, whatever) and check first:

```bash
docker ps --format '{{.Names}}'    # anything from another instance up?
```

## Spin up the shared stack

```bash
cd /var/local/shared/nemo-sparky
cat .env                 # confirm it's the shared config (nemo-shared-*, ports 30040/41)
./nemoctl up             # seeds the sandbox image if needed, composes up, starts auto-freeze
```

Then open the harness UI at **http://localhost:30041** (vLLM warms up for ~3 min).

Stop it when you're done so the box is free for the other instance:

```bash
./nemoctl down           # freezes Nemo's current state, then composes down
```

`./nemoctl help` lists everything. `./nemoctl status` shows containers,
auto-freeze state, and the sandbox image tags.

## How Nemo's playground persists — the part your agent must understand

Nemo (the model) lives in the **sandbox** container and can install packages,
write files, and run servers in there. Two different things persist in two
different ways — don't confuse them:

- **Nemo's FILES** live in the `./sandbox/workspace/` bind mount (`/workspace`
  inside the container). They're on the host disk and survive everything.
- **Nemo's TOOLS** (apt/pip packages, anything he builds under `/usr`, `/opt`,
  etc.) live *inside the container image*. A plain `docker compose down` would
  throw them away. `nemoctl` is what saves them, by committing the live
  container into an image.

The sandbox image has three kinds of tag (all under the repository name set by
`SANDBOX_IMAGE` in `.env`, e.g. `nemo-shared-sandbox`):

| Tag | Meaning |
|---|---|
| `:local` | factory baseline, built from `sandbox/Dockerfile` |
| `:current` | the **evolving** image compose actually runs; frozen on change/stop |
| `:freeze-<ts>` / `:freeze-<ts>-auto` | restore points (auto-pruned) |

What `nemoctl` does for you:

- **`nemoctl up`** seeds `:current` from `:local` on first run, then starts an
  **auto-freeze watcher**: every 15 min it fingerprints the container's
  installed tools and commits a new `:current` **only if something changed**
  (idle time costs nothing). Each commit also tags a `:freeze-<ts>-auto`
  restore point and logs the apt/pip delta to `.sandbox-history/freeze-log.md`.
- **`nemoctl down`** freezes once more (a manual `:freeze-<ts>`), then stops.
- **`nemoctl freeze`** checkpoints right now without stopping.
- **`nemoctl rollback <freeze-ts>`** points `:current` back at a restore point
  (run `nemoctl down` first; that freeze makes the rollback non-destructive).
- **`nemoctl history`** shows the changelog + available restore points.
- **`nemoctl reset`** rebuilds `:local` from the Dockerfile and resets
  `:current` to it. Nemo's `/workspace` files are untouched.

### Do NOT do this

- **Never `docker compose build sandbox`** and never add a `build:` key back to
  the `sandbox` service in compose. That would let compose overwrite Nemo's
  evolved `:current` image with a fresh build and wipe everything he installed.
  Use `nemoctl reset` for an intentional factory reset.
- Don't `docker rmi` the `:current` or `:freeze-*` tags by hand.

The shared instance's Nemo evolves **separately** from kev's private Nemo —
different image lineage, different `:current`, different `/workspace`. They never
touch each other.

## Working on the code as a team

The shared checkout should stay a clean mirror of GitHub `origin/main` so we can
always see how it differs from a private copy.

- **Pull updates:** `git -C /var/local/shared/nemo-sparky pull`
- **Make changes via branches + PRs** against
  `github.com/betweentwomidnights/nemo-sparky`, not by hand-editing files in the
  shared checkout (local edits to tracked files fight with `git pull`).
- The per-checkout, untracked stuff — `.env`, `media/`, `conversations/`,
  `sandbox/workspace/`, `.sandbox-history/` — is gitignored and won't conflict.

For the full architecture (services, ports, tunables, multimodal inputs, memory
budget), see [`../notes.md`](../notes.md).
