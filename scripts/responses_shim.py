"""OpenAI Responses API shim: one public endpoint for every model slot.

Translates the OpenAI Responses protocol (used by agents like pi) to Chat
Completions on the slot gatekeeper ports, both directions: input items,
tools, reasoning content, SSE streaming (with keepalive comments while an
engine cold-loads), and usage.  Unknown model ids route to the configured
default slot.

Config comes from config/stack.json:
  - upstreams.models (model id -> slot name) drives /v1/models and routing
  - auth_token is required as `Authorization: Bearer <token>` from
    non-localhost IPs; loopback (local agents, the Cloudflare tunnel
    connector behind its own Access layer) is trusted.

Stdlib only; http.client never touches the system proxy.
"""
import http.client
import json
import os
import socket
import socketserver
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "config", "stack.json"), encoding="utf-8"))
PORT = int(CFG["shim"]["port"])
BIND = CFG["shim"].get("bind", "0.0.0.0")
AUTH_TOKEN = CFG["auth_token"]
SLOT_BY_NAME = {s["name"]: ("127.0.0.1", s["gate_port"]) for s in CFG["slots"]}
MODEL_ROUTES = CFG["upstreams"]["models"]
DEFAULT_MODEL = CFG["upstreams"]["default_model"]
DEFAULT_UPSTREAM = SLOT_BY_NAME[MODEL_ROUTES[DEFAULT_MODEL]]
UPSTREAM_TIMEOUT = 660          # full-power cold load from a cold disk
LOGFILE = os.path.join(ROOT, "logs", "responses_shim.log")


def log(msg):
    line = time.strftime("[%H:%M:%S] ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def upstream_for(model):
    """Route a model id to its slot gatekeeper (substring match on keys)."""
    for model_id, slot_name in MODEL_ROUTES.items():
        if model_id in (model or "").lower():
            return SLOT_BY_NAME[slot_name]
    return DEFAULT_UPSTREAM

# ---------------------------------------------------------------- translation

def _content_to_text(content):
    """Responses content (string or part list) -> plain text for chat."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if not isinstance(part, dict):
            parts.append(str(part))
            continue
        t = part.get("type", "text")
        if t in ("input_text", "output_text", "text", "summary_text"):
            parts.append(part.get("text", ""))
        elif t in ("input_image", "image_url"):
            parts.append("[图片已省略：当前模型为纯文本]")
        elif t == "refusal":
            parts.append(part.get("refusal", ""))
    return "\n".join(p for p in parts if p)


def responses_input_to_chat(req):
    """Build the chat `messages` list from a Responses request body."""
    msgs = []
    instr = req.get("instructions")
    if instr:
        msgs.append({"role": "system", "content": instr})

    inp = req.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
        return msgs
    if not isinstance(inp, list):
        return msgs

    for item in inp:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype is None and "role" in item:
            itype = "message"
        if itype == "message":
            role = item.get("role", "user")
            text = _content_to_text(item.get("content"))
            if role in ("system", "developer"):
                msgs.append({"role": "system", "content": text})
            elif role == "assistant":
                msgs.append({"role": "assistant", "content": text})
            else:
                msgs.append({"role": "user", "content": text})
        elif itype == "function_call":
            msgs.append({
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or "call_" + uuid.uuid4().hex[:8],
                    "type": "function",
                    "function": {"name": item.get("name", ""),
                                 "arguments": item.get("arguments") or "{}"},
                }]})
        elif itype == "function_call_output":
            out = item.get("output")
            if isinstance(out, (dict, list)):
                out = json.dumps(out, ensure_ascii=False)
            msgs.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                         "content": out if out is not None else ""})
        # reasoning / item_reference and anything else: dropped
    return msgs


def to_chat_tools(tools):
    out = []
    for t in tools or []:
        if isinstance(t, dict) and t.get("type") == "function":
            out.append({"type": "function", "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            }})
    return out


def to_chat_tool_choice(tc):
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict) and tc.get("type") == "function":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    return None


def build_chat_request(req):
    body = {"model": req.get("model", "orca"),
            "messages": responses_input_to_chat(req)}
    if req.get("max_output_tokens"):
        body["max_tokens"] = int(req["max_output_tokens"])
    for key in ("temperature", "top_p"):
        if req.get(key) is not None:
            body[key] = req[key]
    tools = to_chat_tools(req.get("tools"))
    if tools:
        body["tools"] = tools
        tc = to_chat_tool_choice(req.get("tool_choice"))
        if tc:
            body["tool_choice"] = tc
    if req.get("stream"):
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


# upstream (chat completions) -> Responses -----------------------------------

def chat_tools_to_output(tool_calls):
    items = []
    for tc in tool_calls or []:
        fn = tc.get("function", {})
        items.append({"type": "function_call",
                      "id": "fc_" + uuid.uuid4().hex[:24],
                      "call_id": tc.get("id") or ("call_" + uuid.uuid4().hex[:8]),
                      "name": fn.get("name", ""),
                      "arguments": fn.get("arguments") or "{}",
                      "status": "completed"})
    return items


def message_output_item(text, reasoning=None):
    items = []
    if reasoning:
        items.append({"type": "reasoning", "id": "rs_" + uuid.uuid4().hex[:24],
                      "summary": [{"type": "summary_text", "text": reasoning}]})
    items.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:24],
                  "role": "assistant", "status": "completed",
                  "content": [{"type": "output_text", "text": text, "annotations": []}]})
    return items


def usage_block(u):
    if not u:
        return None
    pt = u.get("prompt_tokens", 0)
    ct = u.get("completion_tokens", 0)
    return {"input_tokens": pt,
            "input_tokens_details": {"cached_tokens": u.get("prompt_tokens_details", {}).get("cached_tokens", 0)},
            "output_tokens": ct,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": u.get("total_tokens", pt + ct)}


def response_skeleton(rid, model, status="in_progress"):
    return {"id": rid, "object": "response", "created_at": int(time.time()),
            "status": status, "model": model, "output": [], "error": None,
            "incomplete_details": None, "instructions": None, "metadata": {},
            "usage": None, "temperature": None, "top_p": None,
            "tool_choice": "auto", "parallel_tool_calls": True}


class UpstreamError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def upstream_request(up, body, timeout=UPSTREAM_TIMEOUT):
    conn = http.client.HTTPConnection(up[0], up[1], timeout=timeout)
    payload = json.dumps(body, ensure_ascii=False)
    conn.request("POST", "/v1/chat/completions", body=payload.encode("utf-8"),
                 headers={"Content-Type": "application/json",
                          "Accept": "text/event-stream" if body.get("stream") else "application/json"})
    resp = conn.getresponse()
    if resp.status >= 400:
        raw = resp.read().decode("utf-8", "replace")
        conn.close()
        msg = raw
        try:
            j = json.loads(raw)
            msg = j.get("error") or j.get("message") or raw
            if isinstance(msg, dict):
                msg = msg.get("message", raw)
        except Exception:
            pass
        raise UpstreamError(resp.status, str(msg)[:500])
    return conn, resp


def sse_lines(resp):
    """Yield upstream `data:` payloads from a chat completions SSE stream."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except Exception:
            continue


# ------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        try:
            # SSE deltas are token-sized writes; Nagle would delay/coalesce them
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        pass

    def _authorized(self):
        """Loopback callers (local pi, cloudflared behind Access) are trusted;
        LAN / other interfaces must present the Bearer token."""
        peer = self.client_address[0] if self.client_address else ""
        if peer.startswith("127."):
            return True
        auth = (self.headers.get("Authorization") or "").strip()
        return auth.lower() == f"bearer {AUTH_TOKEN}"

    def _json(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "responses-shim"})
        elif self.path.rstrip("/") == "/v1/models":
            if not self._authorized():
                self._json(401, {"error": {"message": "missing/invalid bearer token",
                                           "type": "authentication_error"}})
                return
            self._json(200, {"object": "list", "data": [
                {"id": mid, "object": "model", "owned_by": "local-llm-stack"}
                for mid in MODEL_ROUTES]})
        else:
            self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/responses"):
            self._json(404, {"error": {"message": "only /v1/responses is supported",
                                       "type": "invalid_request_error"}})
            return
        if not self._authorized():
            log(f"401 from {self.client_address[0] if self.client_address else '?'}")
            self._json(401, {"error": {"message": "missing/invalid bearer token",
                                       "type": "authentication_error"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as e:
            self._json(400, {"error": {"message": f"bad request body: {e}",
                                       "type": "invalid_request_error"}})
            return

        model = req.get("model", "orca")
        up = upstream_for(model)
        chat_body = build_chat_request(req)
        rid = "resp_" + uuid.uuid4().hex[:24]
        t0 = time.time()
        log(f"responses: model={model} stream={bool(req.get('stream'))} "
            f"items={len(req.get('input') or [])} -> {up[1]}")

        try:
            if req.get("stream"):
                self.handle_stream(up, chat_body, rid, model)
            else:
                self.handle_plain(up, chat_body, rid, model)
        except UpstreamError as e:
            log(f"upstream error {e.status}: {e.message[:160]}")
            try:
                self._json(e.status, {"error": {"message": e.message,
                                                "type": "upstream_error"}})
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError):
            log("client went away mid-stream")
        except Exception as e:
            log(f"shim error: {e!r}")
            try:
                self._json(500, {"error": {"message": repr(e)[:300],
                                           "type": "shim_error"}})
            except Exception:
                pass

    # ---- non-streaming ----
    def handle_plain(self, up, chat_body, rid, model):
        t0 = time.time()
        conn, resp = upstream_request(up, chat_body)
        raw = resp.read().decode("utf-8", "replace")
        conn.close()
        chat = json.loads(raw)
        choice = (chat.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        text = msg.get("content") or ""
        reasoning = msg.get("reasoning_content")
        output = message_output_item(text, reasoning)
        output += chat_tools_to_output(msg.get("tool_calls"))
        obj = response_skeleton(rid, model, "completed")
        obj["output"] = output
        obj["usage"] = usage_block(chat.get("usage"))
        log(f"done plain in {time.time()-t0:.1f}s text={len(text)}B tools={len(output)-1}")
        self._json(200, obj)

    # ---- streaming ----
    def handle_stream(self, up, chat_body, rid, model):
        t0 = time.time()
        # headers go out immediately (not after upstream answers): a 131B cold
        # start holds the upstream for minutes, and Cloudflare drops tunnels
        # after ~100s of silence.  SSE comment lines (`: keepalive`) flow every
        # 15s during that window - clients and the edge both ignore them.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        seq = [0]
        wlock = threading.Lock()   # keepalive thread and delta writes share the socket

        def send(etype, data):
            data["type"] = etype
            seq[0] += 1
            data["sequence_number"] = seq[0]
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            with wlock:
                self.wfile.write(b"event: " + etype.encode("utf-8")
                                 + b"\ndata: " + payload + b"\n\n")
                self.wfile.flush()

        skeleton = response_skeleton(rid, model, "in_progress")
        send("response.created", {"response": skeleton})
        send("response.in_progress", {"response": skeleton})

        upstream_ready = threading.Event()

        def keepalive():
            while not upstream_ready.wait(15.0):
                try:
                    with wlock:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except Exception:
                    return

        ka = threading.Thread(target=keepalive, daemon=True)
        ka.start()

        conn = None
        text_parts, reasoning_parts = [], []
        tool_acc = {}                     # index -> {"call_id","name","arguments"}
        item_index = [-1]                 # last emitted output_index
        final_items = []                  # done items, mirrored into response.completed
        usage = None
        finish = None

        try:
            conn, resp = upstream_request(up, chat_body)
        except UpstreamError as e:
            # headers are already out: report as a terminal failed event
            failed = response_skeleton(rid, model, "failed")
            failed["error"] = {"code": "upstream_error", "message": e.message}
            send("response.failed", {"response": failed})
            log(f"stream failed (headers sent): {e.status} {e.message[:160]}")
            return
        finally:
            upstream_ready.set()

        def next_index():
            item_index[0] += 1
            return item_index[0]

        def close_message_item():
            if msg_state.get("opened"):
                msg_state["opened"] = False
                text = "".join(text_parts)
                part = {"type": "output_text", "text": text, "annotations": []}
                send("response.content_part.done",
                     {"item_id": msg_state["id"], "output_index": msg_state["idx"],
                      "content_index": 0, "part": part})
                item = {"type": "message", "id": msg_state["id"],
                        "role": "assistant", "status": "completed",
                        "content": [part]}
                final_items.append(item)
                send("response.output_item.done",
                     {"output_index": msg_state["idx"], "item": item})

        def close_reasoning_item():
            if rs_state.get("opened"):
                rs_state["opened"] = False
                item = {"type": "reasoning", "id": rs_state["id"],
                        "summary": [{"type": "summary_text",
                                     "text": "".join(reasoning_parts)}]}
                final_items.append(item)
                send("response.output_item.done",
                     {"output_index": rs_state["idx"], "item": item})

        def close_tool_item(idx):
            st = tool_acc[idx]
            if st.get("opened"):
                st["opened"] = False
                send("response.function_call_arguments.done",
                     {"item_id": st["id"], "output_index": st["idx"],
                      "arguments": st["arguments"]})
                item = {"type": "function_call", "id": st["id"],
                        "call_id": st["call_id"], "name": st["name"],
                        "arguments": st["arguments"], "status": "completed"}
                final_items.append(item)
                send("response.output_item.done",
                     {"output_index": st["idx"], "item": item})

        msg_state, rs_state = {}, {}
        try:
            for chunk in sse_lines(resp):
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                ch = choices[0] if choices else {}
                delta = ch.get("delta") or {}
                fr = ch.get("finish_reason")
                if fr:
                    finish = fr

                rsn = delta.get("reasoning_content") or delta.get("reasoning")
                if rsn:
                    if not rs_state.get("opened"):
                        rs_state = {"opened": True, "idx": next_index(),
                                    "id": "rs_" + uuid.uuid4().hex[:24]}
                        send("response.output_item.added",
                             {"output_index": rs_state["idx"],
                              "item": {"type": "reasoning", "id": rs_state["id"],
                                       "summary": []}})
                    reasoning_parts.append(rsn)
                    send("response.reasoning_text.delta",
                         {"item_id": rs_state["id"], "output_index": rs_state["idx"],
                          "content_index": 0, "delta": rsn})

                if delta.get("content"):
                    if not msg_state.get("opened"):
                        close_reasoning_item()
                        msg_state = {"opened": True, "idx": next_index(),
                                     "id": "msg_" + uuid.uuid4().hex[:24]}
                        send("response.output_item.added",
                             {"output_index": msg_state["idx"],
                              "item": {"type": "message", "id": msg_state["id"],
                                       "role": "assistant", "status": "in_progress",
                                       "content": []}})
                        send("response.content_part.added",
                             {"item_id": msg_state["id"],
                              "output_index": msg_state["idx"], "content_index": 0,
                              "part": {"type": "output_text", "text": "",
                                       "annotations": []}})
                    text_parts.append(delta["content"])
                    send("response.output_text.delta",
                         {"item_id": msg_state["id"], "output_index": msg_state["idx"],
                          "content_index": 0, "delta": delta["content"]})

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    st = tool_acc.setdefault(idx, {"opened": False, "call_id": "",
                                                   "name": "", "arguments": "",
                                                   "id": "", "idx": -1})
                    if not st["opened"]:
                        close_message_item()
                        st["idx"] = next_index()
                        st["id"] = "fc_" + uuid.uuid4().hex[:24]
                        st["call_id"] = tc.get("id") or ("call_" + uuid.uuid4().hex[:8])
                        st["name"] = (tc.get("function") or {}).get("name", "")
                        st["opened"] = True
                        send("response.output_item.added",
                             {"output_index": st["idx"],
                              "item": {"type": "function_call", "id": st["id"],
                                       "call_id": st["call_id"], "name": st["name"],
                                       "arguments": "", "status": "in_progress"}})
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        st["name"] += fn["name"]
                    if fn.get("arguments"):
                        st["arguments"] += fn["arguments"]
                        send("response.function_call_arguments.delta",
                             {"item_id": st["id"], "output_index": st["idx"],
                              "delta": fn["arguments"]})

            close_message_item()
            close_reasoning_item()
            for idx in sorted(tool_acc):
                close_tool_item(idx)

            final = response_skeleton(rid, model, "completed")
            final["output"] = final_items
            final["usage"] = usage_block(usage)
            send("response.completed", {"response": final})
            log(f"done stream in {time.time()-t0:.1f}s finish={finish} "
                f"text={len(''.join(text_parts))}B reasoning={len(''.join(reasoning_parts))}B "
                f"tools={len(tool_acc)}")
        finally:
            try:
                conn.close()
            except Exception:
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False       # natural mutex, like the gatekeeper


if __name__ == "__main__":
    srv = Server((BIND, PORT), Handler)
    routes = ", ".join(f"{mid}->{SLOT_BY_NAME[sn][1]}" for mid, sn in MODEL_ROUTES.items())
    log(f"responses shim on {BIND}:{PORT} ({routes}; default {DEFAULT_MODEL}->{DEFAULT_UPSTREAM[1]}; "
        f"bearer '{AUTH_TOKEN}' required from non-localhost IPs)")
    srv.serve_forever()
