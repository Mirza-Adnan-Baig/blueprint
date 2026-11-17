# Mac Studio setup guide

This guide takes a **freshly reset Mac Studio, with nothing installed**,
to a working system that staff can use from their own computers. It
assumes no experience with Terminal or macOS administration. Follow it
from top to bottom and don't skip sections.

**What you end up with:**
- Ollama running on the Mac, with two models: `qwen2.5-coder:32b`
  (answers questions by calculating) and `qwen2.5vl:32b` (reads scanned
  pages and photos)
- the pipeline service from this project, running in the background
- Open WebUI, the web page where staff attach a document and ask
  questions about it
- everything starting again by itself after a reboot

**Time needed:** about 1 to 3 hours. Most of it is waiting for downloads.

**You need:** the Mac's admin password, an internet connection, and this
guide open on a second screen or another computer.

### Everything that gets installed, and which step installs it

A reset Mac has none of this. Two steps are done by hand; the setup
script installs everything else.

| What | What it's for | Installed by |
|---|---|---|
| Xcode Command Line Tools, which include **git** | git downloads this project from GitHub. | **You, by hand:** section 2 |
| **Homebrew** | Installs command-line programs such as Ollama. | **You, by hand:** section 3 (it asks for your password) |
| **Ollama** | Runs the AI models. | The setup script, step 4 |
| The two **models** | The AI itself. About 45 GB. | The setup script, step 7 |
| **uv** | Installs Python and the Python libraries. | The setup script, step 8 |
| **Python 3.12** | The language the project is written in. | The setup script, step 8 (uv downloads it) |
| **pandas, DuckDB, pdfplumber, PyMuPDF, openpyxl, FastAPI, uvicorn** and a few more | The Python libraries the code uses. The full list is in `pyproject.toml`. | The setup script, step 8 |
| **Open WebUI** | The web page staff use. | The setup script, step 12 |
| **VS Code** | Only if you want to read or edit the code. Not needed to run anything. | Optional, by you: section 4.4 |

Nothing else is needed: no Docker, no separate Python download, no
GitHub account. The Python libraries go into a folder inside the
project, `~/blueprint/.venv`, and don't touch the rest of the Mac.

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
   including the brackets, with the real value.
8. **If you close Terminal,** just open it again. To get back into the
   project folder, run `cd ~/blueprint`.

Coloured text or the word "warning" is usually fine. The setup script
prints `STOPPED:` when something really is wrong, and tells you what.

### What the internet connection is (and isn't) used for

The internet is used **only to download and update software and
models**. It is **never** used to process documents: uploaded files, the
questions and the answers all stay on the Mac. The models run locally,
the data sits in local files, and no cloud AI service or API key is
involved anywhere.

---

## 1. Check the Mac (this changes nothing)

### 1.1 Hardware, macOS and free disk space
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
- in the last command, the `Avail` column: at least **100 GB free**
  (models about 45 GB, Open WebUI a few GB, plus room for documents)

### 1.2 Do you have admin rights?
Open **System Settings → Users & Groups**. Your account must say
**Admin** under its name.

If it doesn't, stop here. Whoever manages this Mac has to make your
account an admin first. Nothing in this guide can do that for you.

### 1.3 Install macOS updates
**System Settings → General → Software Update.** Install anything that's
offered and restart if asked. A freshly reset Mac often has updates
waiting, and doing them now avoids a restart in the middle of the setup.

### 1.4 Give the Mac a name you'll recognise (optional)
**System Settings → General → About → Name**, for example `Mac Studio
KI`. It makes the Mac easy to find in the office network later.

---

## 2. Xcode Command Line Tools (this installs git)

These are Apple's basic developer tools. They include **git**, which
section 4 uses to download this project, and Homebrew needs them too.
It's a small download (about 1 GB), not the full Xcode app. They also
include an old Python (3.9), which this project does **not** use.

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
git --version
```
The first should print `/Library/Developer/CommandLineTools`, the second
a version such as `git version 2.39.5`.

---

## 3. Homebrew

Homebrew installs command-line programs on a Mac.
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
into it. The project is public on GitHub, so no account or login is
needed.

### 4.2 Run the setup script
```bash
bash scripts/setup_mac.sh
```
Leave the Terminal window open while it runs. It prints a heading for
each step. The model download (step 7) is the slow part: 30 minutes to
several hours depending on the internet connection.

| Step | What it does |
|---|---|
| 1 | Checks this is an Apple Silicon Mac. |
| 2 | Looks for Ollama inside Docker. On a reset Mac there is none, so it just says OK. |
| 3 | Checks Homebrew and switches off Homebrew's usage statistics. |
| 4 | Installs Ollama. |
| 5 | Makes sure nothing else is using Ollama's port (11434). |
| 6 | Starts Ollama as a background service, with the memory settings this project needs. |
| 7 | Downloads the two models (about 45 GB). |
| 8 | Installs `uv`, then Python 3.12, then every Python library the project needs, all into `~/blueprint/.venv`. At the end it prints the installed versions. |
| 9 | Creates the settings file `.env`. |
| 10 | Runs the project's automatic tests. They must all pass. |
| 11 | Starts the pipeline service and the hourly cleanup job, then waits until the pipeline answers. |
| 12 | Installs Open WebUI (a few GB) and starts it as a background service. Its first start takes several minutes. |

When it's finished it prints `All done` and two addresses: the one for
this Mac (`http://localhost:3000`) and the one for office computers
(`http://192.168.x.x:3000`). **Write the second one down.** If it prints
`STOPPED:` instead, read the line after it and go to section 10.

**macOS messages you will see while it runs:**
- **"Background Items Added"** (mentioning `ollama`, `uvicorn`,
  `python`, `open-webui` or `bash`): expected, it's the background
  services being set up. **Don't switch them off.**
- **"Do you want the application python to accept incoming network
  connections?"**: click **Allow**. Otherwise office computers can't
  reach Open WebUI.

The script is safe to run again at any time. It skips what's already
done and restarts the services.

### 4.3 Check the result
```bash
ollama --version
ollama list
curl http://127.0.0.1:11434
curl http://127.0.0.1:8080/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3000
```
Expect:
- an Ollama version number
- both `qwen2.5-coder:32b` and `qwen2.5vl:32b` in the list
- `Ollama is running`
- `{"status":"ok"}`
- `200` (Open WebUI is up; if it says `000`, wait two minutes and try
  again, the first start is slow)

### 4.4 Optional: VS Code, for reading and editing the code
Nothing here needs VS Code; install it only if you want to work on the
code on the Mac.

1. Install it:
   ```bash
   brew install --cask visual-studio-code
   ```
2. Open the project:
   ```bash
   code ~/blueprint
   ```
   If VS Code asks **"Do you trust the authors of the files in this
   folder?"**, click **Yes, I trust the authors**.
3. Click the Extensions icon on the left (four small squares), search for
   **Python**, and install the one published by **Microsoft**.
4. Press `Cmd + Shift + P`, type **Python: Select Interpreter**, press
   Enter, and choose the entry that shows `.venv/bin/python`.
5. Menu **Terminal → New Terminal** opens a terminal already inside
   `~/blueprint`; every command in this guide works there too.

After changing code, restart the pipeline (section 9, "Restart a
service") and run the tests with `uv run pytest`. Don't start a second
copy of the service from VS Code's Run button; both would want port 8080.

### 4.5 For reference: the settings the script uses

Ollama:

| Setting | Value | Why |
|---|---|---|
| `OLLAMA_HOST` | `127.0.0.1:11434` | Only programs on this Mac can reach Ollama. |
| `OLLAMA_CONTEXT_LENGTH` | `32768` | How much text a model can take in at once. |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | Never two big models in memory at the same time (64 GB isn't enough for both). |
| `OLLAMA_NUM_PARALLEL` | `1` | One request at a time per model; parallel slots each need extra memory. |
| `OLLAMA_KEEP_ALIVE` | `5m` | Unload a model after 5 idle minutes, unless the pipeline pinned it. |

Open WebUI:

| Setting | Value | Why |
|---|---|---|
| `DATA_DIR` | `~/open-webui-data` | Users, chats and settings live here, so updates never delete them. |
| `--host 0.0.0.0`, `--port 3000` | | Office computers can open it at `http://<mac-ip>:3000`. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Where Ollama is. |
| `ENABLE_OPENAI_API` | `false` | No connection to OpenAI's cloud. |
| `BYPASS_EMBEDDING_AND_RETRIEVAL` | `true` | Open WebUI doesn't try to index uploads itself. Without this, it rejects scanned PDFs ("The content provided is empty") before the pipeline sees them. |
| `SCARF_NO_ANALYTICS`, `DO_NOT_TRACK`, `ANONYMIZED_TELEMETRY` | | No usage statistics are sent. |

Open WebUI only reads these on its **very first start** and then keeps
its own saved values, which is why section 5 checks them in the Admin
Panel.

---

## 5. First start of Open WebUI

### 5.1 Create the admin account
1. Open **Safari** on the Mac and go to `http://localhost:3000`.
2. Click **Get started** and create the first account: name, email and
   password. The email is only a login name; Open WebUI doesn't send
   emails.
3. **The first account becomes the admin account.** Use a strong
   password and keep it somewhere safe. Everyone else's account is
   created later (ACCESS.md).

### 5.2 Check the settings
Click your name (bottom left) → **Admin Panel** → **Settings**:
1. **Connections:**
   - **OpenAI API** must be **off**.
   - **Ollama API** must show `http://127.0.0.1:11434`. Click the small
     circular arrow next to it (verify): it should say the connection is
     verified.
   - If you changed anything, click **Save**.
2. **Documents:** **Bypass Embedding and Retrieval** must be **on**. If
   it isn't, switch it on and click **Save**.
3. Start a new chat, choose `qwen2.5-coder:32b` at the top, and type
   "Hallo". An answer means Open WebUI can talk to Ollama. (This is only
   a connection test. Staff will never chat with this model directly;
   see ACCESS.md section 5.)

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
4. Check the first lines start with `"""` and
   `title: Document Intelligence`. Then click **Save**.
5. Back in the Functions list, switch **Document Intelligence** on with
   its toggle on the right.

**When updating the function later:** always select everything in the
code area and delete it before pasting the new version. Otherwise the
old code stays in and Open WebUI may keep running it.

### 6.2 Check the function's settings ("Valves")
In the Functions list, click the gear icon next to Document Intelligence.

| Setting | What to enter |
|---|---|
| `API_BASE` | `http://localhost:8080` (the default) |
| `LANGUAGE` | `de` for German status messages, `en` for English. |
| The three `..._SECONDS` settings | Leave as they are. |

Click **Save**.

### 6.3 Let staff see it
A new function is **only visible to admins** until you change this.
1. Admin Panel → **Settings** → **Models**. Find **Document
   Intelligence** and click the pencil icon (edit).
2. Find **Visibility** (in some versions: **Access**) and set it to
   **Public**, or choose the groups of people who should use it.
3. Under **Capabilities**:
   - untick **File Context** if you see that option (it stops Open WebUI
     from running its own, inaccurate document search)
   - leave **File Upload** and **Vision** ticked (without Vision, Open
     WebUI refuses photos and image scans)
4. Click **Save**.

### 6.4 First test with the sample file
Copy the project's test file to your Desktop:
```bash
cp ~/blueprint/samples/lager_beispiel.csv ~/Desktop/
```
If macOS asks whether **Terminal may access files in your Desktop
folder**, click **Allow**.
1. In Open WebUI, start a **New Chat**.
2. At the top, choose **Document Intelligence** as the model.
3. Click the **+** or paperclip icon in the message box, choose
   **Upload Files**, and pick `lager_beispiel.csv` from the Desktop.
4. Ask each of these questions, one message at a time, and compare:

| Question | Expected answer |
|---|---|
| Wie viele Stück sind insgesamt auf Lager? | 4.885 |
| Wie viele Stück liegen im Lager Hamburg? | 2.710 |
| Welchen Gesamtwert hat der Bestand (Menge mal Einzelpreis)? | 27.582,20 € |
| Wie viele verschiedene Bezeichnungen gibt es? | 4 |

A status line above the answer shows what's happening ("Dokument wird
eingelesen", "Antwort wird berechnet"). The very first question after a
restart can take a minute or two while the model loads. Follow-up
questions in the same chat don't need the file attached again.

If an answer is wrong or doesn't come, go to section 10.

### 6.5 Give colleagues access
Creating accounts for colleagues, the link they use, and keeping that
link from changing are all in [`ACCESS.md`](ACCESS.md).

---

## 7. Check the whole system

```bash
cd ~/blueprint
ollama list
curl http://127.0.0.1:11434
curl http://127.0.0.1:8080/health
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(11434|8080|3000) '
```
Expect: both models, `Ollama is running`, `{"status":"ok"}`, and in the
last command `127.0.0.1:11434` (Ollama) and `127.0.0.1:8080` (pipeline),
which only the Mac itself can reach, plus `*:3000` for Open WebUI, the
only thing office computers are meant to open.

**Test a scanned PDF** (a real scan, not a PDF made from Word or Excel).
Upload it in a new chat with Document Intelligence, ask one question,
and when the answer has arrived run:
```bash
ollama ps
```
It must show **only** `qwen2.5-coder:32b`. The vision model is unloaded
as soon as a document has been read, because both don't fit in memory
together.

**Test a photo:** take a phone photo of a printed table (or use any JPG
or PNG of one), attach it in a new chat with Document Intelligence and
ask for a total. The status line should say "Dokument wird eingelesen:
bild_1.jpg" (or `.png`).

**Test from your own PC:** open `http://192.168.x.x:3000` (the address
the script printed) in a browser on your Windows PC. The login page
should appear.

---

## 8. Keep it running like a server

1. **No sleeping:** System Settings → **Energy** → turn on **Prevent
   automatic sleeping when the display is off** and **Start up
   automatically after a power failure**.
2. **Automatic login after a restart.** The background services start
   when your user account logs in. After a reboot or power cut, someone
   has to log in, or you switch on System Settings → **Users & Groups** →
   **Automatically log in as** → your account. This option is greyed out
   while FileVault disk encryption is on; decide together with whoever is
   responsible for the Mac's security.
3. **A fixed address:** ask whoever runs the office network to give the
   Mac a fixed IP address, so the link for staff never changes (ACCESS.md
   section 2).
4. **Restart once to test:** Apple menu → Restart. After logging in, wait
   five minutes and run the commands in section 7. Everything must have
   come back by itself.

---

## 9. Daily use and maintenance

### Where the logs are
Logs are text files that record what happened. Open one with, for
example:
```bash
open -e ~/blueprint/logs/pipeline.log
```
| File | What's in it |
|---|---|
| `~/blueprint/logs/pipeline.log` | Every document read and every question, with the calculation used and its result. Look here first. |
| `~/blueprint/logs/service.log` | Start-up messages of the pipeline service. |
| `~/blueprint/logs/ollama.log` | Ollama's own messages. |
| `~/blueprint/logs/gc.log` | The hourly cleanup. |
| `~/open-webui-data/openwebui.log` | Open WebUI. |

### Restart a service
```bash
launchctl kickstart -k gui/$(id -u)/com.docintel.pipeline
launchctl kickstart -k gui/$(id -u)/com.docintel.ollama
launchctl kickstart -k gui/$(id -u)/com.docintel.openwebui
```
Run only the line you need.

### Change a setting
Settings are in the file `.env`:
```bash
open -e ~/blueprint/.env
```
Change the value after the `=`, save with `Cmd + S`, close TextEdit,
then restart the pipeline (first line above). Each setting is explained
in the file itself.

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
This gets the newest version of the project, upgrades Ollama and Open
WebUI, and restarts everything. Users, chats and settings in Open WebUI
are kept. If `pipe_function.py` changed, also paste it into Open WebUI
again (section 6.1, clear the code area first).

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
| `command not found: brew` | Close Terminal, open a new window. If it's still missing, repeat section 3. |
| `command not found: uv` | Run `source $HOME/.local/bin/env`, or open a new Terminal window. |
| The setup script prints `STOPPED: ... port 11434` | Something else runs Ollama (for example someone installed the Ollama app). Quit it from the menu bar, then run the script again. |
| The setup script prints `STOPPED: ... port 3000` | Another program uses Open WebUI's port. If it's an old Open WebUI in Docker, quit Docker Desktop. Then run the script again. |
| The setup script prints `STOPPED: some tests failed` | Copy the Terminal output into a message to whoever maintains this project. Don't continue. |
| The setup script stops during a download | Usually the internet connection dropped. Run the script again; it continues where it stopped. |
| `http://localhost:3000` doesn't load | The first start can take 10 minutes. Then look at `~/open-webui-data/openwebui.log` and restart Open WebUI (section 9). |
| Office computers can't open the link, but it works on the Mac | ACCESS.md section 9. |
| Open WebUI shows no models, or an Ollama connection error | Check the Ollama address in Admin Panel → Settings → Connections (5.2) and that `curl http://127.0.0.1:11434` says `Ollama is running`. |
| "Der Dokumenten-Dienst ist gerade nicht erreichbar" | The pipeline isn't running. Run `curl http://127.0.0.1:8080/health`. If that fails, restart it (section 9) and look at `logs/service.log`. |
| "Bitte laden Sie zuerst ein Dokument hoch" | No document in this chat yet, or it was deleted after 24 hours. Attach the file again. |
| Uploading a scanned PDF shows an error, or "The content provided is empty" | Switch on **Bypass Embedding and Retrieval** (5.2), then attach the file again. |
| Attaching a photo shows "the model does not support images" | Tick **Vision** for Document Intelligence (6.3). |
| Staff can't see "Document Intelligence" | Set its visibility to Public (6.3). |
| A chat answers without the status line "Dokument wird eingelesen", or with made-up numbers | The chat isn't using **Document Intelligence**. Pick it at the top of a new chat, and hide the other models from staff (ACCESS.md section 5). |
| An answer takes several minutes | Normal for scanned PDFs and photos: each page is read by the vision model. Excel, CSV and normal PDFs should take under a minute. |
| An answer looks wrong | Open `logs/pipeline.log` and find the question. Under `code used:` is the exact calculation, and under `result:` the number it produced. Note the file and the column that was read wrongly. |
| `ollama ps` still shows `qwen2.5vl:32b` minutes after reading a document | Run `ollama stop qwen2.5vl:32b`, then look for `FAILED to evict` in `logs/pipeline.log`. |
| The Mac feels frozen or very slow | Activity Monitor → Memory. If "Memory Pressure" is red, restart Ollama (section 9). |
| After a reboot something doesn't run although it worked before | System Settings → General → Login Items & Extensions → "Allow in the Background": the entries for `ollama`, `uvicorn`, `python`, `open-webui` and `bash` must be on. Then run `bash scripts/setup_mac.sh` again. |
| Anything else | Run `bash scripts/setup_mac.sh` again from `~/blueprint`. It is safe to repeat. |

---

## 11. Final checklist

- [ ] `git --version` prints a version number (section 2)
- [ ] `brew --version` prints a version number (section 3)
- [ ] The setup script printed `All done` (section 4)
- [ ] `cd ~/blueprint && uv run python --version` prints `Python 3.12.x`
- [ ] `ollama list` shows `qwen2.5-coder:32b` and `qwen2.5vl:32b`
- [ ] `curl http://127.0.0.1:8080/health` prints `{"status":"ok"}`
- [ ] `launchctl list | grep com.docintel` shows `ollama`, `pipeline`,
      `gc` and `openwebui`
- [ ] Admin account created; OpenAI off, Ollama verified, Bypass
      Embedding and Retrieval on (section 5)
- [ ] Document Intelligence is on, Public, with Vision ticked (section 6)
- [ ] The sample file gives all four expected answers (6.4)
- [ ] After a scanned PDF, `ollama ps` shows only `qwen2.5-coder:32b` (7)
- [ ] A photo of a table gets an answer (7)
- [ ] Open WebUI opens from your Windows PC (7)
- [ ] Energy and login settings done, and everything came back after one
      test restart (8)
