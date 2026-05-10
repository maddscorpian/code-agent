# Local AI Agent Installation Guide

This guide helps you set up and run `local-ai-agent` on a new machine.

## 1) Prerequisites

- Python `3.11` (recommended) or `3.12`
- Node.js `18+` and `npm`
- [Ollama](https://ollama.com/) installed
- Git

> Avoid Python 3.14 for now due to ecosystem compatibility issues.

---

## 2) Clone the repository

```bash
git clone <your-repo-url>
cd <your-repo>/local-ai-agent
```

---

## 3) Create virtual environment and install Python dependencies

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

If `python3.11` is unavailable, use `python3.12`.

---

## 4) Configure environment variables

```bash
cp .env.example .env
```

Default values are usually fine. Edit `.env` only if needed.

---

## 5) Pull required Ollama models

```bash
ollama pull deepseek-coder-v2
ollama pull nomic-embed-text
```

Check Ollama:

```bash
ollama list
```

---

## 6) Update project paths in `projects.yaml`

Edit `projects.yaml` and set real absolute paths for:

- Angular frontend repo
- Each Spring Boot microservice repo

Supported service types:

- `spring-boot` (preferred)
- `maven`, `gradle`, `spring`, `springboot` (accepted aliases)

---

## 7) Run first full digest

```bash
python -m digest.digest_runner
```

Expected:

- `digests/<project>.digest.json` files created
- `digests/master.digest.json` created

---

## 8) Start API server

```bash
python -m uvicorn api.server:app --port 8765 --reload
```

Use `python -m uvicorn` to ensure correct virtual environment is used.

---

## 9) Build embeddings and vector DB (first index)

Open a second terminal, activate venv, then run:

```bash
curl -X POST http://localhost:8765/reindex \
  -H "Content-Type: application/json" \
  -d "{}"
```

This will:

- run digest
- chunk code
- generate embeddings
- create/update ChromaDB in `vector_db/`

---

## 10) Health check

```bash
curl http://localhost:8765/health
```

Look for:

- `ollama: true`
- `chromadb: true`

---

## 11) Quick API test

```bash
curl -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"List all endpoints in user-service","mode":"chat"}'
```

---

## 12) Setup VS Code extension

```bash
cd vscode-extension
npm install
npm run compile
```

Then open the project in VS Code and press `F5` to launch Extension Development Host.

Use commands:

- `Local AI: Open Chat`
- `Local AI: Re-index Codebase`
- `Local AI: Explain This`
- `Local AI: Generate Change`
- `Local AI: Impact Analysis`

---

## Troubleshooting

### `No module named ollama`

- Activate venv and reinstall dependencies:
  ```bash
  source .venv/bin/activate
  pip install -r requirements.txt
  ```
- Start with:
  ```bash
  python -m uvicorn api.server:app --port 8765 --reload
  ```

### `Unsupported type ...`

- In `projects.yaml`, use `spring-boot` (or aliases supported above).

### `/health` shows `ollama: false`

- Start Ollama daemon/app
- Verify:
  ```bash
  ollama list
  ```

### Reindex is slow first time

- Normal for first embedding run; later incremental runs are faster.

---

## Optional: Clean reinstall

```bash
deactivate 2>/dev/null || true
rm -rf .venv
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```
