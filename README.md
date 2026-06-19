# kl-erp — One-command ERPNext dev setup

Get a brand-new Ubuntu machine running the full Kritilabs ERPNext stack in a
single command — **native bench** (best for customization), with MariaDB in
Docker and a web control panel so you never have to touch the terminal again.

**Stack installed (pinned to production versions):**

| App | Version |
|-----|---------|
| Frappe Framework | v15.56.0 |
| ERPNext | v15.52.0 |
| Frappe HR (hrms) | v15.39.1 |
| India Compliance | v15.16.0 |
| Kriti App (kriti_app) | `dev` branch |

---

## Prerequisites

1. **Ubuntu 22.04 or 24.04** (fresh or existing), with `sudo`.
2. **Bitbucket SSH access** to the private Kriti repo. Verify with:
   ```bash
   ssh -T git@bitbucket.org      # should say "authenticated via ssh key"
   ```
   If not set up, add your SSH key to Bitbucket first.
3. Internet access (the script downloads ERPNext, Node, Docker, etc.).

> You do **not** need to install Python, Node, MariaDB, Docker, or bench
> yourself — the script handles all of it.

---

## Onboard (the whole thing)

```bash
git clone <THIS_REPO_URL> kl-erp-setup
cd kl-erp-setup
./bootstrap.sh
```

The script is **idempotent** — if anything fails (e.g. a flaky download), just
run `./bootstrap.sh` again; it skips the steps already done.

When it finishes:

- **Control panel** → http://localhost:9009
- **ERPNext** → http://erp.localhost:8000 — login `Administrator` / `admin`

Open the panel, click **▶ Start Bench**, then **🌐 Open ERPNext**. Done.

> **First run only:** if the Docker step prints a message about the `docker`
> group, log out and back in (or run `newgrp docker`) and re-run `./bootstrap.sh`.

---

## The Control Panel

A tiny dashboard (Python stdlib only, no dependencies) that replaces the
terminal for day-to-day work. It **auto-starts on boot** as a systemd user
service.

| Section | What it does |
|---------|--------------|
| **Status** | Live cards for MariaDB, Bench, Web (auto-refresh) |
| **Services** | Start / Stop / Restart Bench · Start / Stop the DB container |
| **Maintenance** | Migrate · Clear Cache · Build Assets · Backup |
| **Apps** | Live version + branch table |
| **Log** | Live tail of bench output |

Manage the panel service itself:

```bash
systemctl --user restart erpnext-panel
systemctl --user status  erpnext-panel
```

---

## Why these choices (the gotchas this script solves for you)

- **MariaDB in Docker on port `3307`** — so it never clashes with a local
  MySQL/MariaDB already on `3306`. Your existing DB is untouched.
- **Node installed system-wide** (not nvm) — nvm isn't on `PATH` for
  systemd/non-login shells, which makes `bench start` crash with
  `node: not found`. System-wide Node fixes that.
- **`setuptools<81`** is installed in the bench venv — Python 3.12 dropped
  `pkg_resources`, which Frappe 15.56 still imports.
- **Patched wkhtmltopdf 0.12.6** (with Qt) — the plain `apt` version produces
  broken PDFs/print formats.
- **Exact version pinning** — matches production so you don't chase
  version-drift bugs.

---

## Configuration

Everything tunable lives in [`config.env`](config.env) — versions, ports, site
name, passwords. Edit it **before** running `bootstrap.sh` if you need
different values.

> If you change `SITE`, `DB_PORT`, or `PANEL_PORT`, also update the matching
> constants at the top of [`manage/panel.py`](manage/panel.py).

---

## Useful commands

```bash
cd ~/frappe-bench

bench start                              # run manually (panel does this for you)
bench --site erp.localhost migrate
bench --site erp.localhost console
bench --site erp.localhost backup
bench set-config -g developer_mode 1     # enable when customizing
```
