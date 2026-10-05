"""Local LLM stack manager: one small dashboard for every model slot.

Serves status (shim, per-slot gatekeeper/engine state with idle countdown,
disk) and safe actions: restart a slot's gatekeeper, stop all engines,
restart the shim.  Config comes from config/stack.json (manager.port,
manager.token, slots).  The token also guards every action.

Expose only behind an authenticated tunnel - the models themselves are NOT
routed through here.  Stdlib only.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config", "stack.json"), encoding="utf-8"))
PORT = int(CFG["manager"]["port"])
BIND = CFG["manager"].get("bind", "0.0.0.0")
TOKEN = CFG["manager_token"]
SLOTS = {s["name"]: s for s in CFG["slots"]}
_PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ACT = {}
for _name, _s in SLOTS.items():
    ACT[f"{_name}.restart"] = ("slot", _name)
ACT["engines.stop"] = ("raw", ["taskkill", "/F", "/IM", "llama-server.exe"])
ACT["shim.restart"] = ("shim", None)

_LAST_ERR = {}


def http_ok(url, timeout=2.5):
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            _LAST_ERR[url] = "ok"
            return r.status == 200
    except Exception as e:
        _LAST_ERR[url] = repr(e)[:120]
        return False


def slot_status(name, cfg):
    gate = http_ok(f"http://127.0.0.1:{cfg['gate_port']}/gatekeeper/status")
    hb = None
    hb_path = os.path.join(ROOT, "logs", f"gatekeeper_{name}.json")
    try:
        st = os.stat(hb_path)
        if time.time() - st.st_mtime < 20:
            with open(hb_path) as f:
                hb = json.load(f)
    except Exception:
        pass
    engine = hb.get("engine") == "up" if hb else False
    model = "-"
    if engine:
        try:
            with _OPENER.open(f"http://127.0.0.1:{cfg['engine_port']}/v1/models", timeout=3) as r:
                model = json.loads(r.read().decode())["data"][0]["id"][:40]
        except Exception:
            pass
    return {"gatekeeper": "running" if gate else "stopped",
            "engine": "running" if engine else "stopped",
            "idle": (str(hb.get("idle_sec")) + "s/" + str(hb.get("idle_stop_sec")) + "s") if hb else "-"}


def status():
    shim_port = CFG["shim"]["port"]
    out = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
           "shim": "running" if http_ok(f"http://127.0.0.1:{shim_port}/v1/models") else "stopped"}
    for name, cfg in SLOTS.items():
        out[name] = slot_status(name, cfg)
    disk = "?"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-PSDrive C).Free/1GB"], capture_output=True, text=True, timeout=20)
        disk = "%.0f GB free on C:" % float(r.stdout.strip())
    except Exception:
        pass
    out["disk"] = disk
    out["debug"] = _LAST_ERR
    return out


def _kill_port_listeners(port):
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                         encoding="gbk", errors="replace",
                         creationflags=subprocess.CREATE_NO_WINDOW)
    pids = set()
    for line in (out.stdout or "").splitlines():
        if f":{port}" in line and "LISTENING" in line:
            pid = line.split()[-1]
            if pid.isdigit() and pid != "0":
                pids.add(pid)
    for pid in pids:
        subprocess.run(["taskkill", "/f", "/pid", pid], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return len(pids)


def do_restart_slot(name):
    cfg = SLOTS[name]
    killed = _kill_port_listeners(cfg["gate_port"])
    time.sleep(2)
    _kill_port_listeners(cfg["engine_port"])
    time.sleep(2)
    exe = _PYW
    subprocess.Popen([exe, os.path.join(ROOT, "scripts", "gatekeeper.py"),
                      "--slot", name], cwd=ROOT,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return f"slot '{name}' gatekeeper restarted (killed {killed} on gate port)"


def do_restart_shim():
    shim_port = CFG["shim"]["port"]
    _kill_port_listeners(shim_port)
    time.sleep(2)
    exe = _PYW
    subprocess.Popen([exe, os.path.join(ROOT, "scripts", "responses_shim.py")],
                     cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)
    return "shim restarted"


def run_action(action):
    kind, payload = ACT[action]
    if kind == "slot":
        return do_restart_slot(payload)
    if kind == "shim":
        return do_restart_shim()
    p = subprocess.run(payload, capture_output=True, text=True, timeout=120)
    return ((p.stdout or "") + (p.stderr or "")).strip()[-400:] or "ok"


PAGE = """<!doctype html><meta charset=utf-8><title>LLM stack</title>
<style>body{font-family:system-ui;background:#0f1420;color:#dde3ee;max-width:760px;margin:40px auto;padding:0 16px}
h1{font-size:22px}.card{background:#182032;border-radius:12px;padding:16px 20px;margin:14px 0}
.ok{color:#5ad46a}.off{color:#8a93a6}button{background:#2b6cb0;color:#fff;border:0;border-radius:8px;padding:8px 14px;margin:6px 6px 0 0;cursor:pointer}
button.warn{background:#b02b2b}small{color:#8a93a6}#s{white-space:pre-wrap;font-size:14px}</style>
<h1>Local LLM stack (full-power slots)</h1><div class=card id=s>loading…</div>
__SLOT_CARDS__
<div class=card><b>global</b><br>
<button onclick=act('engines.stop') class=warn>stop all engines (free VRAM)</button>
<button onclick=act('shim.restart')>restart Responses shim</button></div>
<small>Token-guarded. Engines start on demand and idle-stop automatically.</small>
<script>
const T=new URLSearchParams(location.search).get('token');
async function refresh(){const r=await fetch('/status?token='+T);document.getElementById('s').textContent=await r.text();}
async function act(a){if(!confirm(a+' ?'))return;const r=await fetch('/act?token='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});document.getElementById('s').textContent=a+': '+await r.text();setTimeout(refresh,3000);}
refresh();setInterval(refresh,10000);
</script>"""

PAGE = PAGE.replace("__SLOT_CARDS__", "".join(
    f'<div class=card><b>slot {n}</b><br><button onclick=act("{n}.restart")>restart gatekeeper</button></div>'
    for n in SLOTS))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        q = self.path.split("token=")[-1].split("&")[0]
        return q == TOKEN

    def do_GET(self):
        if self.path.startswith("/status"):
            if not self._auth():
                self.send_response(403); self.end_headers(); return
            body = json.dumps(status(), ensure_ascii=False, indent=1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/?") or self.path == "/":
            if not self._auth():
                self.send_response(403); self.end_headers(); return
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if not self._auth():
            self.send_response(403); self.end_headers(); return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            action = json.loads(self.rfile.read(n).decode()).get("action")
        except Exception:
            action = None
        if action not in ACT:
            self.send_response(400); self.end_headers(); return
        result = {"action": action}

        def run():
            try:
                result["result"] = run_action(action)
            except Exception as e:
                result["result"] = repr(e)[:300]
        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=300)
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"manager on http://{BIND}:{PORT}/?token={TOKEN}", flush=True)
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
