# Setup guide

Every service used here is free. Expect it to take about 1–2 hours the first time. Do the steps in order.
Wherever you see `NAME`, use the DuckDNS name you pick in step 1.

---

## 1. Free cloud server (Oracle Cloud Always Free)

1. Sign up at **cloud.oracle.com** (a card is needed for verification only; Always Free resources cost $0).
2. Create a VM instance with the **Ubuntu 24.04** image and an **Always Free eligible** shape. Download the SSH key.
3. In the VM's subnet **Security List**, add ingress rules for TCP **80** and **443** from `0.0.0.0/0`.
4. SSH in (`ssh -i key.pem ubuntu@PUBLIC_IP`) and open those ports in the OS firewall too
   (Oracle's Ubuntu images block them by default):
   ```bash
   sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
   sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```
5. At **duckdns.org**, sign in, create a subdomain `NAME`, and set it to the VM's public IP.
6. Install Caddy (HTTPS) by following https://caddyserver.com/docs/install#debian-ubuntu-raspbian, then:
   ```bash
   sudo cp ~/iphone-agent/server/deploy/Caddyfile /etc/caddy/Caddyfile   # after step 3 below
   sudo sed -i 's/YOURNAME/NAME/' /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```

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
   Sign in with your Gmail account and approve. This creates `token.json`, which goes to the server in step 4.

## 3. GitHub (stores the code the agent generates)

1. Create a **private** repo, e.g. `agent-generated-code`, and tick *Add a README* (the repo must not be empty).
2. Go to *Settings → Developer settings → Fine-grained tokens → Generate*. Under *Repository access*, choose
   **only that repo**. Under *Permissions*, set **Contents: Read and write**. Copy the token.

## 4. Deploy the server

On the VM:
```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <your copy of this repo> ~/iphone-agent      # or scp the folder up
cd ~/iphone-agent/server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && python3 scripts/make_keys.py   # paste both keys into .env
nano .env        # fill in MY_EMAIL, GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_REPO, TIMEZONE
```
From the laptop, copy the token up: `scp -i key.pem server\token.json ubuntu@PUBLIC_IP:~/iphone-agent/server/`

Run it as a service:
```bash
sudo cp deploy/iphone-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now iphone-agent
curl https://NAME.duckdns.org/health        # -> {"ok":true,"helper_online":false}
```
Logs: `journalctl -u iphone-agent -f`

## 5. iPhone Shortcuts (the texting part)

**Before you start:** add your Gmail account to Apple Mail (*Settings → Apps → Mail → Mail Accounts*). The email
trigger only sees mail that arrives in the Mail app.

### Automation A: "Agent In" (your texts → agent → reply in iMessage)
Shortcuts → **Automation** → **+** → **Message**
- *Message Contains*: `agent:`
- *Sender*: leave it as anyone, or choose yourself
- Choose **Run Immediately** and turn off *Notify When Run*

Actions:
1. **Get Contents of URL**: `https://NAME.duckdns.org/message`
   - Method **POST**
   - Headers: `X-Agent-Key` = your AGENT_KEY
   - Request Body **JSON**: key `text` (Text) = **Shortcut Input** (tap it and choose the message *Content*)
2. **Send Message**: *Message* = **Contents of URL**, *Recipients* = **your own number**

### Automation B: "Agent Out" (reminders, digests, "location set" → iMessage)
Shortcuts → **Automation** → **+** → **Email**
- *Sender*: your Gmail address
- *Subject Contains*: `[agent]`
- **Run Immediately**

Actions:
1. **Send Message**: *Message* = **Shortcut Input** (the email body), *Recipients* = **your own number**

### Backup: "Ask Agent" shortcut (use this if Automation A doesn't fire)
Create a regular shortcut: **Ask for Input** (Text) → **Get Contents of URL** (same as above, with `text` =
*Provided Input*) → **Send Message** (Contents of URL → your number). Run it from Siri ("Hey Siri, Ask Agent"),
the Action Button, or a home-screen icon.

### Test these first (this is where iOS can surprise you)
1. Text yourself `agent: help`. Does Automation A fire? Some iOS versions don't count a message **you sent to
   yourself** as "received". If it doesn't fire, use the *Ask Agent* shortcut instead. The server works either way.
2. Run `curl -X POST https://NAME.duckdns.org/message -H "X-Agent-Key: KEY" -H "Content-Type: application/json" -d "{\"text\":\"agent: remind me test in 2 minutes\"}"`.
   Two minutes later the reminder email should arrive and Automation B should text it to you. If the text
   includes the whole email header instead of just the body, add a **Get Text from Input** step before
   *Send Message*.

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
   .\.venv\Scripts\python location_helper.py --server https://NAME.duckdns.org --key HELPER_KEY --dry-run
   .\.venv\Scripts\python location_helper.py --server https://NAME.duckdns.org --key HELPER_KEY
   ```
   Press `Ctrl+C` to stop the helper. It clears the fake location on exit.

**Safety:** install pymobiledevice3 only through the pip command above. **Turn Developer Mode off after your
demo.** Restarting the phone also clears any fake location.

## 7. VPN (IP-location demo)

Install **Proton VPN** from the App Store. Its free plan is the one to use: no data cap, and it doesn't sell
browsing data. Text `agent: where am i` with the VPN off, then on, and compare the IP locations.

## 8. Pre-presentation checklist

- [ ] `https://NAME.duckdns.org/health` shows `"helper_online": true` (helper running, phone plugged in)
- [ ] `agent: help` → a reply arrives in iMessage
- [ ] `agent: put me at <a test address>` → confirm with `agent: yes` → a "Location set" text arrives
- [ ] **Find My** (on another device or icloud.com/find) shows the new spot
- [ ] `agent: real location` → "Real location restored", and Find My is back to normal
- [ ] `agent: where am i` with the VPN off and then on shows different IP locations
- [ ] `agent: summarize my email` and `agent: remind me test in 5 minutes` work
- [ ] Afterwards: stop the helper and turn Developer Mode off
