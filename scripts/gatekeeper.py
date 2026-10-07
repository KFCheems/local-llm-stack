"""Full-power model slot gatekeeper: on-demand start + idle auto-stop.

One instance per model slot.  Listens on the slot's gate port; the real
llama-server lives on the engine port.  On the first INFER/DOC request it
launches the slot's start_bat and holds the connection until /health is ok,
then splices raw bytes (SSE-safe).  After IDLE_STOP seconds with no real
activity it kills the engine process, freeing VRAM for the next slot.

Exclusivity: full-power slots cannot share a consumer GPU.  When a slot's
start is requested while an `exclusive_with` slot's engine is up, the
blocker is evicted (killed) if its own gatekeeper heartbeat shows it has
been idle for at least `exclusive_idle_kill_sec` (default 60s); otherwise
the start is refused with a 503 that names the blocker.

Auth: requests from 127.0.0.1 are trusted (the Cloudflare tunnel connector
runs locally and its own Access layer is the gate there).  Requests from any
other IP must carry `Authorization: Bearer <auth_token>`.

Runs from config/stack.json:  python gatekeeper.py --slot slot1 [--config path]
Stdlib only.  Stop: taskkill this python (or use the manager dashboard).
"""
import argparse
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.request

IDLE_STOP_SEC = 300
START_TIMEOUT_SEC = 300
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

lock = threading.Lock()
start_condition = threading.Condition(lock)
last_activity = time.time()
starting = False
startup_result = (False, "NOT_STARTED")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CFG = {}


def log(name, msg):
    line = time.strftime("[%H:%M:%S] ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(os.path.join(ROOT, "logs", f"gatekeeper_{name}.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config(path, slot_name):
    global CFG
    with open(path, encoding="utf-8") as f:
        CFG = json.load(f)
    for s in CFG["slots"]:
        if s["name"] == slot_name:
            return s
    raise SystemExit(f"slot '{slot_name}' not found in {path}")


def engine_port_of(slot_name):
    for s in CFG["slots"]:
        if s["name"] == slot_name:
            return s["engine_port"]
    return None


def engine_up(timeout=1.5, port=None):
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def exclusive_engine_up(slot):
    for other in slot.get("exclusive_with", []):
        port = engine_port_of(other)
        if port and engine_up(port=port):
            return other, port
    return None, None


def port_open(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def blocker_idle_sec(blocker):
    """Idle seconds reported by another slot's gatekeeper heartbeat, or None
    when the heartbeat is stale/absent or its engine is not up."""
    path = os.path.join(ROOT, "logs", f"gatekeeper_{blocker}.json")
    try:
        if time.time() - os.stat(path).st_mtime > 30:
            return None
        with open(path) as f:
            hb = json.load(f)
        if hb.get("engine") != "up":
            return None
        return int(hb.get("idle_sec") or 0)
    except Exception:
        return None


def stop_engine(name, port):
    log(name, "idle - stopping the engine (frees VRAM)")
    p = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                       encoding="gbk", errors="replace",
                       creationflags=subprocess.CREATE_NO_WINDOW)
    pid = None
    for line in (p.stdout or "").splitlines():
        if f":{port}" in line and "LISTENING" in line:
            pid = line.split()[-1]
            break
    if pid and pid != "0":
        subprocess.run(["taskkill", "/f", "/pid", pid], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        log(name, f"stopped pid {pid}")
    else:
        log(name, "no listener found on engine port")


def start_engine(name, slot):
    global starting, startup_result
    with start_condition:
        if starting:
            # Concurrent requests must wait for the owner's readiness check.
            # A listening llama-server can still return 503 while loading.
            deadline = time.monotonic() + START_TIMEOUT_SEC
            while starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, "TIMEOUT"
                start_condition.wait(timeout=remaining)
            return startup_result
        starting = True
    result = (False, "TIMEOUT")
    try:
        blocker, bport = exclusive_engine_up(slot)
        if blocker:
            idle = blocker_idle_sec(blocker)
            kill_after = int(CFG.get("exclusive_idle_kill_sec", 60))
            if idle is not None and idle >= kill_after:
                log(name, f"exclusive slot '{blocker}' idle {idle}s >= {kill_after}s - evicting it")
                stop_engine(blocker, bport)
                deadline = time.time() + 15
                while port_open(bport) and time.time() < deadline:
                    time.sleep(0.5)
                if port_open(bport):
                    log(name, "blocker engine port still open; VRAM may not be freed yet")
            else:
                state = "unknown" if idle is None else f"idle {idle}s < {kill_after}s"
                log(name, f"start refused: slot '{blocker}' (port {bport}) holds the VRAM ({state})")
                result = (False, f"REFUSED:{blocker}")
                return result
        log(name, "engine down - starting ...")
        subprocess.Popen([slot["start_bat"]], cwd=ROOT,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.time() + START_TIMEOUT_SEC
        while time.time() < deadline:
            if engine_up(port=slot["engine_port"]):
                log(name, "engine ready")
                result = (True, "running")
                return result
            # llama-server answers 503 while the model loads, so poll tightly:
            # every extra second here is dead latency on the cold request.
            time.sleep(0.25)
        log(name, "engine did not become ready in time")
        return result
    except Exception as e:
        log(name, f"engine launch failed: {type(e).__name__}: {e}")
        result = (False, "START_FAILED")
        return result
    finally:
        with start_condition:
            startup_result = result
            starting = False
            start_condition.notify_all()


def idle_watchdog(name, slot, heartbeat, idle_stop):
    global last_activity
    engine_up_cached = False
    last_check = 0.0
    while True:
        time.sleep(5)
        if time.time() - last_check > 10:
            engine_up_cached = engine_up(port=slot["engine_port"])
            last_check = time.time()
        with lock:
            idle_for = time.time() - last_activity
        try:
            with open(heartbeat, "w") as f:
                json.dump({"engine": "up" if engine_up_cached else "down",
                           "idle_sec": int(idle_for), "idle_stop_sec": idle_stop,
                           "ts": time.strftime("%H:%M:%S")}, f)
        except Exception:
            pass
        if idle_for > idle_stop and engine_up_cached:
            stop_engine(name, slot["engine_port"])


PLACEHOLDER = ("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
               "Connection: close\r\n\r\n"
               "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=10>"
               "<body style='font-family:sans-serif;background:#111;color:#ddd;"
               "display:flex;align-items:center;justify-content:center;height:95vh'>"
               "<div style=text-align:center><h1>Model loading&hellip;</h1>"
               "<p>The engine starts on demand; the page refreshes.</p></div>").encode("utf-8")


def connect_engine(port, timeout=10):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # token-sized relays
    s.setblocking(False)
    return s


def splice(a, b, count=True):
    global last_activity
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 30)
            if x:
                break
            if not r:
                if count:
                    with lock:
                        last_activity = time.time()
                continue
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                other = b if s is a else a
                other.sendall(data)
                if count:
                    with lock:
                        last_activity = time.time()
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


def classify(head):
    first = head.split(b"\r\n")[0]
    path = (first.split(b" ")[1] if len(first.split(b" ")) > 1 else b"/")
    if first.startswith(b"GET /gatekeeper/status"):
        return "STATUS"
    if path.startswith((b"/v1/chat", b"/v1/messages", b"/v1/completions", b"/v1/responses")):
        return "INFER"
    if first.startswith(b"GET") and b"text/html" in head.lower():
        return "DOC"
    return "OTHER"


def send_raw(client, status, body, ctype):
    """Framed one-shot response: Content-Length + explicit shutdown/close.
    Without Content-Length an HTTP/1.1 client waits for the connection to
    close before it accepts the answer."""
    payload = body if isinstance(body, bytes) else body.encode("utf-8")
    head = (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode("ascii")
    try:
        client.sendall(head + payload)
    finally:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def pipe(client, name, slot, heartbeat, idle_stop, auth_token):
    global last_activity
    try:
        head = bytearray()
        client.settimeout(10)
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = client.recv(8192)
            if not chunk:
                return
            head += chunk

        peer = client.getpeername()[0] if client.getpeername() else ""
        trusted = peer.startswith("127.")
        if not trusted:
            auth = ""
            for line in bytes(head).split(b"\r\n"):
                if line.lower().startswith(b"authorization:"):
                    auth = line[14:].decode("utf-8", "replace").strip()
                    break
            if auth.lower() != f"bearer {auth_token}":
                send_raw(client, "401 Unauthorized",
                         '{"error": "missing/invalid bearer token"}', "application/json")
                return

        kind = classify(bytes(head))
        if kind == "STATUS":
            try:
                with open(heartbeat) as f:
                    body = json.dumps(json.load(f)).encode()
            except Exception:
                body = json.dumps({"engine": "unknown", "idle_sec": None,
                                   "idle_stop_sec": idle_stop}).encode()
            send_raw(client, "200 OK", body, "application/json")
            return

        if kind in ("INFER", "DOC"):
            with lock:
                last_activity = time.time()

        if engine_up(port=slot["engine_port"]):
            upstream = connect_engine(slot["engine_port"])
        else:
            if kind == "DOC":
                send_raw(client, "200 OK", PLACEHOLDER.split(b"\r\n\r\n", 1)[1], "text/html; charset=utf-8")
                start_engine(name, slot)
                return
            if kind != "INFER":
                send_raw(client, "503 Service Unavailable",
                         '{"error": "engine is asleep (idle stop) - send a chat message"}',
                         "application/json")
                return
            ok, why = start_engine(name, slot)
            if not ok:
                msg = (f"slot '{why.split(':', 1)[1]}' holds the VRAM: wait for its idle stop, then retry."
                       if why.startswith("REFUSED:")
                       else "engine failed to start - check the gatekeeper log.")
                send_raw(client, "503 Service Unavailable", msg, "text/plain; charset=utf-8")
                return
            upstream = connect_engine(slot["engine_port"])

        upstream.sendall(bytes(head))
        splice(client, upstream, count=(kind in ("INFER", "DOC")))
    except Exception as e:
        log(name, f"connection error: {e!r}")
        try:
            client.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", required=True)
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "stack.json"))
    args = ap.parse_args()

    slot = load_config(args.config, args.slot)
    name = slot["name"]
    auth_token = CFG.get("auth_token", "change-me-bearer-token")
    idle_stop = int(slot.get("idle_stop_sec", IDLE_STOP_SEC))
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    heartbeat = os.path.join(ROOT, "logs", f"gatekeeper_{name}.json")

    threading.Thread(target=idle_watchdog, args=(name, slot, heartbeat, idle_stop),
                     daemon=True).start()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", slot["gate_port"]))
    except OSError as e:
        log(name, f"gate port {slot['gate_port']} taken ({e}) - another gatekeeper is running; exiting")
        return
    srv.listen(16)
    log(name, f"gatekeeper '{name}' on 127.0.0.1:{slot['gate_port']} -> engine :{slot['engine_port']} "
              f"(idle stop {idle_stop}s; exclusive with {slot.get('exclusive_with', [])})")
    while True:
        try:
            client, _ = srv.accept()
        except Exception as e:
            log(name, f"accept error (continuing): {e!r}")
            time.sleep(0.5)
            continue
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        client.setblocking(True)
        threading.Thread(target=pipe,
                         args=(client, name, slot, heartbeat, idle_stop, auth_token),
                         daemon=True).start()


if __name__ == "__main__":
    main()
