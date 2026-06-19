#!/usr/bin/env python3
"""
ERPNext Bench Control Panel
A self-contained web dashboard (Python stdlib only) to manage the local
frappe-bench: start/stop/restart bench, start/stop the MariaDB container,
view live status + installed app versions, tail logs, and open ERPNext.

Run:  python3 manage/panel.py   (then open http://localhost:9009)
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------- config
BENCH_DIR = Path(__file__).resolve().parent.parent          # /home/.../frappe-bench
STATE_DIR = Path(__file__).resolve().parent                 # /home/.../frappe-bench/manage
PID_FILE = STATE_DIR / "bench.pid"
LOG_FILE = STATE_DIR / "bench.log"
PANEL_PORT = 9009

SITE = "erp.localhost"
SITE_URL = "http://erp.localhost:8000"
DB_CONTAINER = "frappe-mariadb"

BENCH = shutil.which("bench") or os.path.expanduser("~/.local/bin/bench")
DOCKER = shutil.which("docker") or "docker"

# ports used to infer "is bench up"
WEB_PORT = 8000
REDIS_CACHE_PORT = 13000


# ---------------------------------------------------------------- helpers
def port_open(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def run(cmd, **kw):
    """Run a command, return (rc, stdout+stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=kw.get("timeout", 60))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


def bench_pid():
    """Return the live bench (honcho) PID if our managed process is running."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)        # raises if not alive
            return pid
        except (ProcessLookupError, ValueError, PermissionError):
            return None
    return None


def db_running():
    rc, out = run([DOCKER, "inspect", "-f", "{{.State.Running}}", DB_CONTAINER])
    return rc == 0 and out.strip() == "true"


def start_db():
    if db_running():
        return "DB already running"
    rc, out = run([DOCKER, "start", DB_CONTAINER])
    return "DB started" if rc == 0 else f"DB start failed: {out.strip()}"


def stop_db():
    if not db_running():
        return "DB already stopped"
    rc, out = run([DOCKER, "stop", DB_CONTAINER])
    return "DB stopped" if rc == 0 else f"DB stop failed: {out.strip()}"


def start_bench():
    if bench_pid():
        return "Bench already running"
    if port_open(REDIS_CACHE_PORT):
        return "Ports in use — another bench is already running outside the panel"
    start_db()
    log = open(LOG_FILE, "wb")
    proc = subprocess.Popen(
        [BENCH, "start"],
        cwd=str(BENCH_DIR),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,            # own process group -> clean kill
        env={**os.environ, "PATH": os.path.dirname(BENCH) + os.pathsep + os.environ.get("PATH", "")},
    )
    PID_FILE.write_text(str(proc.pid))
    return f"Bench starting (pid {proc.pid}) — give it ~10s"


def stop_bench():
    pid = bench_pid()
    if not pid:
        # best effort cleanup anyway
        run(["pkill", "-f", "honcho start"])
        PID_FILE.unlink(missing_ok=True)
        return "Bench was not tracked as running (cleaned up stragglers)"
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    run(["pkill", "-f", "honcho start"])
    PID_FILE.unlink(missing_ok=True)
    return "Bench stopped"


_busy = {"task": None}  # name of the maintenance task currently running, if any


def run_async(name, cmd):
    """Run a bench maintenance command in the background, streaming to the log."""
    if _busy["task"]:
        return f"Busy: '{_busy['task']}' is still running"
    if not bench_pid() and name in ("Migrate", "Clear Cache", "Backup"):
        return f"Bench must be running before '{name}'"
    _busy["task"] = name

    def worker():
        try:
            with open(LOG_FILE, "ab") as log:
                banner = f"\n========== {name} :: {time.strftime('%H:%M:%S')} ==========\n"
                log.write(banner.encode())
                log.flush()
                p = subprocess.Popen(
                    cmd, cwd=str(BENCH_DIR), stdout=log, stderr=subprocess.STDOUT,
                    env={**os.environ, "PATH": os.path.dirname(BENCH) + os.pathsep + os.environ.get("PATH", "")},
                )
                p.wait()
                log.write(f"========== {name} finished (rc={p.returncode}) ==========\n".encode())
        finally:
            _busy["task"] = None

    threading.Thread(target=worker, daemon=True).start()
    return f"{name} started — watch the log below"


def app_versions():
    apps = ["frappe", "erpnext", "hrms", "india_compliance", "kriti_app"]
    out = {}
    for app in apps:
        init = BENCH_DIR / "apps" / app / app / "__init__.py"
        ver = "—"
        try:
            m = re.search(r'__version__\s*=\s*["\']([^"\']+)', init.read_text())
            if m:
                ver = m.group(1)
        except FileNotFoundError:
            ver = "not installed"
        # branch
        branch = ""
        rc, b = run(["git", "-C", str(BENCH_DIR / "apps" / app), "rev-parse", "--abbrev-ref", "HEAD"])
        if rc == 0:
            branch = b.strip()
        out[app] = {"version": ver, "branch": branch}
    return out


def status():
    bench_up = bool(bench_pid()) or port_open(REDIS_CACHE_PORT)
    web_up = port_open(WEB_PORT)
    return {
        "db": db_running(),
        "bench": bench_up,
        "web": web_up,
        "site": SITE,
        "site_url": SITE_URL,
        "versions": app_versions(),
    }


def tail_log(n=120):
    if not LOG_FILE.exists():
        return "(no log yet — start bench to generate output)"
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"(could not read log: {e})"


# ---------------------------------------------------------------- HTTP
HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ERPNext Bench Control Panel</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--border:#30363d;--fg:#e6edf3;--muted:#8b949e;
        --green:#2ea043;--red:#da3633;--amber:#d29922;--blue:#1f6feb;}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--fg);}
  header{padding:20px 28px;border-bottom:1px solid var(--border);display:flex;
         align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
  h1{font-size:18px;margin:0;font-weight:600}
  .sub{color:var(--muted);font-size:13px;margin-top:3px}
  main{max-width:1000px;margin:0 auto;padding:24px 28px 60px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:24px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px}
  .card h2{font-size:13px;margin:0 0 10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;vertical-align:middle}
  .up{background:var(--green);box-shadow:0 0 8px var(--green)}
  .down{background:var(--red)}
  .stat{font-size:20px;font-weight:600;display:flex;align-items:center}
  .btns{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 22px}
  button{font-size:14px;font-weight:500;border:1px solid var(--border);border-radius:8px;
         padding:10px 16px;cursor:pointer;color:var(--fg);background:#21262d;transition:.15s}
  button:hover{border-color:#6e7681}
  button:disabled{opacity:.5;cursor:not-allowed}
  .b-green{background:var(--green);border-color:var(--green)}
  .b-red{background:var(--red);border-color:var(--red)}
  .b-blue{background:var(--blue);border-color:var(--blue)}
  a.btn{display:inline-block;text-decoration:none}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase}
  td code{background:#21262d;padding:2px 7px;border-radius:5px;font-size:13px}
  .branch{color:var(--muted);font-size:12px}
  pre{background:#010409;border:1px solid var(--border);border-radius:8px;padding:14px;
      overflow:auto;max-height:340px;font-size:12px;line-height:1.5;margin:0}
  .toast{position:fixed;bottom:20px;right:20px;background:var(--card);border:1px solid var(--border);
         border-left:3px solid var(--blue);padding:12px 18px;border-radius:8px;font-size:14px;
         opacity:0;transform:translateY(10px);transition:.25s;pointer-events:none;max-width:360px}
  .toast.show{opacity:1;transform:none}
  .row{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
  .muted{color:var(--muted);font-size:12px}
</style>
</head>
<body>
<header>
  <div>
    <h1>⚙️ ERPNext Bench Control Panel</h1>
    <div class="sub">site: <code id="site">erp.localhost</code> &nbsp;·&nbsp; MariaDB on :3307 (Docker) &nbsp;·&nbsp; auto-refresh 5s</div>
  </div>
  <a class="btn" id="openErp" href="#" target="_blank"><button class="b-blue">🌐 Open ERPNext</button></a>
</header>
<main>
  <div class="grid">
    <div class="card"><h2>MariaDB (Docker)</h2><div class="stat"><span id="d-db" class="dot down"></span><span id="t-db">…</span></div></div>
    <div class="card"><h2>Bench services</h2><div class="stat"><span id="d-bench" class="dot down"></span><span id="t-bench">…</span></div></div>
    <div class="card"><h2>Web (:8000)</h2><div class="stat"><span id="d-web" class="dot down"></span><span id="t-web">…</span></div></div>
  </div>

  <div class="btns">
    <button class="b-green" onclick="act('/api/bench/start')">▶ Start Bench</button>
    <button class="b-red"   onclick="act('/api/bench/stop')">■ Stop Bench</button>
    <button onclick="act('/api/bench/restart')">↻ Restart Bench</button>
    <button class="b-green" onclick="act('/api/db/start')">▶ Start DB</button>
    <button class="b-red"   onclick="act('/api/db/stop')">■ Stop DB</button>
  </div>

  <h2 style="color:var(--muted);text-transform:uppercase;letter-spacing:.5px;font-size:13px;margin:0 0 8px">Maintenance</h2>
  <div class="btns">
    <button onclick="act('/api/site/migrate')">🛠 Migrate</button>
    <button onclick="act('/api/site/clear-cache')">🧹 Clear Cache</button>
    <button onclick="act('/api/site/build')">📦 Build Assets</button>
    <button onclick="act('/api/site/backup')">💾 Backup</button>
  </div>

  <div class="card" style="margin-bottom:24px">
    <h2>Installed Apps</h2>
    <table><thead><tr><th>App</th><th>Version</th><th>Branch</th></tr></thead>
    <tbody id="apps"><tr><td colspan="3" class="muted">loading…</td></tr></tbody></table>
  </div>

  <div class="card">
    <div class="row"><h2 style="margin:0">Bench Log (tail)</h2>
      <button onclick="refreshLog()" style="padding:5px 12px;font-size:12px">refresh</button></div>
    <pre id="log">…</pre>
  </div>
</main>
<div class="toast" id="toast"></div>

<script>
function setDot(id,up,txt){document.getElementById('d-'+id).className='dot '+(up?'up':'down');
  document.getElementById('t-'+id).textContent=txt;}
async function refresh(){
  try{
    const s=await (await fetch('/api/status')).json();
    setDot('db',s.db,s.db?'Running':'Stopped');
    setDot('bench',s.bench,s.bench?'Running':'Stopped');
    setDot('web',s.web,s.web?'Up (HTTP)':'Down');
    document.getElementById('site').textContent=s.site;
    document.getElementById('openErp').href=s.site_url;
    const rows=Object.entries(s.versions).map(([a,v])=>
      `<tr><td><b>${a}</b></td><td><code>${v.version}</code></td><td class="branch">${v.branch||''}</td></tr>`).join('');
    document.getElementById('apps').innerHTML=rows;
  }catch(e){}
}
async function refreshLog(){
  try{document.getElementById('log').textContent=await (await fetch('/api/logs')).text();
      const p=document.getElementById('log');p.scrollTop=p.scrollHeight;}catch(e){}
}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3500);}
async function act(url){
  toast('Working…');
  try{const r=await (await fetch(url,{method:'POST'})).json();toast(r.message||'done');}
  catch(e){toast('Error: '+e);}
  setTimeout(()=>{refresh();refreshLog();},1500);
}
refresh();refreshLog();
setInterval(refresh,5000);
setInterval(refreshLog,8000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def log_message(self, *a):
        pass  # quiet

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(status())
        elif path == "/api/logs":
            self._send(200, tail_log(), "text/plain; charset=utf-8")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        actions = {
            "/api/bench/start": start_bench,
            "/api/bench/stop": stop_bench,
            "/api/bench/restart": lambda: (stop_bench(), __import__("time").sleep(2), start_bench())[-1],
            "/api/db/start": start_db,
            "/api/db/stop": stop_db,
            "/api/site/migrate": lambda: run_async("Migrate", [BENCH, "--site", SITE, "migrate"]),
            "/api/site/clear-cache": lambda: run_async("Clear Cache", [BENCH, "--site", SITE, "clear-cache"]),
            "/api/site/build": lambda: run_async("Build Assets", [BENCH, "build"]),
            "/api/site/backup": lambda: run_async("Backup", [BENCH, "--site", SITE, "backup"]),
        }
        if path in actions:
            self._json({"message": actions[path]()})
        else:
            self._json({"error": "not found"}, 404)


def main():
    STATE_DIR.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    print(f"ERPNext Control Panel  ->  http://localhost:{PANEL_PORT}")
    print(f"Managing bench at: {BENCH_DIR}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
