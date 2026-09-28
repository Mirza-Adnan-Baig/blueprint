# Giving people access to Open WebUI

This guide explains how colleagues open Open WebUI from their own
computers (like the `http://192.168.x.x:3000` link you use on your
Windows PC), how to create their accounts, and how to keep that link
working. No Terminal knowledge is needed except in two short places,
which are explained.

---

## How the link you were given works

A link like `http://192.168.1.50:3000` has two parts:

- **`192.168.1.50`** is the Mac Studio's address in the office network.
  Every device in the network has one, handed out by the office router.
- **`3000`** is the "door number" (port) Open WebUI listens on.

The previous admin didn't do anything special for this. Open WebUI is
set up to listen on the whole office network (in Docker through
`-p 3000:8080`, in a native install through `--host 0.0.0.0`, see
SETUP.md section 5), so any computer in the same network can open it by
typing the Mac's address and `:3000` into a browser. That's all.

Three consequences worth knowing:

1. **It only works inside the office network** (office cable network or
   office Wi-Fi). Addresses starting with `192.168.` are private and
   can't be reached from the internet. From home, it only works through
   the company's VPN, if there is one; ask whoever runs the office
   network. **Never** open port 3000 to the internet on the router.
2. **The address can change.** The router may give the Mac a different
   address after a restart, and then everyone's link stops working.
   Section 2 fixes that.
3. **The connection is `http`, not `https`.** Inside a trusted office
   network that's normal. If Open WebUI ever has to be reachable from
   outside the office, that needs an encrypted setup, which is a job for
   whoever runs the network, not a change to the link.

---

## 1. Find the Mac's address

On the Mac, either:
- **System Settings → Network →** click the active connection
  (**Ethernet** for a cable, **Wi-Fi** otherwise) → **Details…** →
  **TCP/IP**. The **IP address** line is what you need.
- or in Terminal:
  ```bash
  ipconfig getifaddr en0
  ```
  If this prints nothing, try `ipconfig getifaddr en1`.

The link for everyone is then `http://<that address>:3000`, for example
`http://192.168.1.50:3000`.

**Test it from your Windows PC:** open the link in Edge or Chrome. The
Open WebUI login page should appear. If it doesn't, see section 9.

---

## 2. Make sure the address never changes

Pick one of these. The first is better.

### Option A: ask whoever runs the office network (recommended)
Ask them to give the Mac Studio a **fixed address** (in the router this
is called a "DHCP reservation" or "static lease"). They'll need the Mac's
**hardware (MAC) address**, which you find on the Mac under **System
Settings → Network → Ethernet → Details… → Hardware → MAC Address**, or
in Terminal:
```bash
ifconfig en0 | awk '/ether/{print $2}'
```
It looks like `a1:b2:c3:d4:e5:f6`. Send them that, plus the address the
Mac has now (section 1), and ask them to reserve that address for it.

### Option B: fix the address on the Mac itself
Only do this if the network admin agrees, because the router must never
give the same address to another device.
1. System Settings → Network → **Ethernet** → **Details…** → **TCP/IP**.
2. Change **Configure IPv4** to **Using DHCP with manual address**.
3. Enter the Mac's current address (section 1) and click **OK**.

---

## 3. Let other computers through the Mac's firewall

1. On the Mac: **System Settings → Network → Firewall**.
2. If the firewall is **off**, there's nothing to do.
3. If it's **on**, click **Options…** and look for Open WebUI's program in
   the list: **Docker** (if Open WebUI runs in Docker) or **open-webui**
   or **python** (native install). It must say **Allow incoming
   connections**. If it isn't listed, restart Open WebUI once; macOS then
   asks, and you click **Allow**.

---

## 4. Create accounts for people

Everyone gets their own account. That way each person sees only their
own chats, and you can remove one person's access without affecting
anyone else.

**The three roles in Open WebUI:**

| Role | Can do |
|---|---|
| **admin** | Everything, including managing users and settings. Give this to as few people as possible (you, and maybe one deputy). |
| **user** | Normal use: chat, upload documents, ask questions. This is the role for staff. |
| **pending** | Nothing yet. Someone who signed up and is waiting for an admin to let them in. |

There are two ways to create accounts.

### Way A: you create each account (recommended for a small team)
1. Open Open WebUI and sign in as admin.
2. Click your name at the bottom left → **Admin Panel** → **Users**.
3. Click the **+** button (**Add User**).
4. Fill in **Name**, **Email** and a starting **Password**, set the
   **Role** to **user**, and save. (The email only serves as the login
   name; Open WebUI doesn't send any emails.)
5. Send the person the link, their email and the starting password
   (section 6 has a message you can copy).
6. Ask them to change the password after the first login: their name at
   the bottom left → **Settings** → **Account** → change the password.

For many people at once, the **Add User** window also offers **Import
CSV**.

### Way B: people sign themselves up, you approve them
1. Admin Panel → **Settings** → **General**.
2. Switch on **New Sign Ups**, and make sure **Default User Role** is set
   to **pending**. Save. (Never set the default role to admin.)
3. Send people the link. On the login page they click **Sign up** and
   enter their name, email and a password.
4. They then see **Account Activation Pending** until you approve them.
5. To approve: Admin Panel → **Users** → click the person (or their role)
   → set the role to **user** → save.
6. When everyone has signed up, switch **New Sign Ups** off again, so
   nobody else in the network can create an account.

---

## 5. Give them the right model, and only that

### Make "Document Intelligence" visible to staff
New models are only visible to admins. Do SETUP.md section 6.3 (set its
**Visibility** to **Public**) if you haven't yet.

To give it to some people only: Admin Panel → **Users** → **Groups** →
create a group (for example "Lager") and add the people. Then in the
model's settings (SETUP.md 6.3), under **Visibility**, grant access to
that group instead of making it public.

### Hide the raw models from staff (important)
Staff should always use **Document Intelligence**. If they pick
`qwen2.5-coder:32b` or `qwen2.5vl:32b` directly, or any other model such
as a test model, they chat with a bare language model that guesses
numbers instead of calculating them, which is exactly the problem this
project solves.

Admin Panel → **Settings** → **Models**. For every model other than
Document Intelligence, open its settings (pencil icon) and keep its
**Visibility** on private (admins only), or switch the model off with
its toggle in the list. Don't delete `qwen2.5-coder:32b` or
`qwen2.5vl:32b` in Ollama itself: the pipeline needs them.

### Make it the model a new chat starts with
- For everyone: in the **Admin Panel → Settings** pages, look for
  **Default Model** (depending on the Open WebUI version it's on the
  **Models** page, sometimes behind a gear or settings button, or on the
  **Interface** page) and choose **Document Intelligence**.
- For one person: in a new chat, choose Document Intelligence at the top,
  then click **Set as default** just below the model name.

---

## 6. What to send a colleague

Replace the parts in brackets and send it by email or chat:

> Hallo [Name],
>
> das Dokumenten-Tool erreichen Sie im Büronetz unter:
> **http://[192.168.x.x]:3000**
>
> Anmeldung: E-Mail **[E-Mail]**, Passwort **[Startpasswort]**. Bitte
> ändern Sie das Passwort nach der ersten Anmeldung (unten links auf
> Ihren Namen → Einstellungen → Konto).
>
> So geht's:
> 1. **Neuer Chat** öffnen und oben **Document Intelligence** wählen.
> 2. Über das **Plus- oder Büroklammer-Symbol** im Eingabefeld die Datei
>    anhängen (Excel, CSV oder PDF).
> 3. Die Frage ganz normal stellen, zum Beispiel „Wie viele Stück sind
>    insgesamt auf Lager?“. Weitere Fragen zum selben Dokument können Sie
>    im selben Chat stellen, ohne die Datei erneut anzuhängen.
>
> Die Antworten werden aus den echten Daten der Datei berechnet. Das
> Tool funktioniert nur im Büronetz. Nach 24 Stunden muss eine Datei für
> neue Fragen erneut angehängt werden.

---

## 7. Privacy: who can see what

Tell staff honestly:
- Everything stays on the Mac Studio. Nothing is sent to any cloud
  service.
- **Admins can read other users' chats** in Open WebUI (this is Open
  WebUI's default). To switch that off, add the setting
  `ENABLE_ADMIN_CHAT_ACCESS=false` to Open WebUI:
  - native install: in the file from SETUP.md section 5, option A, add
    `<key>ENABLE_ADMIN_CHAT_ACCESS</key><string>false</string>` next to
    the other settings, then restart Open WebUI (SETUP.md section 9).
  - Docker: add `-e ENABLE_ADMIN_CHAT_ACCESS=false` to the `docker run`
    command in SETUP.md section 5, option B, and create the container
    again the same way.
- The pipeline deletes its copy of uploaded files after 24 hours. Open
  WebUI keeps its own copy of each upload until the chat is deleted.

---

## 8. Removing access and forgotten passwords

- **Someone leaves:** Admin Panel → **Users** → find the person → delete
  them (bin icon). To block them but keep their chats, set their role to
  **pending** instead.
- **Forgotten password:** Admin Panel → **Users** → click the person
  (**Edit User**) → enter a new password → save, and tell them the new
  one.

---

## 9. If someone can't open the link

| What they see | What to check |
|---|---|
| The page doesn't load at all | Are they in the office network (or connected to the VPN)? Is the Mac on and awake (SETUP.md section 8)? Did the Mac's address change (section 1 and 2)? Is the firewall letting it through (section 3)? |
| It works on the Mac (`http://localhost:3000`) but not from other computers | Firewall (section 3), or Open WebUI listens only on the Mac itself: in a native install the file from SETUP.md section 5 must contain `--host` and `0.0.0.0`. |
| The browser says the site can't provide a secure connection | They typed `https://`. It must be `http://`. |
| "Account Activation Pending" | Approve them (section 4, way B, step 5). |
| They don't see Document Intelligence | Its visibility (section 5). |
| Wrong email or password | Reset the password (section 8). |

**Quick connection test from a Windows PC:** open **PowerShell** (Start
menu, type PowerShell) and run, with the Mac's address:
```powershell
Test-NetConnection 192.168.x.x -Port 3000
```
`TcpTestSucceeded : True` means the network and the Mac are fine and the
problem is in the browser or the account. `False` means the Windows PC
can't reach the Mac at all: check the network, the Mac's address and the
firewall.
