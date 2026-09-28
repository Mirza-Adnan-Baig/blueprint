#!/usr/bin/env bash
# Sets up everything this project needs on the Mac Studio, in one go:
# native Ollama as a background service, the two models, Python and the
# project's packages, the .env settings file, the pipeline service and
# the hourly cleanup job. SETUP.md explains every step.
#
# Needs the internet (it downloads about 45 GB of models). Safe to run
# again later: it upgrades Ollama, skips what is already there, and
# restarts the services.
#
# Run from Terminal, inside the project folder:
#   bash scripts/setup_mac.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

CODE_MODEL="qwen2.5-coder:32b"
VISION_MODEL="qwen2.5vl:32b"
AGENTS="$HOME/Library/LaunchAgents"
USER_DOMAIN="gui/$(id -u)"

step() { echo; echo "===== $1 ====="; }
fail() { echo; echo "STOPPED: $1"; echo "See SETUP.md, section \"If something goes wrong\"."; exit 1; }

# Stop a background service if it is loaded, then load it from its file.
restart_agent() {
    local label="$1" plist="$2"
    launchctl bootout "$USER_DOMAIN/$label" >/dev/null 2>&1 || true
    sleep 2
    if ! launchctl bootstrap "$USER_DOMAIN" "$plist" 2>/dev/null; then
        sleep 3
        launchctl bootstrap "$USER_DOMAIN" "$plist" || fail "could not start the background service $label."
    fi
}

port_owner() {
    { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null || true; } | head -1
}

step "1. Checking this is an Apple Silicon Mac"
[ "$(uname -m)" = "arm64" ] || fail "this is not an Apple Silicon Mac (uname -m says $(uname -m))."
echo "OK: Apple Silicon."

step "2. Looking for Ollama running inside Docker"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    CONTAINERS="$(docker ps -a --format '{{.Names}} {{.Image}}' | grep -i ollama | awk '{print $1}' || true)"
    if [ -n "$CONTAINERS" ]; then
        echo "Found Ollama container(s): $CONTAINERS"
        read -r -p "Stop and remove them? The native Ollama installed next replaces them. Type y and press Enter: " answer
        if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
            for c in $CONTAINERS; do docker stop "$c" >/dev/null || true; docker rm "$c" >/dev/null || true; done
            echo "Removed."
        else
            fail "Ollama containers are still there. They would block port 11434."
        fi
    else
        echo "OK: no Ollama containers."
    fi
else
    echo "OK: Docker is not installed or not running, nothing to check."
fi

step "3. Checking Homebrew"
command -v brew >/dev/null 2>&1 || fail "Homebrew is not installed. Do SETUP.md section 3.4 first."
brew analytics off
echo "OK: $(brew --version | head -1)"

step "4. Installing or upgrading native Ollama"
if brew list ollama >/dev/null 2>&1; then
    brew upgrade ollama || true
else
    brew install ollama
fi
OLLAMA_BIN="$(brew --prefix)/bin/ollama"
"$OLLAMA_BIN" --version || true

step "5. Making sure nothing else is using Ollama's port (11434)"
brew services stop ollama >/dev/null 2>&1 || true
launchctl bootout "$USER_DOMAIN/com.docintel.ollama" >/dev/null 2>&1 || true
if pgrep -xq Ollama; then
    echo "The Ollama desktop app is running. Quitting it."
    osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || true
    sleep 5
fi
sleep 2
OWNER="$(port_owner 11434)"
if [ -n "$OWNER" ]; then
    echo "Port 11434 is still in use by:"
    ps -o pid=,command= -p "$OWNER" || true
    fail "another program is using Ollama's port. Quit it (SETUP.md section 3.2), then run this script again."
fi
echo "OK: port 11434 is free."
if [ -d /Applications/Ollama.app ]; then
    echo "NOTE: the Ollama desktop app is still installed. Remove it from Login Items"
    echo "(SETUP.md section 3.2) or it will start again at the next login and cause trouble."
fi

step "6. Starting Ollama as a background service"
mkdir -p "$AGENTS" "$REPO_DIR/logs"
cat > "$AGENTS/com.docintel.ollama.plist" <<EOF
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
restart_agent com.docintel.ollama "$AGENTS/com.docintel.ollama.plist"

echo "Waiting for Ollama to answer ..."
for _ in $(seq 1 60); do
    curl -s http://127.0.0.1:11434 >/dev/null && break
    sleep 1
done
curl -s http://127.0.0.1:11434 >/dev/null || fail "Ollama did not start. Look at logs/ollama.log."
OWNER_CMD="$(ps -o command= -p "$(port_owner 11434)" 2>/dev/null || true)"
case "$OWNER_CMD" in
    *"$OLLAMA_BIN"*|*"/opt/homebrew/"*ollama*) echo "OK: native Ollama is running ($OWNER_CMD).";;
    *) fail "port 11434 is answered by something else: $OWNER_CMD";;
esac

step "7. Downloading the models (large, can take a long time)"
"$OLLAMA_BIN" pull "$CODE_MODEL"
"$OLLAMA_BIN" pull "$VISION_MODEL"

step "8. Installing uv and the project's Python packages"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv sync

step "9. Settings file (.env)"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example."
else
    echo "OK: .env already exists, left unchanged."
fi

step "10. Running the tests"
uv run pytest -q || fail "some tests failed. Copy the output above and look into it before going on."

step "11. Starting the pipeline service and the hourly cleanup"
cat > "$AGENTS/com.docintel.pipeline.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.docintel.pipeline</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO_DIR/.venv/bin/uvicorn</string>
    <string>--app-dir</string><string>src</string>
    <string>pipeline_api:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8080</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$REPO_DIR/logs/service.log</string>
  <key>StandardErrorPath</key><string>$REPO_DIR/logs/service.log</string>
</dict>
</plist>
EOF
cat > "$AGENTS/com.docintel.gc.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.docintel.gc</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO_DIR/.venv/bin/python</string>
    <string>$REPO_DIR/scripts/gc_cleanup.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>$REPO_DIR/logs/gc.log</string>
  <key>StandardErrorPath</key><string>$REPO_DIR/logs/gc.log</string>
</dict>
</plist>
EOF
OWNER="$(port_owner 8080)"
if [ -n "$OWNER" ] && ! ps -o command= -p "$OWNER" | grep -q "pipeline_api"; then
    echo "Port 8080 is in use by:"
    ps -o pid=,command= -p "$OWNER" || true
    fail "another program is using port 8080 (Open WebUI in Docker uses 3000, so this is something else)."
fi
restart_agent com.docintel.pipeline "$AGENTS/com.docintel.pipeline.plist"
restart_agent com.docintel.gc "$AGENTS/com.docintel.gc.plist"

echo "Waiting for the pipeline service (it loads the code model first, up to 3 minutes) ..."
for _ in $(seq 1 180); do
    curl -s http://127.0.0.1:8080/health >/dev/null && break
    sleep 1
done
curl -s http://127.0.0.1:8080/health >/dev/null || fail "the pipeline service did not start. Look at logs/service.log."
echo "OK: pipeline service is running."

echo
echo "=============================================================="
echo " All done. Ollama, the models, the pipeline service and the"
echo " hourly cleanup are installed and running."
echo " Next: SETUP.md section 5 (Open WebUI)."
echo "=============================================================="
