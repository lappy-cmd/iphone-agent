# Setup guide

Every service used here is free. Expect it to take about 1–2 hours the first time. Do the steps in order.
Wherever you see `APP`, use your Render service name from step 4 (your server lives at
`https://APP.onrender.com`). No credit card is needed for the services in steps 1–4.

The server runs 24/7 on Render's free plan. Render puts free services to sleep after 15 minutes without
traffic, so a free pinger keeps it awake (step 4). Render also wipes its disk on every restart, so reminders and
other state live in a free hosted Turso database (step 1).

---

## 1. Database (Turso, free)

1. Sign up at **turso.tech** (signing in with GitHub is fine).
2. Create a database. The name doesn't matter; pick the location closest to you.
3. On the database page, copy its URL (starts with `libsql://`). That's `TURSO_DATABASE_URL`.
4. Create a token for it with **read & write** access and no expiry. That's `TURSO_AUTH_TOKEN`.

The server creates its tables on first start.

## 2. Google: Gemini key + Gmail access

**Gemini:** go to **aistudio.google.com** → *Get API key* → create a key. That's `GEMINI_API_KEY`.
(On the free tier, Google may use your prompts, including email snippets, to improve its products.)

**Gmail API:**
1. Open **console.cloud.google.com** → create a project → *APIs & Services* → *Library* → enable **Gmail API**.
2. Under *OAuth consent screen*, choose User type **External**. Fill in the app name and your email, and add
   yourself as a test user.
3. **Important:** click **Publish app** (set it to *In production*). Apps left in *Testing* get sign-ins that
   **expire every 7 days**. You'll see an "unverified app" warning when you sign in. That's expected for a
   personal app: click *Advanced → Go to app*.
4. Under *Credentials* → *Create credentials* → *OAuth client ID* → **Desktop app** → download the JSON file as
   `credentials.json`.
5. On your **laptop**:
   ```powershell
   cd iphone-agent\server
   python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
   .\.venv\Scripts\python scripts\gmail_auth.py C:\path\to\credentials.json
   ```
   Sign in with your Gmail account and approve. This creates `server	oken.json`. Its contents become `GMAIL_TOKEN_JSON` in step 4.

## 3. GitHub

**A. Repo for the code the agent generates**
1. Create a **private** repo, e.g. `agent-generated-code`, and tick *Add a README* (the repo must not be empty).
2. Go to *Settings → Developer settings → Fine-grained tokens → Generate*. Under *Repository access*, choose
   **only that repo**. Under *Permissions*, set **Contents: Read and write**. Copy the token.

**B. Repo for this project (Render deploys from it)**
1. Create another **private** repo, e.g. `iphone-agent`, and leave it empty (no README).
2. On the laptop, push this folder to it. `.gitignore` keeps `.env`, `token.json` and `credentials.json` out:
   ```powershell
   cd iphone-agent
   git remote add origin https://github.com/YOU/iphone-agent.git
   git push -u origin HEAD:main
   ```

## 4. Deploy the server (Render, free) + keep it awake

1. On the laptop, make your two secret keys and keep the output somewhere safe:
   ```powershell
   cd iphone-agent\server
   .\.venv\Scripts\python scripts\make_keys.py
   ```
2. Sign up at **render.com** with GitHub and let it see your `iphone-agent` repo.
3. Click **New → Blueprint** and pick the `iphone-agent` repo. Render reads `render.yaml` and asks for these values:

   | Variable | Value |
   |---|---|
   | `AGENT_KEY`, `HELPER_KEY` | from step 1 above |
   | `MY_EMAIL` | your Gmail address |
   | `TIMEZONE` | e.g. `America/Los_Angeles` |
   | `GEMINI_API_KEY` | from section 2 |
   | `GITHUB_TOKEN`, `GITHUB_REPO` | from section 3A (`GITHUB_REPO` is `YOU/agent-generated-code`) |
   | `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | from section 1 |
   | `GMAIL_TOKEN_JSON` | open `server	oken.json` in Notepad and paste **the whole contents** |

   Click **Apply**. The first build takes a few minutes.
4. When it's live, the service page shows your URL, `https://APP.onrender.com`. Check it:
   ```powershell
   curl.exe https://APP.onrender.com/health     # -> {"ok":true,"helper_online":false}
   ```
   Logs are on the service's **Logs** tab.
5. **Keep it awake:** sign up at **cron-job.org** (free), then **Create cronjob**:
   - URL `https://APP.onrender.com/health`
   - schedule: every **10 minutes**

   Without this, Render puts the server to sleep after 15 idle minutes. Reminders and the morning digest
   wouldn't go out, and the first text after a sleep could take about a minute to answer. Render's free
   750 hours a month cover one service running all month.

Updating the server later: push to GitHub and Render redeploys automatically. Your data is safe in Turso.

## 5. iPhone Shortcuts (the texting part)

Your **chat with yourself becomes the chat with the AI**: anything you text yourself goes to it, in plain
words. No "agent:" needed. Every reply carries an invisible marker, so the server ignores its own
replies when they show up in the same chat and nothing loops.

**Before you start:** turn on **iCloud Mail** (*Settings → [your name] → iCloud → iCloud Mail*). If you never
used it, iOS asks you to pick an `@icloud.com` address. Then, in Render → *Environment*, set
`NOTIFY_EMAIL` to that address. Reminders and "done" messages go there. iCloud Mail is instant push on
iPhone, while Gmail in Apple Mail is only checked every 15+ minutes.

### Automation A: "Agent In" (your texts → AI → reply in iMessage)
Shortcuts → **Automation** → **+** → **Message**
- *Sender*: **yourself** (pick your own contact card). Leave *Message Contains* empty.
- Choose **Run Immediately** and turn off *Notify When Run*

Actions:
1. **Get Contents of URL**: `https://APP.onrender.com/message`
   - Method **POST**
   - Headers: key `X-Agent-Key` (literally), text = your AGENT_KEY
   - Request Body **JSON**: key `text` (Text) = **Shortcut Input → Content**
2. **Get Text from Input**: input = *Contents of URL*
3. **If**: input = *Text*, condition **has any value**
   - inside the If: **Send Message**: *Message* = **Text**, *Recipients* = **your own number**
   - leave *Otherwise* empty (an empty reply means "this was my own message, send nothing")

### Automation B: "Agent Out" (reminders, digests, "location set" → iMessage)
Shortcuts → **Automation** → **+** → **Email**
- *Account*: iCloud. *Sender*: your Gmail address
- *Subject Contains*: `[agent]`
- **Run Immediately**

Actions:
1. **Get Text from Input**: input = *Shortcut Input*
2. **Send Message**: *Message* = **Text**, *Recipients* = **your own number**

### Backup: "Ask Agent" shortcut (if Automation A ever stops firing)
A regular shortcut: **Ask for Input** (Text) → **Get Contents of URL** (same as above, with `text` =
*Provided Input*) → **Get Text from Input** → **Send Message** (Text → your number). Run it from Siri
("Hey Siri, Ask Agent"), the Action Button, or a home-screen icon.

### Test it
1. Text yourself `hey, what can you do?`. A reply should arrive in the same chat, and **only one**.
   You'll see each message twice (sent + received); that's how iMessage shows texts to yourself.
2. Text `remind me to test this in 2 minutes`. Two minutes later the reminder should arrive as a text.

## 6. Laptop location helper (Windows)

1. Install **iTunes** from apple.com (it provides the iPhone USB driver).
2. Install the helper's dependencies:
   ```powershell
   cd iphone-agent\helper
   python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
   ```
3. Plug the iPhone in with USB, unlock it, and tap **Trust**.
4. Developer Mode: if *Settings → Privacy & Security → Developer Mode* isn't visible, run
   `.\.venv\Scripts\python -m pymobiledevice3 amfi reveal-developer-mode`. Then turn it on; the phone restarts.
5. In a terminal opened with **Run as administrator** (keep it open), start the tunnel:
   ```powershell
   .\.venv\Scripts\python -m pymobiledevice3 remote tunneld
   ```
6. In a normal terminal, do a dry run first, then the real thing:
   ```powershell
   .\.venv\Scripts\python location_helper.py --server https://APP.onrender.com --key HELPER_KEY --dry-run
   .\.venv\Scripts\python location_helper.py --server https://APP.onrender.com --key HELPER_KEY
   ```
   Press `Ctrl+C` to stop the helper. It clears the fake location on exit.

**Safety:** install pymobiledevice3 only through the pip command above. **Turn Developer Mode off after your
demo.** Restarting the phone also clears any fake location.

## 7. VPN (IP-location demo)

Install **Proton VPN** from the App Store. Its free plan is the one to use: no data cap, and it doesn't sell
browsing data. Text `where am i?` with the VPN off, then on, and compare the IP locations.

## 8. Pre-presentation checklist

- [ ] `https://APP.onrender.com/health` shows `"helper_online": true` (helper running, phone plugged in)
- [ ] Text yourself anything → exactly one reply arrives
- [ ] `put me at <a test address>` → it tells you what it found → `yes` → it confirms the move
- [ ] **Find My** (on another device or icloud.com/find) shows the new spot
- [ ] `give me my real location back` → it confirms, and Find My is back to normal
- [ ] `where am i?` with the VPN off and then on shows different IP locations
- [ ] `what's in my email?` and `remind me to test in 5 minutes` work
- [ ] Afterwards: stop the helper and turn Developer Mode off

## 9. Experimental: change location from anywhere (Tailscale VPN)

This tries to let the laptop at home reach the phone while you're out, so the agent can move the location
without a cable. **It may not work.** The laptop finds the phone with Bonjour (local-network discovery),
which normally doesn't cross a VPN, and Apple may only allow these developer connections on the local
network. Steps 1–3 work on their own (Wi-Fi at home), so do them first.

1. **Let Windows discover devices on your home Wi-Fi.** In an *Administrator* PowerShell:
   ```powershell
   Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private
   ```
2. **Allow the phone over Wi-Fi.** With the phone plugged in and unlocked:
   ```powershell
   cd iphone-agent\helper
   .\.venv\Scripts\python -m pymobiledevice3 lockdown wifi-connections on
   ```
3. **Test at home over Wi-Fi.** Phone and laptop on the same Wi-Fi, phone unplugged and awake. Start the
   tunnel (Administrator window), then check that the phone shows up under `"wifi"`:
   ```powershell
   .\.venv\Scripts\python -m pymobiledevice3 remote tunneld
   # in a normal window:
   .\.venv\Scripts\python -m pymobiledevice3 remote browse --timeout 8
   ```
   If it shows up, start the helper through the tunnel by setting your phone's UDID first (find it with
   `pymobiledevice3 usbmux list` while plugged in):
   ```powershell
   $env:PYMOBILEDEVICE3_TUNNEL = "YOUR-PHONE-UDID"
   .\.venv\Scripts\python location_helper.py --server https://APP.onrender.com --key HELPER_KEY
   ```
   Text a location change. If it works, you're cable-free at home.
4. **Install Tailscale** (free) on the laptop (tailscale.com/download) and the iPhone (App Store). Sign in
   to the **same account** on both, and turn on the VPN on the phone.
5. **Keep the laptop awake 24/7** when it's plugged into power:
   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   ```
   Leave it on, with the tunnel and helper windows open.
6. **Test away from home.** Switch the phone to cellular (Wi-Fi off), keep Tailscale on, wait a minute,
   then run `remote browse` on the laptop again (or just text a location change).
   - **The phone shows up under "wifi" and the change works:** it works from anywhere, as long as the
     laptop stays on and both stay connected to Tailscale.
   - **It doesn't show up:** discovery doesn't cross the VPN. Connecting directly to the phone's
     Tailscale address would need extra code, and Apple may still refuse the connection.
