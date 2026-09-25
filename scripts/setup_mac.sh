#!/usr/bin/env bash
# Phase I -- native environment setup for the Mac Studio.
# Run this once, from the Terminal app, from inside the cloned repo folder.
set -euo pipefail

echo "== 1. Checking for Docker-based Ollama =="
if command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qi ollama; then
    echo "Found an Ollama container. Stopping and removing it (native Ollama will replace it)."
    docker stop ollama || true
    docker rm ollama || true
else
    echo "No Docker Ollama container found, nothing to remove."
fi

echo "== 2. Installing Homebrew (if missing) =="
if ! command -v brew >/dev/null 2>&1; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

echo "== 3. Installing native Ollama =="
if ! command -v ollama >/dev/null 2>&1; then
    brew install ollama
fi
brew services start ollama
sleep 3

echo "== 4. Installing uv (Python package/venv manager) =="
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "== 5. Pulling models =="
ollama pull qwen2.5-coder:32b
ollama pull llava

echo "== 6. Setting context window =="
# OLLAMA_CONTEXT_LENGTH controls the default num_ctx for the server; the
# pipeline also passes num_ctx explicitly per-request (see .env NUM_CTX),
# so this just raises the server-side default/floor.
launchctl setenv OLLAMA_CONTEXT_LENGTH 32768
echo "Add 'export OLLAMA_CONTEXT_LENGTH=32768' to your shell profile so it survives a reboot."

echo "== 7. Python project setup =="
uv sync

echo "== 8. Copying .env =="
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example -- review it before starting the service."
fi

echo
echo "Setup complete. Verify with:"
echo "  ollama --version     (want 0.34.0 or newer)"
echo "  ollama list           (should show qwen2.5-coder:32b and llava)"
echo "  uv run uvicorn --app-dir src pipeline_api:app --port 8080"
