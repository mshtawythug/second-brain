# Running the local reranker (llama.cpp)

> Part of the [Second Brain](../../README.md) docs. See [docs/README.md](../README.md) for the full index.

The cross-encoder reranker (`BRAIN_RERANKER`, see
[configuration](../configuration.md#brain_reranker--the-cross-encoder-reranker-off-by-default))
reorders the head of every search. Its local backend, `llamacpp`, is **`llama-server`** from
llama.cpp, serving the **`bge-reranker-v2-m3`** cross-encoder over loopback HTTP. Ollama has no
rerank endpoint, so Ollama cannot run it. This guide installs the server, downloads the model,
checks both, and optionally keeps the server running under launchd.

The reranker ships **off**. Until `brain eval` shows it helps on your corpus (roadmap ticket
RM-14c), running it only costs memory. Search never fails because of it: when it is not
running, results stay in hybrid order and the CLI prints one line, `reranker unavailable;
results are in hybrid order`.

## What was tested

Measured on 2026-10-30, on an Apple Silicon Mac running macOS 26.

| | |
|---|---|
| llama.cpp | **0.7.0** from Homebrew (`brew install llama.cpp`). `llama-server --version` prints `version: 0.7.0 (build 11669, commit 620d2225c)`. It pulls in `ggml` 0.27.0. |
| Model | [`gpustack/bge-reranker-v2-m3-GGUF`](https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF), file **`bge-reranker-v2-m3-Q8_0.gguf`**: a GGUF conversion of [`BAAI/bge-reranker-v2-m3`](https://huggingface.co/BAAI/bge-reranker-v2-m3) (Apache 2.0). |
| Model size | **635,676,416 bytes** (606 MiB). sha256 `a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3`, which matches the file's Hugging Face LFS id. |
| Resident memory | **822,896 KiB (≈ 804 MiB)** once `/health` answered, and 825,200 KiB after one 3-passage rerank (`ps -o rss -p <pid>`). The server ran with its defaults: 4 slots, an 8,192-token context per slot. |
| Request shape | `POST /v1/rerank` with `{model, query, documents, top_n}` returned `{"results": [{"index", "relevance_score"}, ...]}`, the shape the `llamacpp` backend (`brain/rerank_backends.py`) parses. |

Only the Q8_0 file was measured. The same repository has smaller quantizations (`Q4_K_M` is
438,376,864 bytes), whose resident memory and ranking quality were not measured here. A newer
llama.cpp may change the rerank API; if `brain search` starts printing `reranker unavailable`
after a `brew upgrade`, check the request shape against the
[llama.cpp server README](https://github.com/ggml-org/llama.cpp/tree/master/tools/server).

## 1. Install llama.cpp

```bash
brew install llama.cpp
llama-server --version
```

## 2. Download the model

The launchd job looks for the model at `$BRAIN_HOME/models/bge-reranker-v2-m3-Q8_0.gguf`
(`~/.brain/models/` for a default install) unless `BRAIN_RERANK_GGUF` names another path.

```bash
mkdir -p ~/.brain/models && cd ~/.brain/models
curl -L -o bge-reranker-v2-m3-Q8_0.gguf \
  https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q8_0.gguf
shasum -a 256 bge-reranker-v2-m3-Q8_0.gguf
# a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3
```

## 3. Try it by hand

All three model flags are required. `--reranking` only enables the endpoint; it is
`--embedding --pooling rank` that makes the model score query–passage pairs. Bind to
`127.0.0.1`: the passages are your documents' text, and `BRAIN_RERANK_URL` refuses any
non-loopback host.

```bash
llama-server -m ~/.brain/models/bge-reranker-v2-m3-Q8_0.gguf \
  --embedding --pooling rank --reranking --host 127.0.0.1 --port 8081
```

In another terminal:

```bash
curl -s http://127.0.0.1:8081/health                # {"status":"ok"} once the model is loaded
curl -s http://127.0.0.1:8081/v1/rerank -H 'Content-Type: application/json' -d '{
  "model": "bge-reranker-v2-m3", "query": "what is a panda?", "top_n": 2,
  "documents": ["Paris is the capital of France.", "The giant panda is a bear native to China."]}'
```

The second document should get the higher `relevance_score`. `/health` answers HTTP 503 while
the model is still loading, which took about two seconds here. Stop the server with Ctrl-C.

## 4. Turn it on

In `$BRAIN_HOME/.env`:

```bash
BRAIN_RERANKER=llamacpp
# BRAIN_RERANK_URL=http://127.0.0.1:8081   # the default
```

`brain search --no-rerank "…"` and `--rerank` override the setting for one run. The other
knobs (depth, timeout, passage length) are in [configuration](../configuration.md).

## 5. Keep it running (optional launchd job)

```bash
brain-install-launchd --with-reranker
```

This installs `com.brain.reranker`, a `KeepAlive` LaunchAgent that runs the command from step 3
at login and restarts it if it dies. Its arguments are fixed in the template
(`src/brain/templates/launchd/com.brain.reranker.plist.j2`). Only the model path and the port
can change, and the host is always `127.0.0.1`, so the job never listens beyond this machine.

- **The port** is `BRAIN_RERANK_URL`'s, so the job listens where searches look for it: 8081
  when the variable is unset. It is read from your shell, else from `$BRAIN_HOME/.env`. The
  installer refuses a URL whose host is not `127.0.0.1`, `localhost` or `::1`, and one without
  an explicit port in 1024–65535 (`http://127.0.0.1` alone, or `:80`). The host in the URL
  never reaches the job: it binds `127.0.0.1` either way, so use `127.0.0.1` in the URL too.
  To change the port, set it and run `brain-install-launchd` again.

- **The model path** is `BRAIN_RERANK_GGUF`, taken from your shell, else from
  `$BRAIN_HOME/.env`, else `$BRAIN_HOME/models/bge-reranker-v2-m3-Q8_0.gguf`. The installer
  refuses `--with-reranker` when that file does not exist. To change the path, set it and run
  `brain-install-launchd` again.
- **It stays installed.** `brain-up` runs `brain-install-launchd` without flags on every start.
  That run re-renders and reloads a reranker job that is already installed, and never removes
  it. A model file that has gone missing does not stop `brain-up`; `brain doctor` reports the
  dead job instead.
- **Logs:** `$BRAIN_HOME/logs/com.brain.reranker.err.log` (llama-server writes its log to
  stderr) and `….out.log`.
- **Restarts are throttled** to one a minute (`ThrottleInterval` 60), so a missing model or a
  taken port does not spin.

## 6. Check it

```bash
brain doctor
```

Two lines cover the reranker:

| Line | Says |
|---|---|
| `reranker` | `not configured` while `BRAIN_RERANKER=none`. With `llamacpp`, it calls `GET {BRAIN_RERANK_URL}/health` with a 2-second timeout: OK when it answers 200, a WARN `not running` when nothing answers, and a WARN `not ready (HTTP 503)` while the model loads. With `voyage` there is nothing local to check. It is never a FAIL, because search carries on without the reranker. |
| `daemons` | Includes `com.brain.reranker` only when its plist is installed, like the other agents, so a machine that never opted in sees no change. |

## When port 8081 is taken

On the machine this guide was measured on, port 8081 was already in use by a LanguageTool
server (8081 is LanguageTool's default port), and `llama-server` exited with `couldn't bind
HTTP server socket, hostname: 127.0.0.1, port: 8081`. Under launchd that line lands in
`com.brain.reranker.err.log`, `brain doctor`'s `reranker` line says `not running`, and its
`daemons` line reports the job's non-zero exit.

Find the owner with `lsof -nP -iTCP:8081 -sTCP:LISTEN`, pick a free port (8091 here), and move
the reranker there:

```bash
echo 'BRAIN_RERANK_URL=http://127.0.0.1:8091' >> ~/.brain/.env
brain-install-launchd            # re-renders com.brain.reranker with --port 8091
```

Searches and `brain doctor` read the same variable, so all three now agree. That is why the
port follows `BRAIN_RERANK_URL`. The roadmap ticket fixed the job at 8081, and this machine is
the reason it no longer does (a recorded deviation).

## Memory

The server holds the model resident: about 0.8 GiB with the Q8_0 file, on top of Ollama's
models and Postgres. Stop it whenever you are not using the reranker. Searches do not need it.

## Remove it

`brain-uninstall-launchd` removes **every** brain LaunchAgent, the reranker included. To remove
only the reranker:

```bash
launchctl bootout "gui/$(id -u)/com.brain.reranker"
rm ~/Library/LaunchAgents/com.brain.reranker.plist
```

Deleting the plist is what stops the next `brain-up` from bringing the job back. Then set
`BRAIN_RERANKER=none` (or remove the line), and delete the model file if you want the disk
space back.

## The hosted alternative

`BRAIN_RERANKER=voyage` needs no local runtime. It uses `VOYAGE_API_KEY` and the model in
`VOYAGE_RERANK_DEFAULT_MODEL` (`rerank-3`, checked against Voyage's docs on 2026-10-29; see
`src/brain/settings/rerank.py`). It never receives a confidential document.
