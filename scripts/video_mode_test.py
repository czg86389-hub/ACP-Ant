# -*- coding: utf-8 -*-
"""Offline test of video mode (AGY_VIDEO_MODE=1): video, audio and PDF become files the read_file-only agent may open,
and nothing else can be opened.

    python3 scripts/video_mode_test.py

Needs no account, network or agy: the agy process is replaced by a fake that plays back what agy 1.5.3 sends (a
read_file tool call, then the answer). Checks parsing in the three APIs, the Gemini-only rule and the limits, that
request files are written with random names and deleted afterwards together with agy's own copies, that tool calls
never reach the client, and that a request whose model opens any other path is stopped before the content gets out."""
import asyncio
import atexit
import base64
import json
import os
import shutil
import sys
import tempfile

os.environ["AGY_ROOT"] = tempfile.mkdtemp(prefix="acp-ant-video-test-")
atexit.register(shutil.rmtree, os.environ["AGY_ROOT"], True)
os.environ["AGY_GATEWAY_KEY"] = "test-key"
os.environ["AGY_VIDEO_MODE"] = "1"
os.environ["AGY_VIDEO_MAX_FILES"] = "3"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import acp_ant as gw  # noqa: E402

MP4_RAW = b"\0\0\0\x18ftypmp42" + b"\1" * 64
MP4 = base64.b64encode(MP4_RAW).decode()
PDF = base64.b64encode(b"%PDF-1.4 test").decode()
WAV = base64.b64encode(b"RIFF\0\0\0\0WAVEfmt ").decode()
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 16).decode()
ZIP = base64.b64encode(b"PK\3\4zip").decode()
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name, "" if cond else "  -> " + str(detail)[:300]))


def refused(name, fn, *words):
    try:
        out = fn()
    except gw.BadContent as e:
        check(name, all(w in str(e) for w in words), "message lacks %s: %s" % (words, e))
        return e
    check(name, False, "not refused, got %r" % (out,))


def uri(mime, data):
    return "data:%s;base64,%s" % (mime, data)


def oa(*parts, model="gemini-4-argon"):
    return {"model": model, "messages": [{"role": "user", "content": list(parts)}]}


def kinds(msgs, i=0):
    return [(p["type"], p.get("mime")) for p in msgs[i]["parts"]]


check("video mode is on in this test", gw.VIDEO_MODE)

# ---- parsing: video / audio / PDF become media parts -----------------------------------------------------------------
_s, msgs, _t, _c = gw.from_openai(oa({"type": "text", "text": "break this down"}, {"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}))
check("openai: file part with video/mp4 -> media", kinds(msgs) == [("text", None), ("media", "video/mp4")] and msgs[0]["parts"][1]["where"] == "messages[0].content[1]", msgs)
_s, msgs, _t, _c = gw.from_openai(oa({"type": "input_audio", "input_audio": {"data": WAV, "format": "wav"}}))
check("openai: input_audio wav -> media audio/wav", kinds(msgs) == [("media", "audio/wav")], msgs)
_s, msgs, _t, _c = gw.from_openai(oa({"type": "input_audio", "input_audio": {"data": WAV, "format": "mp3"}}))
check("openai: input_audio mp3 -> media audio/mpeg", kinds(msgs) == [("media", "audio/mpeg")], msgs)
refused("openai: input_audio with an unknown format refused", lambda: gw.from_openai(oa({"type": "input_audio", "input_audio": {"data": WAV, "format": "midi"}})), '"midi"')
_s, msgs, _t, _c = gw.from_openai(oa({"type": "video_url", "video_url": {"url": uri("video/mp4", MP4)}}))
check("openai: video_url data URI -> media", kinds(msgs) == [("media", "video/mp4")], msgs)
refused("openai: video_url link refused (no downloads)", lambda: gw.from_openai(oa({"type": "video_url", "video_url": {"url": "https://x/v.mp4"}})),
        "does not download", "messages[0].content[0]")
_s, msgs, _t, _c = gw.from_openai(oa({"type": "image_url", "image_url": {"url": uri("video/mp4", MP4)}}))
check("openai: image_url carrying a video data URI -> media", kinds(msgs) == [("media", "video/mp4")], msgs)
_s, msgs, _t, _c = gw.from_openai(oa({"type": "file", "file": {"file_data": uri("application/pdf", PDF), "filename": "a.pdf"}}))
check("openai: PDF file -> media", kinds(msgs) == [("media", "application/pdf")], msgs)
refused("openai: zip file still refused", lambda: gw.from_openai(oa({"type": "file", "file": {"file_data": uri("application/zip", ZIP)}})), "application/zip")
refused("openai: file by file_id still refused", lambda: gw.from_openai(oa({"type": "file", "file": {"file_id": "file-1"}})), "file_id")
refused("openai: video in a system message refused", lambda: gw.from_openai({"messages": [{"role": "system", "content": [
    {"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}]}, {"role": "user", "content": "q"}]}), "only contain text")
_s, msgs, _t, _c = gw.from_openai(oa({"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": uri("image/png", PNG)}}))
check("openai: images unchanged", kinds(msgs) == [("text", None), ("image", "image/png")], msgs)


def an(*blocks):
    return {"model": "gemini-4-argon", "messages": [{"role": "user", "content": list(blocks)}]}


_s, msgs, _t, _c = gw.from_anthropic(an({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": PDF}}))
check("anthropic: PDF document -> media", kinds(msgs) == [("media", "application/pdf")] and msgs[0]["parts"][0]["where"] == "messages.0.content.0", msgs)
_s, msgs, _t, _c = gw.from_anthropic(an({"type": "image", "source": {"type": "base64", "media_type": "video/mp4", "data": MP4}}))
check("anthropic: image block with video/mp4 -> media", kinds(msgs) == [("media", "video/mp4")], msgs)
refused("anthropic: document by URL still refused", lambda: gw.from_anthropic(an({"type": "document", "source": {"type": "url", "url": "https://x/a.pdf"}})), '"url"')
_s, msgs, _t, _c = gw.from_anthropic({"messages": [
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "fetch", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": PDF}}]}]}]})
check("anthropic: PDF inside a tool_result -> media in the tool message", [p["type"] for p in msgs[1]["parts"]] == ["tool_result", "media"], msgs)


def rs(*parts):
    return {"model": "gemini-4-argon", "input": [{"type": "message", "role": "user", "content": list(parts)}]}


_s, msgs, _t, _c, _x = gw.from_responses(rs({"type": "input_file", "filename": "a.pdf", "file_data": uri("application/pdf", PDF)}))
check("responses: input_file PDF -> media", kinds(msgs) == [("media", "application/pdf")], msgs)
_s, msgs, _t, _c, _x = gw.from_responses(rs({"type": "input_audio", "input_audio": {"data": WAV, "format": "wav"}}))
check("responses: input_audio -> media", kinds(msgs) == [("media", "audio/wav")], msgs)
refused("responses: input_file by URL still refused", lambda: gw.from_responses(rs({"type": "input_file", "file_url": "https://x/a.pdf"})), "URL")

# ---- prepare_media: Gemini only, limits, decoding ------------------------------------------------------------------
_s, msgs, _t, _c = gw.from_openai(oa({"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}))
refused("non-Gemini model refused", lambda: gw.prepare_media(msgs, "claude-sonnet-5-5"), "only to Gemini", "claude-sonnet-5-5", "messages[0].content[0]")
refused("unknown model name refused (no silent fallback with files)", lambda: gw.prepare_media(msgs, "gpt-4o"), "gpt-4o")
gw.prepare_media(msgs, "gemini-4-argon-high")
check("Gemini model accepted, data decoded to bytes", msgs[0]["parts"][0]["data"] == MP4_RAW, type(msgs[0]["parts"][0]["data"]))
_s, msgs, _t, _c = gw.from_openai(oa(*[{"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}] * 4))
refused("more files than AGY_VIDEO_MAX_FILES refused", lambda: gw.prepare_media(msgs, "gemini-4-argon"), "at most 3", "messages[0].content[3]")
_s, msgs, _t, _c = gw.from_openai(oa({"type": "file", "file": {"file_data": uri("video/mp4", "not base64!!")}}))
refused("invalid base64 refused", lambda: gw.prepare_media(msgs, "gemini-4-argon"), "not valid base64")
gw.VIDEO_MAX_MB, old_mb = 0.00005, gw.VIDEO_MAX_MB
_s, msgs, _t, _c = gw.from_openai(oa({"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}))
refused("files over AGY_VIDEO_MAX_MB refused", lambda: gw.prepare_media(msgs, "gemini-4-argon"), "AGY_VIDEO_MAX_MB")
gw.VIDEO_MAX_MB = old_mb

# ---- to_acp: media blocks in place ---------------------------------------------------------------------------------
_s, msgs, _t, _c = gw.from_openai({"messages": [
    {"role": "user", "content": [{"type": "text", "text": "first"}, {"type": "file", "file": {"file_data": uri("application/pdf", PDF)}}]},
    {"role": "assistant", "content": "ok"},
    {"role": "user", "content": [{"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}, {"type": "text", "text": "and this?"}]}]})
gw.prepare_media(msgs, "gemini-4-argon")
_sys, blocks = gw.to_acp("", msgs, [], "auto")
media_blocks = [b for b in blocks if b["type"] == "media"]
check("to_acp: history and current files become media blocks in order",
      [b["mime"] for b in media_blocks] == ["application/pdf", "video/mp4"] and "[attachment 1: application/pdf]" in blocks[0]["text"], blocks)
_s, msgs, _t, _c = gw.from_openai(oa({"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}))
gw.prepare_media(msgs, "gemini-4-argon")
_sys, blocks = gw.to_acp("", msgs, [], "auto")
check("to_acp: a file without text still gets a text block", any(b["type"] == "text" and b["text"].strip() for b in blocks), blocks)

# ---- tool-call guard -----------------------------------------------------------------------------------------------
req = os.path.join(os.environ["AGY_ROOT"], "accounts", "g1", "media", "w", "a" * 32)
att = os.path.join(req, "attachment-1.mp4")
allowed = {os.path.normpath(att)}


def tc(path, name="read_file", kind="read", sess="tool_call"):
    u = {"sessionUpdate": sess, "toolCallId": "c1", "name": name, "kind": kind, "status": "in_progress"}
    if path is not None:
        u.update({"args": {"Path": path}, "locations": [{"path": path}], "rawInput": {"Path": path}})
    return u


check("guard: reading the attachment is fine", gw.tool_call_problem(tc(att), allowed, req, True) == "")
check("guard: file:// form of the attachment is fine", gw.tool_call_problem(tc("file://" + att), allowed, req, True) == "")
check("guard: relative name of the attachment is fine", gw.tool_call_problem(tc("attachment-1.mp4"), allowed, req, True) == "")
check("guard: /tmp file refused", gw.tool_call_problem(tc("/tmp/x.txt"), allowed, req, True) == "/tmp/x.txt")
check("guard: proxy.env refused", gw.tool_call_problem(tc("/opt/acp-ant/accounts/g1/proxy.env"), allowed, req, True) != "")
check("guard: traversal out of the request dir refused", gw.tool_call_problem(tc(req + "/../../../proxy.env"), allowed, req, True) != "")
check("guard: relative path out of the request dir refused", gw.tool_call_problem(tc("../other/secret.txt"), allowed, req, True) != "")
check("guard: directory of the request refused", gw.tool_call_problem(tc(req), allowed, req, True) != "")
check("guard: another tool refused", gw.tool_call_problem(tc(att, name="run_command", kind="execute"), allowed, req, True) == "tool run_command")
check("guard: first tool call without a path refused", gw.tool_call_problem(tc(None), allowed, req, True) != "")
check("guard: status-only update without a path is fine", gw.tool_call_problem({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"},
                                                                          allowed, req, False) == "")
mixed = tc(att)
mixed["locations"].append({"path": "/etc/passwd"})
check("guard: one bad path among good ones refused", gw.tool_call_problem(mixed, allowed, req, True) == "/etc/passwd")

t = gw.Turn(allowed, req)
t.on_update(tc(att))
t.on_update({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed", "content": [{"type": "content", "content": {"type": "text", "text": "FILE BYTES"}}]})
t.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "A red ball."}})
items = []
while not t.queue.empty():
    items.append(t.queue.get_nowait())
check("tool calls and their results never reach the client", items == [("text", "A red ball.")], items)
t = gw.Turn(allowed, req)
t.on_permission({"sessionId": "s", "toolCall": tc("/home/ubuntu/x")})
check("a permission request is a violation in video mode", t.queue.get_nowait() == ("violation", "/home/ubuntu/x"))
t = gw.Turn()
t.on_update(tc("/tmp/x"))
check("the normal bridge agent is unaffected by the guard", t.queue.empty())

# ---- run_on with a fake agy: files, cleanup, violation -------------------------------------------------------------
home = os.path.join(os.environ["AGY_ROOT"], "accounts", "g1")
os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
gw.prepare_media_home(home)


class FakeAcp:
    """Plays back agy 1.5.3: read_file on the resource link (or on `evil_path`), then answer text."""

    def __init__(self, evil_path=""):
        self.turns, self.sessions, self.cwd, self.evil = {}, 0, "", evil_path
        self.seen = {}
        self.acc = acc

    async def ensure(self):
        gw.prepare_media_home(home)

    async def maybe_recycle(self):
        pass

    async def notify(self, method, params):
        self.seen.setdefault("notify", []).append(method)

    async def request(self, method, params, timeout):
        self.seen.setdefault(method, []).append(params)
        if method == "session/new":
            sid = "sess-%d-0000" % (len(self.seen[method]))
            base = os.path.join(home, ".gemini", "antigravity-cli")
            os.makedirs(os.path.join(base, "brain", sid, "media"), exist_ok=True)
            os.makedirs(os.path.join(base, "conversations"), exist_ok=True)
            open(os.path.join(base, "conversations", sid + ".db"), "wb").write(b"copy")
            open(os.path.join(base, "brain", sid, "media", "step_2_media_0.mp4"), "wb").write(b"copy")
            return {"sessionId": sid, "configOptions": [{"id": "model", "currentValue": "gemini-4-argon",
                                                          "options": [{"value": "gemini-4-argon"}, {"value": "claude-sonnet-5-5"}]}]}
        if method == "session/prompt":
            turn = self.turns[params["sessionId"]]
            link = next(b for b in params["prompt"] if b["type"] == "resource_link")
            path = link["uri"][len("file://"):]
            self.seen["file_on_disk"] = (os.path.exists(path), open(path, "rb").read() == MP4_RAW, oct(os.stat(path).st_mode & 0o777))
            await asyncio.sleep(0.01)
            turn.on_update(tc(self.evil or path))
            await asyncio.sleep(0.01)
            turn.on_update(dict(tc(self.evil or path, sess="tool_call_update"), status="completed"))
            turn.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "HTTPS_PROXY=secret" if self.evil else "A cat jumps."}})
            await asyncio.sleep(0.01)
            return {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 3}}
        return {}


def run(acc, blocks):
    async def go():
        out = []
        try:
            async for item in gw.run_on(acc, "gemini-4-argon", None, "", blocks, gw.time.time() + 30):
                out.append(item)
        except gw.AcpError as e:
            return out, e
        return out, None
    return asyncio.run(go())


acc = gw.Account(name="g1", home=home)
acc.acp = object()          # the normal process must not be used for a request with files
acc.media = FakeAcp()
_s, msgs, _t, _c = gw.from_openai(oa({"type": "text", "text": "what happens?"}, {"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}))
gw.prepare_media(msgs, "gemini-4-argon")
_sys, blocks = gw.to_acp("", msgs, [], "auto")
out, err = run(acc, blocks)
fake = acc.media
link = next(b for b in fake.seen["session/prompt"][0]["prompt"] if b["type"] == "resource_link")
req_dir = fake.seen["session/new"][0]["cwd"]
check("run_on: answer comes through, no error", err is None and ("text", "A cat jumps.") in out, (out, err))
check("run_on: no media bytes sent to agy, only a resource link",
      all(b["type"] in ("text", "resource_link") for b in fake.seen["session/prompt"][0]["prompt"]) and link["mimeType"] == "video/mp4", link)
check("run_on: file written in the request dir, mode 600, same bytes", fake.seen["file_on_disk"][:2] == (True, True)
      and (os.name == "nt" or fake.seen["file_on_disk"][2] == "0o600"), fake.seen["file_on_disk"])
check("run_on: session cwd is a random request dir under media/w", os.path.dirname(req_dir) == os.path.join(gw.media_home(home), "w")
      and len(os.path.basename(req_dir)) == 32 and link["uri"] == "file://" + os.path.join(req_dir, "attachment-1.mp4"), (req_dir, link))
check("run_on: request dir deleted afterwards", not os.path.exists(req_dir), req_dir)
base = os.path.join(home, ".gemini", "antigravity-cli")
check("run_on: agy's own copies of the session deleted", not os.path.exists(os.path.join(base, "conversations", "sess-1-0000.db"))
      and not os.path.exists(os.path.join(base, "brain", "sess-1-0000")))

acc.media = FakeAcp(evil_path=os.path.join(home, "proxy.env"))
out, err = run(acc, blocks)
check("run_on: reading another path stops the request (BLOCKED)", err is not None and err.kind == "BLOCKED" and "not attached" in str(err), err)
check("run_on: the error does not tell the client the server path", "proxy.env" not in str(err) and home not in str(err), err)
check("run_on: nothing the model wrote after that reaches the client", not any("secret" in t for _k, t in out), out)
check("run_on: the session was cancelled", "session/cancel" in acc.media.seen.get("notify", []), acc.media.seen.get("notify"))


def list_request_dirs(path):
    """media/w is mode 0o300 (write+search, not listable) on purpose; open it up only for the test's own check."""
    mode = os.stat(path).st_mode & 0o777
    os.chmod(path, 0o700)
    try:
        return os.listdir(path)
    finally:
        os.chmod(path, mode)


W_DIR = os.path.join(gw.media_home(home), "w")
check("run_on: request dir deleted after a stopped request too", list_request_dirs(W_DIR) == [], list_request_dirs(W_DIR))
check("media HOME holds only the four fixed dirs", sorted(os.listdir(gw.media_home(home))) == ["empty", "tmp", "w"], os.listdir(gw.media_home(home)))

# ---- HTTP: Gemini rule and limits answered with 400 before any account is used ---------------------------------------
calls = []


async def fake_generate(model_name, system, blocks, effort_override=None):
    calls.append(blocks)
    yield "text", "ok", "fake"


async def _asgi_post(path, body, headers):
    raw = json.dumps(body).encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())] + [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": hdrs, "client": ("127.0.0.1", 1), "server": ("test", 80)}
    state = {"sent": False, "status": None, "body": []}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)

    async def send(msg):
        if msg["type"] == "http.response.start":
            state["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            state["body"].append(msg.get("body", b""))
    await gw.app(scope, receive, send)
    return state["status"], json.loads(b"".join(state["body"]) or b"null")


gw.generate = fake_generate
H = {"Authorization": "Bearer test-key"}
st, j = asyncio.run(_asgi_post("/v1/chat/completions", oa({"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}, model="claude-sonnet-5-5"), H))
check("http chat: video for a non-Gemini model -> 400", st == 400 and j["error"]["code"] == "unsupported_content" and "Gemini" in j["error"]["message"], (st, j))
st, j = asyncio.run(_asgi_post("/v1/messages", dict(an({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": PDF}}),
                                                     model="claude-sonnet-5-5"), H))
check("http messages: PDF for a non-Gemini model -> Anthropic 400", st == 400 and j["type"] == "error", (st, j))
check("refused requests never reached the model", calls == [], len(calls))
st, j = asyncio.run(_asgi_post("/v1/chat/completions", oa({"type": "text", "text": "describe"}, {"type": "file", "file": {"file_data": uri("video/mp4", MP4)}}), H))
check("http chat: video for gemini-4-argon goes to the model as a media block", st == 200 and j["choices"][0]["message"]["content"] == "ok"
      and any(b["type"] == "media" and b["data"] == MP4_RAW for b in calls[-1]), (st, j))
st, j = asyncio.run(_asgi_post("/v1/responses", rs({"type": "input_audio", "input_audio": {"data": WAV, "format": "wav"}}), H))
check("http responses: audio for gemini-4-argon accepted", st == 200 and any(b["type"] == "media" for b in calls[-1]), (st, j))

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
