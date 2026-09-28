# Mac Studio setup guide

This guide takes the Mac Studio from "someone installed something, we're
not sure what" to a working system that staff can use from their own
computers. It assumes no experience with Terminal or macOS
administration. Follow it from top to bottom and don't skip sections.

**What you end up with:**
- Ollama running natively on the Mac (not in Docker), with two models:
  `qwen2.5-coder:32b` (answers questions by calculating) and
  `qwen2.5vl:32b` (reads scanned pages)
- the pipeline service from this project, running in the background
- Open WebUI, where staff attach a document and ask questions about it
- everything restarting on its own after a reboot

**Time needed:** about 1 to 3 hours. Most of it is waiting for downloads.

**You need:** the Mac's admin password, an internet connection, and this
guide open on a second screen or another computer.

---

## Before you start: how to use Terminal

Terminal is the app where you type commands. Everything in the grey
boxes below is a command.

1. **Open Terminal:** press `Cmd + Space`, type `Terminal`, press Enter.
   A window opens with a line ending in `%`. That line is the *prompt*:
   it means Terminal is waiting for you.
2. **Copy a command:** on GitHub, move the mouse over a grey box and
   click the copy icon in its top right corner. Or select the text and
   press `Cmd + C`.
3. **Paste and run it:** click into the Terminal window, press `Cmd + V`,
   then press Enter.
4. **Wait for the prompt.** A command is finished when the line ending in
   `%` appears again. Some commands take a few seconds, some take an hour.
   Don't type anything new until the prompt is back.
5. **Passwords are invisible.** When Terminal asks for your password,
   nothing appears while you type, not even dots. That's normal. Type it
   and press Enter.
6. **To stop a command** that seems stuck, press `Control + C`.
7. **Anything in `<angle brackets>`** is a placeholder. Replace it,
   including the brackets, with the real value. Example: if a command
   says `docker stop <container-name>` and the name is `ollama`, you type
   `docker stop ollama`.
8. **If you close Terminal,** just open it again. To get back into the
   project folder, run `cd ~/blueprint`.

Coloured text or the word "warning" is usually fine. The setup script
prints `STOPPED:` when something really is wrong, and tells you what.

---

## 0. What the internet connection is (and isn't) used for

The Mac is connected to the internet. That connection is used **only to
download and update software and models**: Homebrew packages, Python
packages, Ollama itself, and models (about 45 GB for the two this project
uses; you can add others later, see section 9).

It is **never** used to process documents. Uploaded files, the questions
asked about them and the answers all stay on the Mac. The models run
locally in Ollama, the data sits in local files, and no cloud AI service
or API key is involved anywhere. Section 5 switches off the parts of Open
WebUI that would otherwise talk to outside services.

---

## 1. Find out what's on this Mac (this changes nothing)

Run these one at a time and read what comes back. Write the results into
the table in 1.7.

### 1.1 Hardware, macOS version and free disk space
```bash
sw_vers
uname -m
sysctl -n hw.memsize | awk '{print $1/1024/1024/1024 " GB RAM"}'
df -h /
```
Expect:
- a macOS version number (`ProductVersion`)
- `arm64` (this means Apple Silicon, which is what we need)
- `64 GB RAM`
- in the last command, the `Avail` column: at least **80 GB free**
  (models take about 45 GB, plus room for documents)

### 1.2 Do you have admin rights?
Easiest way: open **System Settings → Users & Groups**. Your account
should say **Admin** under its name.

Or in Terminal:
```bash
whoami
dscl . -read /Groups/admin GroupMembership
```
The name printed by the first command must appear in the second one.

If you are not an admin, stop here. Whoever manages this Mac has to make
your account an admin first. Nothing in this guide can do that for you.

### 1.3 Is Docker installed, and what runs in it?
```bash
ls /Applications | grep -i docker
docker ps -a
```
- If the first command prints nothing and the second says
  `command not found`, Docker isn't installed. That's fine.
- If the second command says `Cannot connect to the Docker daemon`, Docker
  is installed but not running. Open the **Docker** app from Applications,
  wait until the whale icon in the menu bar stops moving, and run
  `docker ps -a` again.
- Otherwise it lists containers. Note every row whose `NAMES` or `IMAGE`
  column contains `ollama` or `open-webui`, and write down the name.

Why it matters: Docker on a Mac runs inside a virtual machine that can't
use the Mac's graphics chip. Ollama in Docker therefore runs on the CPU
only, which is much slower and a likely cause of hanging and timeouts.

### 1.4 Is Ollama installed, and how?
```bash
which ollama
ollama --version
ls /Applications | grep -i ollama
```
How to read it:
- `which ollama` prints a path such as `/opt/homebrew/bin/ollama`: Ollama
  is installed natively.
- `ls /Applications` shows `Ollama.app`: the Ollama **desktop app** is
  installed. It will have to be quit (section 3.2).
- `which ollama` prints nothing, but 1.3 showed an ollama container: it
  only exists in Docker.
- Nothing anywhere: not installed. The setup script will install it.

### 1.5 Is Open WebUI installed, and how?
```bash
docker ps -a | grep -i open-webui
which open-webui
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(3000|8080) '
```
Then open Safari on the Mac and go to `http://localhost:3000`. If a
login page appears, Open WebUI is installed and running.

Note which of these is true: it runs in Docker (first command printed a
line), it's installed natively (second command printed a path), or it's
not installed.

### 1.6 Which models are already downloaded?
```bash
ollama list
```
If Ollama only exists in Docker, use the container name from 1.3:
```bash
docker exec -it <container-name> ollama list
```
Old models aren't harmful, but each one takes disk space. You can remove
one later with `ollama rm <model-name>`.

### 1.7 Write down what you found

| Question | Your answer |
|---|---|
| Admin rights? | yes / no |
| Free disk space | ___ GB |
| Ollama is | native / desktop app / in Docker / not installed |
| Ollama container name (if Docker) | ___ |
| Open WebUI is | native / in Docker / not installed |
| Open WebUI container name (if Docker) | ___ |

---

## 2. Decide what to keep and what to replace

| | If it's native | If it's in Docker | If it's not installed |
|---|---|---|---|
| **Ollama** | Keep it. The setup script upgrades it and runs it properly. | **Replace it** with native Ollama (section 3.1). | The setup script installs it. |
| **Open WebUI** | Keep it. | Keep it, Docker is fine for Open WebUI (section 5, option B). | Install it natively (section 5, option A). |

**Why only Ollama has to leave Docker:** Ollama does the heavy work and
needs the Mac's graphics chip, which Docker can't give it. Open WebUI is
only the web page staff use; it does no heavy work, so Docker costs it
nothing. If Open WebUI is already in Docker with user accounts and chats,
leave it there.

---

## 3. Prepare the Mac

### 3.1 Only if Ollama runs in Docker: remove that container
Use the exact name you wrote down in 1.7:
```bash
docker stop <container-name>
docker rm <container-name>
```
This removes only that container, not Docker and not Open WebUI. Its
downloaded models are not reused; the native install downloads them
again. (The setup script also offers to do this for you.)

### 3.2 Only if the Ollama desktop app is installed: quit it for good
The desktop app starts its own copy of Ollama every time someone logs in,
which blocks the one this project sets up.
1. Click the llama icon in the menu bar at the top of the screen, then
   **Quit Ollama**.
2. Open **System Settings → General → Login Items & Extensions** (on
   older macOS: **Login Items**). Under "Open at Login", select
   **Ollama** and click the **minus** button below the list.
3. Recommended: open Finder → Applications and drag **Ollama** to the
   Trash.

### 3.3 Xcode Command Line Tools
These are Apple's developer tools. Homebrew and `git` need them. It's a
small download, not the full Xcode app.
```bash
xcode-select --install
```
- A window appears: click **Install**, then **Agree**, and wait until it
  says the software was installed (a few minutes).
- If Terminal says `command line tools are already installed`, that's
  fine, nothing to do.

Check:
```bash
xcode-select -p
```
It should print `/Library/Developer/CommandLineTools`.

### 3.4 Homebrew
Homebrew installs command-line programs on a Mac. First check if it's
already there:
```bash
brew --version
```
If this prints a version number, skip to section 4. If it says
`command not found`, install it:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
This is Homebrew's official installer. While it runs:
- it asks for your Mac password (typing is invisible, then Enter)
- it says `Press RETURN to continue`: press Enter
- it takes a few minutes
- at the end it prints **"Next steps"** with two or three commands. Copy
  and run exactly the commands it shows. On an Apple Silicon Mac they are
  normally these two:
```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```
Now **close the Terminal window and open a new one** (so it picks up the
change), then check:
```bash
brew --version
```
It should print a version number such as `Homebrew 4.x.x`.

---

## 4. Download the project and run the setup script

### 4.1 Download the project
```bash
cd ~
git clone https://github.com/Mirza-Adnan-Baig/blueprint.git
cd blueprint
```
This creates a folder called `blueprint` in your home folder and moves
into it. If git says the folder `already exists`, the project is already
there; update it instead:
```bash
cd ~/blueprint
git pull
```

### 4.2 Run the setup script
```bash
bash scripts/setup_mac.sh
```
Leave the Terminal window open while it runs. It does everything below
by itself and prints a heading for each step. The model downloads (step
7) are the slow part: expect 30 minutes to several hours depending on the
internet connection.

| Step | What it does |
|---|---|
| 1 | Checks this is an Apple Silicon Mac. |
| 2 | Looks for Ollama inside Docker. If it finds a container, it asks whether to remove it: type `y` and press Enter. |
| 3 | Checks Homebrew is installed and switches off Homebrew's usage statistics. |
| 4 | Installs Ollama natively, or upgrades it if it's already there. |
| 5 | Makes sure nothing else is using Ollama's port. If the Ollama desktop app is running, it quits it. If something else blocks the port, it stops and tells you what. |
| 6 | Starts Ollama as a background service that also starts after every reboot, with the memory settings this project needs. |
| 7 | Downloads the two models (about 45 GB). |
| 8 | Installs `uv` (Python's package manager) and this project's Python packages. |
| 9 | Creates the settings file `.env`. |
| 10 | Runs the project's automatic tests. They must all pass. |
| 11 | Starts the pipeline service and the hourly cleanup job as background services, then waits until the pipeline answers. |

When it's finished it prints `All done`. If it prints `STOPPED:`
instead, read the line after it and go to section 10.

The script is safe to run again at any time. It skips what's already
done and restarts the services.

### 4.3 Check the result
```bash
ollama --version
ollama list
curl http://127.0.0.1:11434
curl http://127.0.0.1:8080/health
```
Expect:
- an Ollama version number
- both `qwen2.5-coder:32b` and `qwen2.5vl:32b` in the list
- `Ollama is running`
- `{"status":"ok"}`

### 4.4 The Ollama settings, for reference
The script puts these into the Ollama background service. You don't
need to change them.

| Setting | Value | Why |
|---|---|---|
| `OLLAMA_HOST` | `127.0.0.1:11434` | Only programs on this Mac can reach Ollama. |
| `OLLAMA_CONTEXT_LENGTH` | `32768` | How much text a model can take in at once. |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | Never two big models in memory at the same time (64 GB isn't enough for both). |
| `OLLAMA_NUM_PARALLEL` | `1` | One request at a time per model; parallel slots each need extra memory. |
| `OLLAMA_KEEP_ALIVE` | `5m` | Unload a model after 5 idle minutes, unless the pipeline pinned it. |

---

## 5. Open WebUI

Whichever option applies, Open WebUI gets these settings so that nothing
leaves the Mac:

| Setting | Value | Effect |
|---|---|---|
| `ENABLE_OPENAI_API` | `false` | Removes the built-in connection to OpenAI's cloud, so no chat can go to a cloud model by accident. |
| `SCARF_NO_ANALYTICS`, `DO_NOT_TRACK` | `true` | No usage statistics are sent. |
| `ANONYMIZED_TELEMETRY` | `false` | No telemetry from the built-in search database. |

### Option A: install Open WebUI natively
Use this if Open WebUI is not installed yet. (If it runs in Docker and
you'd rather keep it there, use option B instead.)

1. Make `uv` available in this Terminal window (the setup script
   installed it):
   ```bash
   source $HOME/.local/bin/env
   ```
2. Install Open WebUI (a few minutes):
   ```bash
   uv tool install --python 3.11 open-webui
   ```
3. Set it up as a background service. Copy this **whole** box at once;
   it writes a settings file and starts Open WebUI:
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
4. **The first start takes a few minutes** (it downloads a small helper
   model once). If macOS asks whether to **allow incoming network
   connections**, click **Allow**; otherwise other computers can't reach
   Open WebUI.
5. Open Safari on the Mac and go to `http://localhost:3000`. When the
   page appears, click **Get started** and create the first account.
   **The first account becomes the admin account.** Use a strong password
   and keep it somewhere safe.

What the settings mean: `DATA_DIR` keeps all users, chats and settings in
the folder `open-webui-data` in your home folder, so updating Open WebUI
never deletes them. `--host 0.0.0.0` lets other computers in the office
reach it.

### Option B: keep Open WebUI in Docker
Settings can only be given to a Docker container when it's created, so
the container is created again. Its users and chats are kept, because
they live in a separate storage area (a "volume") that is reused.

1. Find the volume name. Use the Open WebUI container name from 1.7:
   ```bash
   docker inspect <container-name> --format '{{range .Mounts}}{{.Name}} -> {{.Destination}}{{"\n"}}{{end}}'
   ```
   Find the line that ends in `/app/backend/data`. The word before `->`
   is the volume name (often `open-webui`). Write it down.
2. Re-create the container. Replace both placeholders, then copy the
   whole box:
   ```bash
   docker stop <container-name>
   docker rm <container-name>
   docker run -d -p 3000:8080 \
     --add-host=host.docker.internal:host-gateway \
     -v <volume-name>:/app/backend/data \
     -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
     -e ENABLE_OPENAI_API=false \
     -e SCARF_NO_ANALYTICS=true \
     -e DO_NOT_TRACK=true \
     -e ANONYMIZED_TELEMETRY=false \
     --restart always \
     --name open-webui \
     ghcr.io/open-webui/open-webui:main
   ```
   Be careful with the volume name: a typo creates a new, empty volume
   and Open WebUI starts with no users. If that happens, stop and remove
   the container again and repeat with the correct name.
3. Make Docker start by itself after a reboot: open the Docker app →
   **Settings** (gear icon) → **General** → tick **Start Docker Desktop
   when you sign in** → **Apply**.
4. Remember for section 6: inside Docker, `localhost` means the container
   itself, not the Mac. That's why Ollama is reached at
   `host.docker.internal`, and why the Pipe's address in section 6.2 is
   different for Docker.

### Check Open WebUI (both options)
1. Open `http://localhost:3000` in Safari on the Mac and sign in with the
   admin account.
2. Click your name (bottom left) → **Admin Panel** → **Settings** →
   **Connections**. The Ollama connection should be listed and work (a
   green tick or no error). There should be no OpenAI connection.
3. From another computer in the office, open `http://<mac-ip>:3000`. To
   find the Mac's IP address:
   ```bash
   ipconfig getifaddr en0
   ```
   If that prints nothing (the Mac uses Wi-Fi rather than a cable), try
   `ipconfig getifaddr en1`.

---

## 6. Connect Open WebUI to the pipeline

### 6.1 Add the "Document Intelligence" function
1. On the Mac, open the file with the function's code:
   ```bash
   open -e ~/blueprint/openwebui/pipe_function.py
   ```
   It opens in TextEdit. Press `Cmd + A` (select all), then `Cmd + C`
   (copy). Close TextEdit without changing anything.
2. In Open WebUI: your name (bottom left) → **Admin Panel** →
   **Functions** → the **+** button (New Function).
3. Click into the code area, press `Cmd + A` and then Delete to remove
   the example code, then press `Cmd + V` to paste.
4. Check the first lines of the code area start with `"""` and
   `title: Document Intelligence`. Then click **Save**.
5. Back in the Functions list, switch **Document Intelligence** on with
   its toggle on the right.

**Important when updating the function later:** always select everything
in the code area and delete it before pasting the new version. If you
paste without clearing it first, the old code stays in the file and
Open WebUI may keep running the old version.

### 6.2 Check the function's settings ("Valves")
In the Functions list, click the gear icon next to Document Intelligence.

| Setting | What to enter |
|---|---|
| `API_BASE` | `http://localhost:8080` if Open WebUI was installed natively (option A). `http://host.docker.internal:8080` if Open WebUI runs in Docker (option B). |
| `LANGUAGE` | `de` for German status messages, `en` for English. |
| The three `..._SECONDS` settings | Leave as they are. |

Click **Save**.

### 6.3 Let staff see it, and switch off File Context
A new function is **only visible to admins** until you change this.
1. Admin Panel → **Settings** → **Models**. Find **Document
   Intelligence** and click the pencil icon (edit).
2. Find **Visibility** (in some versions: **Access**) and set it to
   **Public**, or add the groups of people who should use it.
3. Under **Capabilities**, untick **File Context** if you see that
   option. This stops Open WebUI from also running its own (inaccurate)
   document search on every upload. The pipeline works either way, but
   it's faster with it off. Leave **File Upload** ticked.
4. Click **Save**.
5. To check: sign in as a normal (non-admin) user and make sure
   "Document Intelligence" appears in the model list at the top of a new
   chat.

### 6.4 First test with the sample file
The project includes a small test file with answers you can check. Copy
it to your Desktop:
```bash
cp ~/blueprint/samples/lager_beispiel.csv ~/Desktop/
```
1. In Open WebUI, start a **New Chat**.
2. At the top, choose **Document Intelligence** as the model.
3. Click the **+** or paperclip icon in the message box, choose
   **Upload Files**, and pick `lager_beispiel.csv` from the Desktop.
4. Ask each of these questions, one message at a time, and compare with
   the expected answer:

| Question | Expected answer |
|---|---|
| Wie viele Stück sind insgesamt auf Lager? | 4.885 |
| Wie viele Stück liegen im Lager Hamburg? | 2.710 |
| Welchen Gesamtwert hat der Bestand (Menge mal Einzelpreis)? | 27.582,20 € |
| Wie viele verschiedene Bezeichnungen gibt es? | 4 |

While it works, a status line above the answer says what's happening
("Dokument wird eingelesen", "Antwort wird berechnet"). The very first
question after a restart can take a minute or two while the model loads.
Follow-up questions in the same chat don't need the file attached again.

If an answer is wrong or doesn't come, go to section 10.

---

## 7. Check the whole system

Run these and compare:
```bash
cd ~/blueprint
ollama list
curl http://127.0.0.1:11434
curl http://127.0.0.1:8080/health
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(11434|8080|3000) '
```
Expect: both models listed, `Ollama is running`, `{"status":"ok"}`, and
in the last command `127.0.0.1:11434` (Ollama) and `127.0.0.1:8080`
(pipeline), which means only the Mac itself can reach them, plus `*:3000`
for Open WebUI, the only thing office computers are meant to open.

Then test a **scanned PDF** (a real scan, not a PDF made from Word or
Excel). Upload it in a new chat, ask one question, and when the answer
has arrived run:
```bash
ollama ps
```
It must show **only** `qwen2.5-coder:32b`. The vision model is unloaded
as soon as a document has been read, because both models don't fit in
memory together.

---

## 8. Keep it running like a server

1. **No sleeping:** System Settings → **Energy** → turn on **Prevent
   automatic sleeping when the display is off** and **Start up
   automatically after a power failure**. (On older macOS: System
   Settings → Energy Saver.)
2. **Automatic login after a restart.** The background services start
   when your user account logs in. After a reboot or power cut, someone
   has to log in, or you switch on System Settings → **Users & Groups** →
   **Automatically log in as** → your account. This option is greyed out
   while FileVault disk encryption is on; decide together with whoever is
   responsible for the Mac's security.
3. If Open WebUI runs in Docker: Docker must start at login (section 5,
   option B, step 3).
4. After the first reboot, check with the commands in section 7 that
   everything came back by itself.

---

## 9. Daily use and maintenance

### Where the logs are
Logs are text files that record what happened. Open one with:
```bash
open -e ~/blueprint/logs/pipeline.log
```
| File | What's in it |
|---|---|
| `~/blueprint/logs/pipeline.log` | Every document read and every question, including errors. Look here first. |
| `~/blueprint/logs/service.log` | Start-up messages of the pipeline service. |
| `~/blueprint/logs/ollama.log` | Ollama's own messages. |
| `~/blueprint/logs/gc.log` | The hourly cleanup. |
| `~/open-webui-data/openwebui.log` | Open WebUI (option A only). For Docker: `docker logs open-webui`. |

### Restart a service
```bash
launchctl kickstart -k gui/$(id -u)/com.docintel.pipeline
launchctl kickstart -k gui/$(id -u)/com.docintel.ollama
launchctl kickstart -k gui/$(id -u)/com.docintel.openwebui
```
Run only the line you need. (The last one is for option A only; for
Docker use `docker restart open-webui`.)

### Change a setting
Settings are in the file `.env`:
```bash
open -e ~/blueprint/.env
```
Change the value after the `=`, save with `Cmd + S`, close TextEdit, then
restart the pipeline (first line above). Each setting is explained in
the file itself.

### Uploaded files are deleted after 24 hours
The cleanup job removes uploaded documents and their data after 24
hours (`GC_MAX_AGE_HOURS` in `.env`). After that, a question in an old
chat says the document should be uploaded again.

### Update everything
```bash
cd ~/blueprint
git pull
bash scripts/setup_mac.sh
```
This gets the newest version of the project, upgrades Ollama, and
restarts everything. If `pipe_function.py` changed, also paste it into
Open WebUI again (section 6.1, clear the code area first).

Update Open WebUI, option A:
```bash
source $HOME/.local/bin/env
uv tool upgrade open-webui
launchctl kickstart -k gui/$(id -u)/com.docintel.openwebui
```
Option B: `docker pull ghcr.io/open-webui/open-webui:main`, then repeat
section 5, option B, step 2 with the same volume name.

### Try a different model
```bash
ollama pull <model-name>
```
Then put its name after `CODE_MODEL=` (the model that writes the
calculations) or `VISION_MODEL=` (the one that reads scans) in `.env`,
and restart the pipeline. Keep the 64 GB limit in mind: one model at a
time, and it should need less than about 30 GB including its context,
which in practice means models up to about 32B parameters. `ollama ps`
shows what a loaded model really uses. Remove a model you no longer
need with `ollama rm <model-name>`.

---

## 10. If something goes wrong

| What you see | What to do |
|---|---|
| The setup script prints `STOPPED: ... port 11434` | Something else runs Ollama. Quit the Ollama desktop app (3.2), remove Ollama containers (3.1), then run the script again. |
| The setup script prints `STOPPED: some tests failed` | Copy the Terminal output into a message to whoever maintains this project. Don't continue. |
| The setup script stops during the model download | Usually the internet connection dropped. Run the script again; it continues where it stopped. |
| Open WebUI says "Der Dokumenten-Dienst ist gerade nicht erreichbar" | The pipeline isn't running. Run `curl http://127.0.0.1:8080/health`. If that fails, restart it (section 9) and look at `logs/service.log`. If Open WebUI runs in Docker, check that `API_BASE` is `http://host.docker.internal:8080` (6.2). |
| "Bitte laden Sie zuerst ein Dokument hoch" | No document in this chat yet, or it was deleted after 24 hours. Attach the file again. |
| Staff can't see "Document Intelligence" | Set its visibility to Public (6.3). |
| An answer takes several minutes | Normal for scanned PDFs: every page is read by the vision model. Watch the status line. Excel, CSV and normal PDFs should take under a minute. |
| An answer looks wrong | Open `logs/pipeline.log` (section 9) and find the question. Below it, under `code used:`, is the exact calculation, and under `result:` the number it produced. If a number column was read wrongly, note the file and the column. |
| `ollama ps` still shows `qwen2.5vl:32b` minutes after reading a document | Run `ollama stop qwen2.5vl:32b`, then look for `FAILED to evict` in `logs/pipeline.log`. |
| The Mac feels frozen or very slow | Open Activity Monitor → Memory. If "Memory Pressure" is red, too much is loaded: restart Ollama (section 9). |
| `command not found: brew` | Close Terminal, open a new window. If it's still missing, repeat 3.4. |
| `command not found: uv` | Run `source $HOME/.local/bin/env`, or open a new Terminal window. |
| Anything else | Restart everything by running `bash scripts/setup_mac.sh` again from `~/blueprint`. It is safe to repeat. |

---

## 11. Final checklist

- [ ] `ollama --version` prints a version number
- [ ] `ps aux | grep "[o]llama serve"` shows a path with `/opt/homebrew/`,
      not `docker`
- [ ] `ollama list` shows `qwen2.5-coder:32b` and `qwen2.5vl:32b`
- [ ] `curl http://127.0.0.1:8080/health` prints `{"status":"ok"}`
- [ ] `launchctl list | grep com.docintel` shows `ollama`, `pipeline` and
      `gc` (and `openwebui` if installed natively)
- [ ] Open WebUI opens at `http://<mac-ip>:3000` from another computer
- [ ] "Document Intelligence" is switched on, visible to normal users,
      and `API_BASE` is right for your setup (6.2)
- [ ] The sample file gives all four expected answers (6.4)
- [ ] After a scanned PDF, `ollama ps` shows only `qwen2.5-coder:32b` (7)
- [ ] Energy and login settings done (8), and everything came back after
      one test reboot
