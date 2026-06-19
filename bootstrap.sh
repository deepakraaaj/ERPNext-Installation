#!/usr/bin/env bash
# ============================================================================
#  kl-erp onboarding bootstrap
#  One command to get a brand-new Ubuntu machine running the full ERPNext
#  stack (ERPNext + Frappe HR + India Compliance + Kriti app) via native bench,
#  with a MariaDB Docker container and a web control panel.
#
#  Usage:   ./bootstrap.sh
#  Re-runnable: yes — every step is idempotent, safe to run again.
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.env"

# ---- pretty logging --------------------------------------------------------
c_blue=$'\e[1;34m'; c_green=$'\e[1;32m'; c_red=$'\e[1;31m'; c_yellow=$'\e[1;33m'; c_off=$'\e[0m'
step(){ echo; echo "${c_blue}==> $*${c_off}"; }
ok(){   echo "${c_green}  ✓ $*${c_off}"; }
warn(){ echo "${c_yellow}  ! $*${c_off}"; }
die(){  echo "${c_red}  ✗ $*${c_off}"; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] && die "Run as your normal user (NOT root). It will sudo when needed."
have sudo || die "sudo is required."

# ---------------------------------------------------------------------------
step "1/10  System packages (apt)"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  git curl wget software-properties-common \
  python3 python3-dev python3-pip python3-venv pipx \
  redis-server \
  mariadb-client \
  libmariadb-dev pkg-config \
  build-essential \
  xvfb libfontconfig1 libxrender1 fontconfig \
  >/dev/null
ok "base packages installed"

# ---------------------------------------------------------------------------
step "2/10  Node.js ${NODE_MAJOR}.x (system-wide) + Yarn"
# Installed system-wide on purpose: nvm is NOT on PATH for systemd/non-login
# shells, which makes 'bench start' (watch process) crash with 'node: not found'.
if ! have node || [ "$(node -v | cut -dv -f2 | cut -d. -f1)" -lt 18 ]; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | sudo -E bash - >/dev/null
  sudo apt-get install -y -qq nodejs >/dev/null
fi
have yarn || sudo npm install -g yarn >/dev/null 2>&1
ok "node $(node -v), yarn $(yarn --version)"

# ---------------------------------------------------------------------------
step "3/10  wkhtmltopdf (patched 0.12.6 with Qt — needed for PDFs/print)"
if wkhtmltopdf --version 2>/dev/null | grep -qi "with patched qt"; then
  ok "already installed: $(wkhtmltopdf --version)"
else
  deb="/tmp/wkhtmltox.deb"
  url="https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.jammy_amd64.deb"
  for i in 1 2 3 4 5; do
    curl -sSL -o "$deb" "$url"
    file "$deb" | grep -qi "Debian binary package" && break
    warn "download attempt $i failed (GitHub 502?), retrying…"; sleep 5
  done
  file "$deb" | grep -qi "Debian binary package" || die "could not download wkhtmltopdf"
  sudo apt-get install -y -qq "$deb" >/dev/null
  ok "installed: $(wkhtmltopdf --version)"
fi

# ---------------------------------------------------------------------------
step "4/10  Docker (for the MariaDB container)"
if ! have docker; then
  curl -fsSL https://get.docker.com | sudo sh >/dev/null
fi
if ! docker ps >/dev/null 2>&1; then
  sudo usermod -aG docker "$USER" || true
  warn "Added you to the 'docker' group. If the next docker step fails, log out/in"
  warn "(or run: newgrp docker) and re-run this script."
  sudo systemctl enable --now docker || true
fi
ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"

# ---------------------------------------------------------------------------
step "5/10  MariaDB container '${DB_CONTAINER}' on port ${DB_PORT}"
if docker inspect "$DB_CONTAINER" >/dev/null 2>&1; then
  docker start "$DB_CONTAINER" >/dev/null 2>&1 || true
  ok "container already exists (started)"
else
  docker run -d \
    --name "$DB_CONTAINER" \
    --restart unless-stopped \
    -p "${DB_PORT}:3306" \
    -e MARIADB_ROOT_PASSWORD="$DB_ROOT_PASSWORD" \
    -v "${DB_CONTAINER}-data:/var/lib/mysql" \
    "$MARIADB_IMAGE" \
    --character-set-server=utf8mb4 \
    --collation-server=utf8mb4_unicode_ci \
    --character-set-client-handshake=FALSE >/dev/null
  ok "container created"
fi
echo -n "  waiting for MariaDB"
for i in $(seq 1 30); do
  if docker exec "$DB_CONTAINER" mariadb -uroot -p"$DB_ROOT_PASSWORD" -e "SELECT 1" >/dev/null 2>&1; then
    echo " ready"; break; fi
  echo -n "."; sleep 2
done

# ---------------------------------------------------------------------------
step "6/10  Install bench CLI"
if ! have bench; then
  pipx install frappe-bench >/dev/null 2>&1 || pip3 install --user --quiet frappe-bench
  pipx ensurepath >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"
fi
have bench || die "bench not on PATH — open a new shell and re-run."
ok "bench $(bench --version 2>/dev/null | head -1)"

# ---------------------------------------------------------------------------
step "7/10  Initialise bench at ${BENCH_DIR}"
if [ -d "$BENCH_DIR/apps/frappe" ]; then
  ok "bench already initialised"
else
  bench init --frappe-branch version-15 "$BENCH_DIR"
  ok "bench initialised"
fi
cd "$BENCH_DIR"
bench set-config -g db_host 127.0.0.1 >/dev/null
bench set-config -g db_port "$DB_PORT" >/dev/null

# helper: ensure an app is present at a pinned tag/branch -------------------
get_app_pinned(){ # name  url  ref
  local name="$1" url="$2" ref="$3"
  if [ ! -d "apps/$name" ]; then
    bench get-app --branch version-15 "$url" || bench get-app "$url"
  fi
  local remote; remote="$(git -C "apps/$name" remote | head -1)"
  git -C "apps/$name" fetch --depth 1 "$remote" tag "$ref" 2>/dev/null || \
    git -C "apps/$name" fetch "$remote" "$ref" 2>/dev/null || true
  git -C "apps/$name" checkout -- . 2>/dev/null || true
  git -C "apps/$name" clean -fd 2>/dev/null || true
  git -C "apps/$name" checkout -q "$ref"
}

step "8/10  Fetch & pin apps"
get_app_pinned frappe           "https://github.com/frappe/frappe"               "$FRAPPE_VERSION"
get_app_pinned erpnext          "https://github.com/frappe/erpnext"              "$ERPNEXT_VERSION"
get_app_pinned hrms             "https://github.com/frappe/hrms"                 "$HRMS_VERSION"
get_app_pinned india_compliance "https://github.com/resilient-tech/india-compliance" "$INDIA_COMPLIANCE_VERSION"
# Kriti app (private, branch not tag)
if [ ! -d "apps/$KRITI_APP_NAME" ]; then
  bench get-app --branch "$KRITI_BRANCH" "$KRITI_REPO" || \
    die "could not fetch $KRITI_REPO — check your Bitbucket SSH access (ssh -T git@bitbucket.org)"
else
  git -C "apps/$KRITI_APP_NAME" checkout -q "$KRITI_BRANCH" 2>/dev/null || true
fi
ok "apps pinned: frappe $FRAPPE_VERSION, erpnext $ERPNEXT_VERSION, hrms $HRMS_VERSION, india_compliance $INDIA_COMPLIANCE_VERSION, $KRITI_APP_NAME @ $KRITI_BRANCH"

# pkg_resources fix (Python 3.12) + dependency resync
./env/bin/pip install --quiet "setuptools<81"
bench setup requirements

# ---------------------------------------------------------------------------
step "9/10  Create site '${SITE}' + install apps"
if [ -d "sites/$SITE" ]; then
  ok "site already exists — skipping creation"
else
  # bench's own redis must be up for install patches (hrms). Start them quietly.
  ( bench start >/tmp/bench-bootstrap.log 2>&1 & echo $! >/tmp/bench-bootstrap.pid )
  echo -n "  waiting for redis"; for i in $(seq 1 15); do
    (echo > /dev/tcp/127.0.0.1/13000) 2>/dev/null && { echo " up"; break; }; echo -n "."; sleep 1; done

  bench new-site "$SITE" \
    --force \
    --db-root-username root \
    --db-root-password "$DB_ROOT_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD" \
    --db-host 127.0.0.1 --db-port "$DB_PORT" \
    --mariadb-user-host-login-scope='%' \
    --install-app erpnext \
    --install-app hrms \
    --install-app india_compliance \
    --install-app "$KRITI_APP_NAME"

  # stop the temporary bench (the panel will manage it from now on)
  kill "$(cat /tmp/bench-bootstrap.pid)" 2>/dev/null || true
  pkill -f "honcho start" 2>/dev/null || true
  ok "site created with all apps"
fi
bench build >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
step "10/10  Control panel + autostart"
mkdir -p "$BENCH_DIR/manage"
cp "$HERE/manage/panel.py" "$BENCH_DIR/manage/panel.py"

mkdir -p "$HOME/.config/systemd/user"
sed -e "s|@BENCH_DIR@|$BENCH_DIR|g" -e "s|@PYTHON@|$(command -v python3)|g" \
    "$HERE/systemd/erpnext-panel.service" > "$HOME/.config/systemd/user/erpnext-panel.service"
systemctl --user daemon-reload
systemctl --user enable --now erpnext-panel.service
# start panel even before graphical login
sudo loginctl enable-linger "$USER" >/dev/null 2>&1 || true
ok "panel service enabled"

echo
echo "${c_green}============================================================${c_off}"
echo "${c_green} ✅ Done!${c_off}"
echo "   Control panel : http://localhost:${PANEL_PORT}"
echo "   ERPNext       : http://${SITE}:8000   (login: Administrator / ${ADMIN_PASSWORD})"
echo
echo "   Open the panel and click ▶ Start Bench, then Open ERPNext."
echo "${c_green}============================================================${c_off}"
