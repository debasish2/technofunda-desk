# Moving TechnoFunda Desk to an always-on server (Mumbai)

After this, the PC can be off: the server runs the website and every scheduled job, and you open it from any phone or computer.
Allow about 90 minutes the first time. Nothing here changes your PC until the very last step.

> **Honest note.** These files were written and checked on Windows (the shell scripts pass a syntax check; the data packing and the
> source-reachability test were run for real). They have **not** been run on a Linux server, because there was no server to run them on.
> If any step prints an error, send me the text: it is usually a one-line fix.

## What is in this folder

| File | Where it runs | What it does |
|---|---|---|
| `check_sources.py` | the new server | Step 1: proves NSE, BSE and Yahoo answer from that address |
| `pack.py` | your PC | Step 2: makes `app.tar.gz` (code) and `data.tar.gz` (databases, logins, everyone's settings) |
| `setup_server.sh` | the new server | Step 4: installs everything, starts the app, schedules the jobs |
| `systemd/` | the server | the app as a service, and timers for the nightly, top-up, weekly, hourly and backup jobs (IST) |
| `backup.sh` | the server | daily copy of logins, settings, saved screens (14 days kept) |
| `update_server.ps1`, `apply_update.sh` | PC / server | later: send new code with one command |

## Step 0 – Create the server

Choose **one**. Region **Mumbai** (or Bangalore) matters: NSE and BSE answer Indian addresses best.

* **AWS Lightsail** (region *Mumbai, ap-south-1*): Create instance → Linux/Unix → *OS only* → **Ubuntu 24.04 LTS** → plan with **2 GB RAM**
  → Create. Then *Networking* → *Create static IP* and attach it (so the address never changes). In the instance's *Networking* tab add the
  firewall rules **HTTPS 443** and **HTTP 80** if you will use a domain. Login name: `ubuntu`.
* **DigitalOcean** (*Bangalore, BLR1*): Create Droplet → **Ubuntu 24.04** → Basic, **2 GB RAM** → add your SSH key. Login name: `root`.
* **Oracle Cloud Always Free** (Mumbai) costs nothing but signing up and getting capacity is hit and miss.

Do not pick a 512 MB or 1 GB plan: the nightly job loads two years of prices for about 2,300 stocks. Check the provider's current price
(typically a four-figure rupee amount a month for 2 GB). Download the SSH key or add yours as the provider explains, and note the **server IP**
and **login name**. In the commands below, replace `USER@IP` with those, for example `ubuntu@13.233.10.20`.

## Step 1 – Is the server allowed to reach the data?  (5 minutes, do this first)

From your PC, in the project folder:

```bash
scp deploy\check_sources.py USER@IP:~
ssh USER@IP python3 check_sources.py
```

All lines must say **PASS**. If NSE or BSE says FAIL, delete the server and try the other provider or city. Do not go further.
(On your own PC the same test passes, so a FAIL on the server means that provider's address is being refused.)

## Step 2 – Pack the app and the data  (on your PC)

Commit your latest changes first (the code that goes is what is committed). Then:

```bash
.venv\Scripts\python deploy\pack.py
```

This writes `deploy\out\app.tar.gz` (small) and `deploy\out\data.tar.gz` (about 90 MB). The data includes **your login and
everyone's saved settings**, so you sign in on the server with the same user name and password.

## Step 3 – Copy to the server

```bash
scp deploy\out\app.tar.gz deploy\out\data.tar.gz deploy\setup_server.sh USER@IP:~
```

## Step 4 – Install

Log in (`ssh USER@IP`) and run **one** of these. Tell it how you will reach the app:

* **A. Public address with https** (needs a domain you own, say `desk.yourname.in`; create a DNS *A record* pointing it at the server IP first):

  ```bash
  sudo bash setup_server.sh --domain desk.yourname.in
  ```

* **B. Private, through Tailscale** (no domain, nothing open to the internet; people you invite use Tailscale):

  ```bash
  sudo bash setup_server.sh --tailscale
  sudo tailscale up        # open the link it prints, sign in, then use  http://<server's Tailscale name>:8000
  ```

* **C. Only through an ssh tunnel** (simplest, for a first test):

  ```bash
  sudo bash setup_server.sh
  ```
  then on your PC `ssh -L 8000:127.0.0.1:8000 USER@IP` and open http://localhost:8000

It takes about 5-10 minutes (the Python packages are the slow part) and ends with `the app is running`.

## Step 5 – Check it

1. Open the address, sign in with your usual login. Your charts, columns and saved screens should be there.
2. On the server: `systemctl list-timers 'technofunda*'` lists when each job runs next.
3. Run tonight's job by hand once to prove it works end to end, and watch it:

   ```bash
   sudo systemctl start technofunda-job@nightly
   journalctl -u technofunda-job@nightly -f        # Ctrl+C to stop watching; the job keeps going
   ```

   It should finish in about 10 minutes with `finished: exit code 0`. The newest prices then show on the site.
4. The **fundamentals refresh** (Saturday 09:00) is slower: about an hour.

## Step 6 – Retire the PC's jobs  (only after Step 5 works)

On the PC, so the same jobs do not run twice:

```bash
powershell -ExecutionPolicy Bypass -File scripts\register_nightly.ps1 -Remove
powershell -ExecutionPolicy Bypass -File scripts\register_intraday.ps1 -Remove
```

Keep the "TechnoFunda Backup" task if you still change the code on the PC: it only copies code to GitHub. From now on the **server's**
data is the real one; the copy on the PC goes stale (your settings live on the server under your name).

## Everyday things

| I want to… | Do this |
|---|---|
| Send new code to the server | commit, then `powershell -ExecutionPolicy Bypass -File deploy\update_server.ps1 -Server USER@IP` |
| See why something failed | `journalctl -u technofunda -n 80` (the site) or `journalctl -u technofunda-job@nightly -n 80` (a job) |
| Restart the site | `sudo systemctl restart technofunda` |
| Add a person | sign in, **Profile** → People (or `sudo -u desk /opt/technofunda/.venv/bin/python -m backend.auth add NAME` from `/opt/technofunda`) |
| Forgot my password | on the server, in `/opt/technofunda`: `sudo -u desk .venv/bin/python -m backend.auth passwd NAME` |
| Keep a copy off the server | `scp USER@IP:/home/desk/backups/technofunda-$(date +%F).tar.gz .` (daily backups hold logins, settings, saved screens); also switch on the provider's weekly snapshot |

## Safety

* The login is mandatory for every page and data call. In option A the connection is encrypted by https (Caddy gets and renews the certificate itself).
  Never expose the app over plain `http://` on a public address.
* The server firewall (ufw) allows only ssh, plus 80/443 (option A) or the Tailscale interface (option B). `fail2ban` blocks repeated ssh guessing,
  and security updates install themselves.
* Prices come from Yahoo Finance and NSE/BSE for personal use. Before giving access to many people, check their terms.

## If something goes wrong

* **`check_sources.py` fails** – wrong provider or city, see Step 1.
* **The site does not open** – `systemctl status technofunda`, then `journalctl -u technofunda -n 50`.
* **Prices are a day old** – `journalctl -u technofunda-job@nightly -n 80`; NSE and Yahoo occasionally fail for a night, the next run repairs it.
* **Option A: certificate error** – the domain's DNS A record has to point at the server IP before `setup_server.sh` runs; fix DNS, then
  `sudo systemctl restart caddy`.
* **Out of memory** – check `free -m`; resize to the next plan size.
