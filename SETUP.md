# Mac Studio environment setup — from unknown state to verified

Use this when you don't actually know what's installed on the machine, or
don't trust that it was installed correctly. It assumes nothing about the
Mac's current state except that macOS is running. Every command includes
what it does and what the output should look like, so you're never just
typing something you don't understand.

**How to use this:** open Terminal (press `Cmd+Space`, type `Terminal`,
press Enter), then work through sections in order. Section 1 is read-only
— it only inspects the machine, it doesn't install or change anything.
Don't skip it: the whole point is to find out what's already there before
deciding what to install or remove.

Once every box in section 6 is checked, go back to `README.md` section 3
— `scripts/setup_mac.sh` will run cleanly because its assumptions have
actually been verified here, not guessed at.

---

## 1. Find out what's actually on this machine (inspect only, changes nothing)

### 1.1 Confirm the hardware and macOS version
```bash
sw_vers
uname -m
sysctl -n hw.memsize | awk '{print $1/1024/1024/1024 " GB RAM"}'
```
Expect: a macOS version line, `arm64` (confirms Apple Silicon, not Intel),
and `64 GB RAM` (or close to it — a few GB is reserved by the system).

### 1.2 Confirm you have admin rights
Installing anything (Homebrew, native Ollama) needs this. Check:
```bash
whoami
dscl . -read /Groups/admin GroupMembership
```
Your username from the first command should appear in the second
command's output. If it doesn't, stop here — you need to be added to the
admin group (or be given the credentials of an account that already is)
before continuing. Whoever manages this Mac needs to do that; no command
here can grant it to you.

### 1.3 Check for Docker
```bash
ls /Applications | grep -i docker
docker --version 2>&1
docker ps -a 2>&1
```
The last command lists every container, running or stopped. Note any
name/image containing `ollama` or `open-webui` — that tells you if either
service is currently running inside a container rather than natively.
This matters because Docker Desktop's Linux VM on Apple Silicon has
historically not had reliable Metal/GPU passthrough — a real, independent
explanation for hangs and slow responses, separate from anything in this
project's code.

### 1.4 Check for an existing Ollama install
```bash
which ollama
ollama --version 2>&1
ls -la /Applications | grep -i ollama
launchctl list | grep -i ollama
brew list 2>&1 | grep -i ollama
```
Interpreting this:
- `which ollama` prints a path (e.g. `/opt/homebrew/bin/ollama`) → it's
  installed natively.
- `which ollama` prints nothing, but you saw an `ollama` container in
  1.3 → it only exists inside Docker.
- Nothing anywhere → not installed at all.

If native, also check the version against what this project needs:
```bash
ollama --version
```
Versions before **0.19.0** have a documented bug where certain "thinking"
models leak a tool call into plain visible text instead of it being
parsed correctly. This project needs **0.34.0 or newer** (current stable
at time of writing) both for that fix and for the MLX inference backend
that native Ollama uses on Apple Silicon since v0.19.

### 1.5 Check for an existing Open WebUI install
```bash
docker ps -a | grep -i open-webui
pip3 show open-webui 2>&1
lsof -i :3000 2>&1
ps aux | grep -i open-webui
```
Same idea: Docker container, pip-installed native process, or nothing.

### 1.6 Check what models are already downloaded
```bash
ollama list
```
If Ollama only exists inside Docker, run this instead (replace the name
with what you found in 1.3):
```bash
docker exec -it <container-name> ollama list
```

### 1.7 Write down what you found
Before moving to section 2, you should be able to answer all of these:
- Ollama: native, Docker, or not installed? What version, if native?
- Open WebUI: native, Docker, or not installed?
- Do you have confirmed admin rights?
- Any models already pulled?
- Free disk space (`df -h /`) and the RAM figure from 1.1?

---

## 2. Decide what to keep vs. replace

| Component | Currently native | Currently in Docker | Not installed |
|---|---|---|---|
| **Ollama** | Keep if version ≥ 0.34.0 (upgrade if older — section 3.4) | **Move to native** (section 3) — this is the most likely fix for hangs/slow responses, and it's how this whole project is designed to run | Install native (section 3) |
| **Open WebUI** | Fine as-is | Fine to leave as-is — see below | Install (section 4) |

**Why Ollama matters more than Open WebUI here:** Ollama is the thing
actually doing GPU-accelerated inference — it needs real Metal access,
which is where Docker's VM layer is the likely problem. Open WebUI is
just a web UI and orchestration layer; it makes no GPU calls itself, so
whether it runs natively or in Docker has little practical effect. You
can leave it in Docker if it's already working and only move Ollama.

---

## 3. Installing / upgrading Ollama natively

Skip straight to 3.4 if Ollama is already native and up to date.

### 3.1 If Ollama is currently only in Docker, stop and remove that container
```bash
docker stop <container-name>
docker rm <container-name>
```
(use the actual name from `docker ps -a` in section 1.3 — don't guess it)

### 3.2 Install Xcode Command Line Tools
Homebrew requires these. This is Apple's own compiler/developer tools,
not Xcode itself (much smaller download).
```bash
xcode-select --install
```
A GUI installer window pops up — click **Install**, accept the license,
wait for it to finish (a few minutes, depending on connection). Verify:
```bash
xcode-select -p
```
Should print `/Library/Developer/CommandLineTools`.

### 3.3 Install Homebrew
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
This downloads and runs Homebrew's own official install script (from
Homebrew's GitHub — this is the standard, documented way to install it,
not a third-party mirror). It will:
- ask for your Mac login password (needed to create `/opt/homebrew`)
- print a "Next steps" block at the end — **follow exactly what it
  prints**, it's usually these two lines (run them, don't just read them):
```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```
This adds Homebrew to your shell's `PATH` so the `brew` command works in
new terminal windows. Verify:
```bash
brew --version
```

### 3.4 Install or upgrade Ollama
If it wasn't installed at all:
```bash
brew install ollama
```
If it's native but older than 0.34.0:
```bash
brew upgrade ollama
```
Run it as a persistent background service, so it survives reboots and
you never have to manually start it:
```bash
brew services start ollama
```
Verify:
```bash
ollama --version           # want 0.34.0 or newer
curl http://localhost:11434  # should respond "Ollama is running"
```

### 3.5 Pull the models this pipeline uses
```bash
ollama pull qwen2.5-coder:32b
ollama pull llava
```
Both together are tens of GB — check you have disk space first
(`df -h /`) and expect this to take a while depending on your connection.

### 3.6 Raise the context window default
```bash
launchctl setenv OLLAMA_CONTEXT_LENGTH 32768
echo 'export OLLAMA_CONTEXT_LENGTH=32768' >> ~/.zprofile
```
The second line makes it survive a reboot; the first applies it to the
current session immediately.

---

## 4. Installing Open WebUI (only if missing, or you're deliberately moving it off Docker)

Pick one — both are fine, this is not a performance-sensitive choice
(see section 2):

**Option A — native, via `uv`** (recommended only for consistency with
the rest of this project, which is `uv`-managed):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install open-webui
open-webui serve
```

**Option B — Docker** (perfectly fine to leave here if it's already
working):
```bash
docker run -d -p 3000:8080 \
  --add-host=host.docker.internal:host-gateway \
  -v open-webui:/app/backend/data \
  --name open-webui \
  ghcr.io/open-webui/open-webui:main
```
Important if you use this option: inside the container, Ollama's address
must be `http://host.docker.internal:11434`, **not** `localhost` — the
container is a separate network namespace from the native Ollama process
running on the Mac itself. Set this under Open WebUI's Admin Panel →
Settings → Connections.

Verify either option: open `http://localhost:3000` in a browser and sign
in / create the admin account if this is the first run.

---

## 5. Installing `uv` (this pipeline's own Python dependency manager)

Skip if you already ran this in section 4, Option A.
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
Verify:
```bash
uv --version
```

---

## 6. Final verification checklist

Run each and confirm before moving to the main `README.md`:

- [ ] `ollama --version` → 0.34.0 or newer
- [ ] `ps aux | grep ollama` → shows a native process path (e.g.
      `/opt/homebrew/bin/ollama`), **not** a line containing `docker`
- [ ] `ollama list` → shows `qwen2.5-coder:32b` and `llava`
- [ ] `curl http://localhost:11434` → `Ollama is running`
- [ ] `curl http://localhost:3000` (or wherever Open WebUI listens) →
      responds, and you can log into it in a browser
- [ ] `uv --version` → works
- [ ] `df -h /` → enough free disk space for document uploads and model
      storage
- [ ] Activity Monitor → enough free RAM headroom before you start
      loading large documents

---

## 7. Next step

Continue with `README.md`, section 3 onward — the environment this
project's own setup script and code assume is now actually verified.
