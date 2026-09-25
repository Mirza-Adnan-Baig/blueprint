#!/usr/bin/env bash
# Phase I -- native environment setup for the Mac Studio.
#
# Downloads Homebrew packages, Python packages and ~45 GB of models, so
# it needs the internet connection. Safe to re-run later: it upgrades
# Ollama and skips anything already in place.
#
# Prerequisite: SETUP.md sections 1-3.3 done (admin rights confirmed,
# Xcode Command Line Tools and Homebrew installed).
#
# Run from Terminal, inside the repo folder:  ./scripts/setup_mac.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

CODE_MODEL="qwen2.5-coder:32b"
VISION_MODEL="qwen2.5vl:32b"
OLLAMA_PLIST="$HOME/Library/LaunchAgents/com.docintel.ollama.plist"

echo "== 1. Hardware check =="
if [ "$(uname -m)" != "arm64" ]; then
    echo "This is not an Apple Silicon Mac (uname -m = $(uname -m)). Stopping."
    exit 1
fi
echo "Apple Silicon confirmed."

echo "== 2. Docker-based Ollama =="
if command -v docker >/dev/null 2>&1; then
    CONTAINERS="$(docker ps -a --format '{{.Names}} {{.Image}}' 2>/dev/null | grep -i ollama | awk '{print $1}' || true)"
    if [ -n "$CONTAINERS" ]; then
        echo "Found Ollama container(s): $CONTAINERS"
        read -r -p "Stop and remove them? Native Ollama replaces them. [y/N] " answer
        if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
            for c in $CONTAINERS; do docker stop "$c" || true; docker rm "$c" || true; done
        else
            echo "Left in place. Make sure they are stopped, or they will fight over port 11434."
        fi
    else
        echo "No Ollama containers."
    fi
else
    echo "Docker not installed, nothing to check."
fi

if [ -d /Applications/Ollama.app ]; then
    echo "WARNING: /Applications/Ollama.app exists. The desktop app starts its own server on"
    echo "port 11434 at login and will conflict with the service this script sets up."
    echo "Quit it and remove it from System Settings > General > Login Items (SETUP.md 3.4)."
fi

echo "== 3. Homebrew =="
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found. Follow SETUP.md section 3.3 first, then re-run this script."
    exit 1
fi
brew analytics off

echo "== 4. Native Ollama =="
if brew list ollama >/dev/null 2>&1; then
    brew upgrade ollama || true
else
    brew install ollama
fi
OLLAMA_BIN="$(brew --prefix)/bin/ollama"
"$OLLAMA_BIN" --version || true

# Our own LaunchAgent instead of `brew services`, because a background
# service never reads ~/.zprofile -- environment variables have to live in
# the plist itself to actually reach the Ollama server.
brew services stop ollama >/dev/null 2>&1 || true
mkdir -p "$HOME/Library/LaunchAgents" "$REPO_DIR/logs"
cat > "$OLLAMA_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.docintel.ollama</string>
  <key>ProgramArguments</key>
  <array>
    <string>$OLLAMA_BIN</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OLLAMA_HOST</key><string>127.0.0.1:11434</string>
    <key>OLLAMA_CONTEXT_LENGTH</key><string>32768</string>
    <key>OLLAMA_MAX_LOADED_MODELS</key><string>1</string>
    <key>OLLAMA_NUM_PARALLEL</key><string>1</string>
    <key>OLLAMA_KEEP_ALIVE</key><string>5m</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$REPO_DIR/logs/ollama.log</string>
  <key>StandardErrorPath</key><string>$REPO_DIR/logs/ollama.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/com.docintel.ollama" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$OLLAMA_PLIST"

echo "Waiting for Ollama to answer on 127.0.0.1:11434 ..."
for _ in $(seq 1 30); do
    if curl -s http://127.0.0.1:11434 >/dev/null; then break; fi
    sleep 1
done
curl -s http://127.0.0.1:11434 && echo

echo "== 5. Models (large downloads) =="
"$OLLAMA_BIN" pull "$CODE_MODEL"
"$OLLAMA_BIN" pull "$VISION_MODEL"

echo "== 6. uv and Python dependencies =="
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
uv sync

echo "== 7. .env =="
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example."
else
    echo ".env already exists, left unchanged."
fi

echo
echo "Setup complete. Verify (SETUP.md section 7):"
echo "  $OLLAMA_BIN --version"
echo "  $OLLAMA_BIN list        # expect $CODE_MODEL and $VISION_MODEL"
echo "  uv run pytest tests/ -v"
echo "Then continue with SETUP.md section 5 (Open WebUI) and section 6 (end-to-end check)."
