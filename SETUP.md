# Mac Studio environment setup — from unknown state to verified, offline

Use this when you don't know what's installed on the machine, or don't
trust that it was installed correctly. It assumes nothing except that
macOS is running. Every command says what it does and what the output
should look like.

**Target:** Apple Silicon Mac Studio (M2 Ultra, 64GB unified memory),
running 100% offline once set up. Only free, open-source / open-weight
software is used: Ollama, Qwen2.5 models, Open WebUI, Python, DuckDB.

**How to use this:** open Terminal (`Cmd+Space`, type `Terminal`, press
Enter). Every fenced code block below is meant to be copied into Terminal
and run. In section 1, run commands **one at a time** and read each output
before moving on. Replace anything in `<angle-brackets>` with the real
value from an earlier output — never paste those literally.

---

## 0. Two phases: connected once, then air-gapped

Downloads are unavoidable exactly once: Homebrew packages, Python
packages, and the two models (about 45 GB together). So setup happens in
two phases:

1. **Connected phase** — sections 1–5. The Mac needs internet.
2. **Air-gapped phase** — section 6. Disconnect, then prove that
   everything still works with no network at all. From then on, nothing
   in this system needs or attempts an internet connection.

If this Mac may never touch the internet at all, see section 9 first.

---

## 1. Find out what's on this machine (inspect only, changes nothing)

### 1.1 Hardware and macOS version
```bash
sw_vers
uname -m
sysctl -n hw.memsize | awk '{print $1/1024/1024/1024 " GB RAM"}'
df -h /
```
Expect: a macOS version, `arm64` (Apple Silicon), `64 GB RAM`, and — in
the last command's `Avail` column — at least **80 GB free** (models ~45 GB,
plus room for documents and their databases).

### 1.2 Admin rights
```bash
whoami
dscl . -read /Groups/admin GroupMembership
```
Your username from the first command must appear in the second output.
If it doesn't, stop — whoever manages this Mac has to add you to the admin
group. No command here can grant it.

### 1.3 Docker
```bash
ls /Applications | grep -i docker
docker --version
docker ps -a
```
The last command lists every container, running or stopped. Note any
name or image containing `ollama` or `open-webui`. An error like
`command not found` just means Docker isn't installed — that's fine.

Why this matters: Docker on a Mac runs containers inside a Linux virtual
machine that does not get proper access to the Apple GPU (Metal). Ollama
inside Docker therefore runs on the CPU only — much slower, and a likely
cause of hangs and timeouts on large prompts.

### 1.4 Ollama — installed? how? which version?
```bash
which ollama
ollama --version
ls /Applications | grep -i ollama
brew list 2>/dev/null | grep -i ollama
launchctl list | grep -i ollama
```
How to read it:
- `which ollama` prints a path (e.g. `/opt/homebrew/bin/ollama`) → a
  native command-line install exists.
- `ls /Applications` shows `Ollama.app` → the Ollama desktop app is
  installed. It starts its own server at login (see 3.2).
- `which` prints nothing but section 1.3 showed an `ollama` container →
  it only exists inside Docker.
- Nothing anywhere → not installed.

Version requirement: **0.19.0 or newer** at minimum (older versions have
a documented bug where some models' tool calls leak into plain text, and
lack the Apple MLX backend); **the current release is recommended**. The
setup script upgrades it anyway.

### 1.5 Open WebUI — installed? how?
```bash
docker ps -a | grep -i open-webui
which open-webui
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(3000|8080) '
```
Container, native command, or nothing. The last command shows which
programs are listening on ports 3000/8080 (the usual Open WebUI ports).

### 1.6 Models already downloaded
```bash
ollama list
```
If Ollama only exists in Docker, use the container name from 1.3:
```bash
docker exec -it <container-name> ollama list
```
Old models (e.g. `llava`, `qwen3.6:35b`) aren't harmful, but each one
takes disk space. You can remove one later with `ollama rm <model-name>`.

### 1.7 Write down what you found
- Ollama: native CLI / desktop app / Docker / not installed? Version?
- Open WebUI: native / Docker / not installed? Which port?
- Admin rights confirmed?
- Free disk space?

---

## 2. Decide what to keep vs. replace

| Component | Currently native | Currently in Docker | Not installed |
|---|---|---|---|
| **Ollama** | Keep — the setup script upgrades it and runs it as a proper background service | **Replace with native** (section 3.1) | Install (section 4) |
| **Open WebUI** | Keep | Keep, it's fine in Docker (section 5, option B) | Install natively (section 5, option A) |

**Why only Ollama must be native:** Ollama does the heavy GPU work and
needs direct Metal access, which Docker can't give it on a Mac. Open WebUI
is a web interface — it does no model computation itself, so running it
in Docker costs nothing meaningful. If it's already in Docker and has
user accounts and chats in it, leave it there.

---

## 3. Prerequisites

### 3.1 If Ollama runs in Docker: stop and remove that container
Use the exact name from section 1.3:
```bash
docker stop <container-name>
docker rm <container-name>
```
This removes the container, not the Docker app. Its downloaded models are
not reused — the native install downloads them again.

### 3.2 If the Ollama desktop app is installed: stop it from auto-starting
The desktop app and the background service set up in section 4 would
both try to use port 11434. Keep only the service:
1. Click the llama icon in the menu bar → **Quit Ollama**.
2. System Settings → General → **Login Items** → select Ollama → click
   **–** to remove it.
3. Optional: drag `/Applications/Ollama.app` to the Trash.

### 3.3 Xcode Command Line Tools
Apple's compiler tools, which Homebrew needs (not the full Xcode app):
```bash
xcode-select --install
```
A window pops up — click **Install**, accept the license, wait a few
minutes. If it says the tools are already installed, that's fine. Verify:
```bash
xcode-select -p
```
Should print `/Library/Developer/CommandLineTools`.

### 3.4 Homebrew
Skip if `brew --version` already works.
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
This is Homebrew's official installer. It will:
- ask for your Mac login password (it creates `/opt/homebrew`) — nothing
  appears while you type the password; that's normal, press Enter
- ask you to press Enter to continue
- print a **"Next steps"** block at the end. Run the commands it prints.
  They are normally exactly these two:
```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```
Verify, then switch off Homebrew's usage analytics:
```bash
brew --version
brew analytics off
```

---

## 4. Run the setup script (Ollama, models, Python dependencies)

Get the project onto the Mac. With git (installed by the Command Line
Tools in 3.3):
```bash
cd ~
git clone https://github.com/Mirza-Adnan-Baig/blueprint.git
cd blueprint
```
Then run the script:
```bash
chmod +x scripts/setup_mac.sh
./scripts/setup_mac.sh
```
What it does, in order:

| Step | What happens | Why |
|---|---|---|
| 1 | Confirms Apple Silicon | Everything here assumes an M-series Mac |
| 2 | Offers to remove Docker Ollama containers; warns if `Ollama.app` exists | Only one Ollama may own port 11434 |
| 3 | Checks Homebrew, turns off its analytics | Nothing should report usage anywhere |
| 4 | Installs/upgrades native Ollama and registers it as a background service (LaunchAgent) with its settings built in | Starts automatically at login, restarts if it crashes |
| 5 | Downloads `qwen2.5-coder:32b` and `qwen2.5vl:32b` | ~45 GB total, can take a long time |
| 6 | Installs `uv` if missing, runs `uv sync` | Creates `.venv` with this project's exact Python packages |
| 7 | Creates `.env` from `.env.example` | Your local settings file |

The Ollama settings the script builds into the service, and why:

| Setting | Value | Why |
|---|---|---|
| `OLLAMA_HOST` | `127.0.0.1:11434` | Only programs on this Mac can reach Ollama |
| `OLLAMA_CONTEXT_LENGTH` | `32768` | Default context window when a request doesn't set one |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | Safety net: never two 32B models in memory at once — see README section 5 |
| `OLLAMA_NUM_PARALLEL` | `1` | Each parallel slot reserves its own context memory; one user at a time is plenty |
| `OLLAMA_KEEP_ALIVE` | `5m` | Default for any model not pinned explicitly. The code model is pinned by the pipeline itself |

These live in `~/Library/LaunchAgents/com.docintel.ollama.plist`, not in
`~/.zprofile` — a background service never reads your shell profile, so
settings put there would silently have no effect.

Verify:
```bash
ollama --version
ollama list
curl http://127.0.0.1:11434
launchctl list | grep com.docintel.ollama
```
Expect: a current version; both `qwen2.5-coder:32b` and `qwen2.5vl:32b`
listed; `Ollama is running`; and one line for `com.docintel.ollama`.

Check it's native and on the GPU:
```bash
ps aux | grep "[o]llama serve"
```
The path shown must be `/opt/homebrew/...`, not anything with `docker`.

If you ever need to restart Ollama:
```bash
launchctl kickstart -k gui/$(id -u)/com.docintel.ollama
```

---

## 5. Open WebUI, configured for offline use

Open WebUI tries by default to check for updates and download helper
models from the internet. These settings turn that off:

| Setting | Value | Effect |
|---|---|---|
| `OFFLINE_MODE` | `true` | No update checks, no automatic model downloads |
| `HF_HUB_OFFLINE` | `1` | Never contact Hugging Face |
| `ENABLE_OPENAI_API` | `false` | Removes the default OpenAI (cloud) connection |
| `SCARF_NO_ANALYTICS`, `DO_NOT_TRACK` | `true` | No usage analytics |
| `ANONYMIZED_TELEMETRY` | `false` | No telemetry from the bundled vector store |

### Option A — native (if not installed, or you're moving it off Docker)

Install (needs internet, part of the connected phase):
```bash
uv tool install --python 3.11 open-webui
```
Register it as a background service. Copy this whole block into Terminal
— it writes the file for you:
```bash
mkdir -p ~/open-webui-data
cat > ~/Library/LaunchAgents/com.docintel.openwebui.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.docintel.openwebui</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/.local/bin/open-webui</string>
    <string>serve</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>3000</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DATA_DIR</key><string>$HOME/open-webui-data</string>
    <key>OLLAMA_BASE_URL</key><string>http://127.0.0.1:11434</string>
    <key>OFFLINE_MODE</key><string>true</string>
    <key>HF_HUB_OFFLINE</key><string>1</string>
    <key>ENABLE_OPENAI_API</key><string>false</string>
    <key>SCARF_NO_ANALYTICS</key><string>true</string>
    <key>DO_NOT_TRACK</key><string>true</string>
    <key>ANONYMIZED_TELEMETRY</key><string>false</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/open-webui-data/openwebui.log</string>
  <key>StandardErrorPath</key><string>$HOME/open-webui-data/openwebui.log</string>
</dict>
</plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.docintel.openwebui.plist
```
- `DATA_DIR` keeps users, chats and settings in `~/open-webui-data`, so
  upgrading Open WebUI never wipes them.
- `--host 0.0.0.0` lets other computers on the office network open it at
  `http://<mac-ip-address>:3000`. Find the IP with
  `ipconfig getifaddr en0`.

### Option B — keep it in Docker

Environment variables can only be set when a container is created, so
the container is re-created with the same data volume (users and chats
are kept). First find the data volume name:
```bash
docker inspect <container-name> --format '{{range .Mounts}}{{.Name}} -> {{.Destination}}{{"\n"}}{{end}}'
```
Look for the line ending in `/app/backend/data`; the name before `->` is
the volume (commonly `open-webui`). Then:
```bash
docker stop <container-name>
docker rm <container-name>
docker run -d -p 3000:8080 \
  --add-host=host.docker.internal:host-gateway \
  -v <volume-name>:/app/backend/data \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  -e OFFLINE_MODE=true \
  -e HF_HUB_OFFLINE=1 \
  -e ENABLE_OPENAI_API=false \
  -e SCARF_NO_ANALYTICS=true \
  -e DO_NOT_TRACK=true \
  -e ANONYMIZED_TELEMETRY=false \
  --restart always \
  --name open-webui \
  ghcr.io/open-webui/open-webui:main
```
Two Docker-specific things to remember:
- Inside a container, `localhost` means the container itself, not the
  Mac. That's why Ollama is reached at `host.docker.internal`.
- For the same reason, when you add the Pipe function (README section
  10), set its `API_BASE` valve to `http://host.docker.internal:8080`
  instead of `http://localhost:8080`.

### Verify (either option)
Open `http://localhost:3000` in Safari on the Mac. Sign in, or create the
admin account on first run. Then Admin Panel → Settings → Connections:
the Ollama connection should be green, and no OpenAI connection listed.

---

## 6. Go offline and prove it

Disconnect: unplug the network cable and/or turn Wi-Fi off. Then:

```bash
ollama list
curl http://127.0.0.1:11434
cd ~/blueprint
UV_OFFLINE=1 uv run pytest tests/ -v
```
Expect: models listed, `Ollama is running`, and all tests passing — with
no network at all. (`UV_OFFLINE=1` makes `uv` fail loudly rather than
quietly try to download something.)

Then do one real end-to-end run: start the pipeline service (README
section 9), upload a document in Open WebUI and ask a question. It must
work with the cable unplugged.

Finally, check that nothing is trying to reach out:
```bash
lsof -nP -iTCP -sTCP:ESTABLISHED | grep -v 127.0.0.1
```
With the network disconnected this should print nothing, or only
connections between Open WebUI and the browsers on your local network.

---

## 7. Keep it running like a server

A Mac Studio used as a server should not sleep, and should come back on
its own after a power cut:

1. System Settings → Energy → turn on **Prevent automatic sleeping when
   the display is off** and **Start up automatically after a power
   failure**.
2. The background services (Ollama, Open WebUI, the pipeline) are
   LaunchAgents: they start when **your user account logs in**. After a
   reboot, someone has to log in — or enable System Settings → Users &
   Groups → **Automatically log in as** your account. (This option is
   not available while FileVault disk encryption is on; weigh that with
   whoever is responsible for the machine's security.)

---

## 8. Final checklist

- [ ] `ollama --version` → current release (0.19.0 absolute minimum)
- [ ] `ps aux | grep "[o]llama serve"` → `/opt/homebrew/...`, not `docker`
- [ ] `ollama list` → `qwen2.5-coder:32b` and `qwen2.5vl:32b`
- [ ] `launchctl list | grep com.docintel` → the Ollama service (and
      Open WebUI, if native)
- [ ] `curl http://127.0.0.1:11434` → `Ollama is running`
- [ ] Open WebUI loads at `http://<mac-ip>:3000` from another computer
- [ ] `uv run pytest tests/ -v` → all pass
- [ ] Section 6 done with the network physically disconnected
- [ ] After ingesting a scanned PDF, `ollama ps` shows **only**
      `qwen2.5-coder:32b` (the vision model was evicted — README
      section 5)
- [ ] Energy settings from section 7

---

## 9. If this Mac can never be connected to the internet

Then all downloads happen on a second Mac that is online, and the results
are carried over on an external drive:

1. On the online Mac, run sections 3.3–4 there.
2. Copy to the drive:
   - `~/.ollama/models` — the downloaded model files
   - the whole `~/blueprint` folder, **including** its `.venv`
   - `~/.local/bin/uv` — the `uv` program (a single file)
   - `~/.local/share/uv` — the Python interpreter `uv` downloaded; the
     `.venv` points into it and does not work without it
   - the standalone Ollama command-line build, `ollama-darwin.tgz`,
     from the Assets list of the latest release on Ollama's GitHub
     releases page. It needs no Homebrew.
3. On the offline Mac, put everything back at the same paths (same
   user name on both Macs keeps those paths identical), and
   unpack `ollama-darwin.tgz` into a folder such as `~/ollama`. Then
   write the LaunchAgent from `scripts/setup_mac.sh` step 4 by hand, with
   `ProgramArguments` pointing at `~/ollama/ollama`.

Both Macs should run the same macOS major version and the same Python
version, or the copied `.venv` may not work. This path is more fragile
than a one-time connected setup — use it only if policy really requires
it.

---

## 10. Next step

Continue with `README.md` — section 9 starts the pipeline service,
section 10 connects it to Open WebUI.
