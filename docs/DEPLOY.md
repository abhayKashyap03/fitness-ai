# Deploying the coach to a host

The runbook for [ADR-0019](adr/0019-hosting-the-owners-instance.md): the hosted
instance is the owner's own, and **the host becomes the only instance**. The
laptop stops holding live data and becomes the backup archive.

Everything here except step 1 is mechanical. Step 1 needs a credit card.

**Order is not negotiable.** WHOOP is authorised *on the host* against a public
`https://<domain>/callback`, so the domain and its certificate are prerequisites
of ingest, not follow-on polish. Doing 6 before 4 means authorising against a
callback that does not resolve.

---

## 0. Before you start

On the laptop, take a backup and prove it comes back:

```bash
coach db backup
coach db rehearse-restore "$(ls -t data/backups/*.db | head -1)"
```

You want to read `restorable, canonical data identical to live`. Everything
below assumes you can get back to today if it goes wrong.

Note the fingerprint — step 7 compares against it.

```bash
coach db verify
```

## 1. The VPS *(you)*

Any small provider. 1 GB RAM is plenty: SQLite, one Python process, one user.
Debian or Ubuntu LTS.

Create an unprivileged user (`coach` below), disable password SSH, and put your
key on it. Do not run any of this as root beyond the `sudo` steps shown.

## 2. The domain *(you)*

Point an A record at the VPS. `coach.example.com` throughout this document.

DNS must have propagated before step 4 — Caddy proves control of the name by
answering a challenge on it, and it cannot do that against a record that does
not resolve yet.

## 3. Install the app

```bash
sudo apt update && sudo apt install -y python3 python3-venv git caddy
sudo adduser --disabled-password --gecos "" coach
sudo -u coach -i

git clone <your repo> ~/fitness-ai
cd ~/fitness-ai
python3 -m venv .venv
.venv/bin/pip install -e '.[web]'
mkdir -p data
```

Create `~/fitness-ai/.env` from `.env.example`. It needs the WHOOP client
credentials, the MyFitnessPal cookie, an LLM key, and:

```
COACH_DB_PATH=/home/coach/fitness-ai/data/coach.db
COACH_SECRET_KEY=<output of `coach user genkey`>
COACH_WHOOP_REDIRECT_URI=https://coach.example.com/callback
```

`COACH_SECRET_KEY` decrypts per-user source credentials. ADR-0018 requires it to
live in the host environment and **never** in the database — that is what makes
a stolen dump yield ciphertext alone. Generate it with `coach user genkey`,
which prints to stdout and writes nowhere.

```bash
chmod 600 ~/fitness-ai/.env
```

## 4. TLS and the proxy

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/coach.example.com/<your domain>/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy obtains and renews the certificate itself. There is no certbot cron job to
forget — deliberate, on a machine meant to survive months of neglect (risk #9).

Confirm before continuing:

```bash
curl -I https://coach.example.com/healthz
```

## 5. Run the app

```bash
sudo cp deploy/coach-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coach-web
journalctl -u coach-web -n 30
```

The unit binds **127.0.0.1** on purpose. Caddy is the only thing that should
reach the app; a public bind would silently bypass TLS.

### Claim the account

The app **refuses to start** on a non-loopback bind with no claimed account, and
the loopback bind above means that refusal is not what is protecting you here —
so claim it now rather than relying on the guard:

```bash
cd ~/fitness-ai
.venv/bin/coach user set-email --email you@example.com
.venv/bin/coach user set-password
```

`set-password` prompts without echo and has no flag, so the password never
enters shell history or the process list.

Visit `https://coach.example.com`, sign in, and accept the safety notice
([ADR-0020](adr/0020-medical-disclaimer.md)) — the app will not show you a
number until you have.

## 6. Authorise WHOOP *on the host*

Register `https://coach.example.com/callback` as a redirect URI in the WHOOP
developer console first.

```bash
cd ~/fitness-ai && .venv/bin/coach auth whoop --headless
```

It prints a URL. Open it on any machine with a browser, authorise, and paste
back the URL the browser lands on. **That page will probably fail to load —
that is expected**; the part that matters is in the address bar.

The laptop's existing token is deliberately *not* copied up. The host
authorising itself keeps a rollback path alive: if this goes badly, the laptop
still has a working token and is still a working instance.

## 7. Migrate the data

One shot. On the laptop:

```bash
coach db backup --to /tmp/cutover.db
coach db verify                      # note the canonical fingerprint
scp /tmp/cutover.db coach@coach.example.com:~/fitness-ai/data/
```

On the host:

```bash
cd ~/fitness-ai
.venv/bin/coach db rehearse-restore data/cutover.db
sudo systemctl stop coach-web
.venv/bin/coach db restore data/cutover.db --yes
.venv/bin/coach db init          # apply any migrations the laptop hadn't
.venv/bin/coach db verify        # fingerprint MUST match the laptop's
sudo systemctl start coach-web
```

**The fingerprints must match.** If they do not, stop and work out why before
going further — that is the whole reason to compare them.

Then, on the laptop, rename rather than delete:

```bash
mv data/coach.db data/coach.db.pre-cutover
```

Renamed, not deleted, on purpose (§8.5). The laptop is now the archive; if the
host turns out to be a mistake, this file is the way back.

## 8. Schedule sync and backups

```bash
sudo cp deploy/coach-cron /etc/cron.d/coach
sudo chmod 644 /etc/cron.d/coach
sudo sed -i 's/you@example.com/<your real address>/' /etc/cron.d/coach
```

Set `MAILTO` to an address you actually read. `sync --quiet` prints nothing when
it worked, so **mail arriving means something needs you**. With `MAILTO` unset
every failure is silent, which defeats the point.

## 9. Rehearse the restore. Actually do it.

On the laptop, pull the first backup down and verify the copy that is being
kept:

```bash
deploy/pull-backup.sh coach@coach.example.com ~/archive/fitness-ai
```

The script verifies the downloaded file, not the host's copy — the archive copy
is the one that has to survive, and the transfer is a thing that can corrupt it.

Add it to the laptop's crontab, after the host's 02:30 backup:

```
0 8 * * * /path/to/repo/deploy/pull-backup.sh coach@coach.example.com ~/archive/fitness-ai
```

**Then do a full restore drill at least once**, into a scratch path, and look at
the result:

```bash
COACH_DB_PATH=/tmp/drill.db coach db restore ~/archive/fitness-ai/<newest>.db --yes
COACH_DB_PATH=/tmp/drill.db coach db verify
COACH_DB_PATH=/tmp/drill.db coach status
```

An untested backup is a belief, not a backup. The weekly `rehearse-restore` in
cron keeps that belief honest, but it has never shown you the data — do that
once, by hand, now, while nothing is wrong.

---

## Operating it

| | |
|---|---|
| Logs | `journalctl -u coach-web -f` |
| Restart | `sudo systemctl restart coach-web` |
| Health | `curl -sf https://coach.example.com/healthz` |
| Sync now | `cd ~/fitness-ai && .venv/bin/coach sync` |
| What's wrong | `cd ~/fitness-ai && .venv/bin/coach doctor` |
| Spend | `cd ~/fitness-ai && .venv/bin/coach cost` |
| CLI | over SSH — `COACH_DB_PATH` is a filesystem path handed to `sqlite3.connect`, so there is no remote-database mode and the CLI must run on the host |

### When the MyFitnessPal cookie expires

It will; it is a session cookie. `coach sync` reports `mfp: auth needed` and
exits non-zero, so cron mails you. Paste a fresh cookie into `~/fitness-ai/.env`
and `sudo systemctl restart coach-web`.

### Rolling back to the laptop

The reason step 7 renames rather than deletes:

```bash
mv data/coach.db.pre-cutover data/coach.db   # laptop is live again
```

Then pull anything the host gathered in the meantime from the archive. The
laptop's WHOOP token was never invalidated, because the host authorised itself
in step 6 instead of taking it.

---

## Not covered here

- **Firewall.** Allow 22, 80, 443; deny the rest. Provider-specific.
- **Unattended upgrades.** Worth enabling on a machine you intend to ignore.
- **A second human logging in.** Still gated on the medical-disclaimer
  obligation being genuinely satisfied for *someone else's* body, not just
  displayed (ADR-0018, ADR-0020). ADR-0019 defers the beta; this runbook hosts
  one person.
