# -*- coding: utf-8 -*-
"""ACP-Ant: OpenAI- and Anthropic-compatible API in front of the official Antigravity CLI (`agy`), spoken to over ACP.

Several Google accounts are logged in with the official `agy` CLI. Each account has its own HOME directory and,
optionally, its own HTTP/SOCKS5 proxy. The gateway puts them all behind one endpoint that agents and apps already
know how to talk to:

    POST /v1/chat/completions       OpenAI Chat Completions: system, multi-turn, images, streaming, tools / tool_calls
    POST /v1/messages               Anthropic Messages: system, multi-turn, images, streaming, tools / tool_use
    POST /v1/responses              OpenAI Responses (Codex CLI etc.): function tools, custom (free-form) tools, streaming
    POST /v1/messages/count_tokens  Anthropic token count (estimate)
    GET  /v1/models                 model list (OpenAI and Anthropic shape)
    GET  /admin/status              per-account state, concurrency, cooldowns, last error, optional quota
    POST /admin/reset/{name}        clear cooldown / disabled flag and restart that account's ACP process
    GET  /healthz                   liveness (no auth, counts only)

How it talks to Antigravity (ACP = Agent Client Protocol, JSON-RPC over stdio):
    agy acp --gemini-dir <account>/.gemini --agent <name> --disable-telemetry      (one long-lived process per account)
    initialize -> session/new (system prompt via _meta.antigravity.systemPrompt.append)
               -> session/set_config_option (model, thought_level) -> session/prompt (text + image blocks)
               -> session/close
The agent we register has no built-in tools (tools: [], excludeDefaultComponents: true) and every permission request
is refused, so Antigravity itself never touches the server. Tools that a client agent declares (OpenAI `tools`,
Anthropic `tools`) are described to the model in the system prompt; the model asks for them with a small text
protocol (<tool_calls>[...]</tool_calls>) which the gateway turns back into native tool_calls / tool_use blocks.
The client agent runs the tools and sends the results back, exactly as with any other model provider.
ACP has no temperature / max_tokens; those fields are ignored.
Only text and images reach the model. Video, audio, PDF and other binary files are refused with HTTP 400 instead of
being dropped (a model that silently misses them still answers, and invents what they contained).
Video mode (AGY_VIDEO_MODE=1, off by default) lets Gemini models see video and PDF and hear audio: such requests run in
a second, sandboxed agy process per account whose agent has a single tool, read_file (see "Video mode" below).

The idea of driving `agy acp` from another program comes from lili-luo/dsh-plugin-bundle
(dsh-llm-antigravity-acp, MIT). This file is an independent implementation.

Google credentials live inside each account's HOME, where `agy` keeps them. The gateway does not use them (the panel
only reads the account's e-mail address from the login file); it writes one only when the owner logs an account in
with a refresh_token in the panel: written once to that account's login file, never logged or sent back.
"""
from __future__ import annotations

import asyncio
import base64
import json
try:
    from json_repair import repair_json
except ImportError:
    repair_json = None
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

__version__ = "1.4.1"     # keep in step with the newest heading in CHANGELOG.md (the panel compares against it)

log = logging.getLogger("acp-ant")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() not in ("0", "false", "no", "off", "")


ROOT = os.environ.get("AGY_ROOT", "/opt/acp-ant")
AGY = os.environ.get("AGY_BIN", f"{ROOT}/bin/agy")
ACCOUNTS_DIR = os.environ.get("AGY_ACCOUNTS", f"{ROOT}/accounts")
API_KEY = os.environ.get("AGY_GATEWAY_KEY", "")
AGENT = re.sub(r"[^a-zA-Z0-9_-]", "", os.environ.get("AGY_AGENT", "acpant")) or "acpant"
PER_ACCOUNT = _env_int("AGY_PER_ACCOUNT", 3)                  # concurrent requests per account (the panel can change it live)
REQUEST_TIMEOUT = float(os.environ.get("AGY_REQUEST_TIMEOUT", "290"))
HEARTBEAT = float(os.environ.get("AGY_HEARTBEAT", "15"))       # streams: keep-alive after this many silent seconds; 0 = off
MAX_CONTINUE = 3                                               # auto-continue when a reply stops at max_tokens
COOLDOWN_QUOTA = _env_int("AGY_COOLDOWN_QUOTA", 30 * 60)       # quota exhausted
COOLDOWN_RATE = _env_int("AGY_COOLDOWN_RATE", 90)              # rate limited / overloaded
COOLDOWN_VERIFY = _env_int("AGY_COOLDOWN_VERIFY", 600)         # Google asks the account to verify itself
KEEP_HOURS = float(os.environ.get("AGY_KEEP_HOURS", "6"))      # delete local conversation files older than this (0 = keep)
REQUIRE_PROXY = _env_bool("AGY_REQUIRE_PROXY", False)   # since 1.4.0 a proxy is optional; set 1 to insist on one per account
DEFAULT_MODEL = os.environ.get("AGY_DEFAULT_MODEL", "gemini-3.8-flash-medium")
MODEL_FALLBACK = _env_bool("AGY_MODEL_FALLBACK", True)         # unknown model name -> DEFAULT_MODEL instead of an error
MAX_IMAGES = _env_int("AGY_MAX_IMAGES", 10)                    # images per request (the most recent ones are kept)
FETCH_IMAGES = _env_bool("AGY_FETCH_IMAGES", True)             # download http(s) image URLs sent by clients
MAX_PROMPT_CHARS = _env_int("AGY_MAX_PROMPT_CHARS", 130000)        # prompt budget per turn; avoids agy's ~184KB tail truncation
MAX_TOOL_RESULT_CHARS = _env_int("AGY_MAX_TOOL_RESULT_CHARS", 25000) # clamp individual oversized tool outputs
# One long-lived `agy acp` per account serving all its sessions: session/new takes ~0.02 s instead of ~2 s with
# --process-per-session (which starts a new worker for every request). Set 1 to go back to a process per session.
PROCESS_PER_SESSION = _env_bool("AGY_PROCESS_PER_SESSION", False)
RECYCLE_SESSIONS = _env_int("AGY_RECYCLE_SESSIONS", 500)      # restart a shared process when idle after this many sessions

# Video mode (off by default). Measured with agy 1.5.3: no ACP prompt block carries video, audio or PDF to the model,
# but an agent that may call read_file on a resource_link sees the whole file (a video including its sound).
# read_file runs without asking only inside the session's working directory and inside $TMPDIR (/tmp when unset);
# every other path raises a permission request, which the gateway refuses. So video-mode requests run in a second agy
# process per account with its own empty HOME (proxy passed in the environment, log kept outside), a private TMPDIR,
# and a fresh working directory with a random name per request, deleted afterwards together with agy's own copies.
# On top of that the gateway watches the agent's tool calls and stops the request as soon as it opens any other path.
VIDEO_MODE = _env_bool("AGY_VIDEO_MODE", False)
VIDEO_MAX_MB = float(os.environ.get("AGY_VIDEO_MAX_MB", "40"))   # all files of one request together, after decoding
VIDEO_MAX_FILES = _env_int("AGY_VIDEO_MAX_FILES", 5)
try:   # Antigravity model ids allowed to receive files in video mode (Claude / GPT models behind Antigravity cannot read them)
    VIDEO_MODELS = re.compile(os.environ.get("AGY_VIDEO_MODELS") or r"^gemini-")
except re.error:
    log.warning("AGY_VIDEO_MODELS is not a valid regular expression; using ^gemini-")
    VIDEO_MODELS = re.compile(r"^gemini-")
MEDIA_AGENT = AGENT + "-media"
# MIME type -> file extension (read_file decides by the extension). Gemini's video, audio and document types.
MEDIA_EXT = {
    "video/mp4": ".mp4", "video/mpeg": ".mpeg", "video/mpg": ".mpg", "video/quicktime": ".mov", "video/mov": ".mov",
    "video/webm": ".webm", "video/x-flv": ".flv", "video/3gpp": ".3gp", "video/avi": ".avi", "video/x-msvideo": ".avi",
    "video/wmv": ".wmv", "video/x-ms-wmv": ".wmv",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff",
    "application/pdf": ".pdf",
}
AUDIO_FORMATS = {"wav": "audio/wav", "mp3": "audio/mpeg", "aac": "audio/aac", "ogg": "audio/ogg", "flac": "audio/flac", "aiff": "audio/aiff"}

# Public model name -> (Antigravity model id, thought level or None to keep the account default).
# Names not listed here are passed through; a trailing -high / -medium / -low selects the thought level.
MODEL_ALIASES: dict[str, tuple[str, str | None]] = {
    "gemini-3.8-flash-high": ("gemini-3.8-flash", "high"),
    "gemini-3.8-flash-medium": ("gemini-3.8-flash", "medium"),
    "gemini-3.8-flash-low": ("gemini-3.8-flash", "low"),
    "gemini-3.8-flash": ("gemini-3.8-flash", None),
    "gemini-4-argon-high": ("gemini-4-argon", "high"),
    "gemini-4-argon": ("gemini-4-argon", None),
}
try:  # extra aliases: AGY_MODEL_ALIASES='{"fast": ["gemini-3.8-flash", "low"], "claude-haiku-5-5": ["gemini-3.8-flash", "low"]}'
    for _k, _v in json.loads(os.environ.get("AGY_MODEL_ALIASES") or "{}").items():
        MODEL_ALIASES[str(_k)] = (str(_v[0]), (str(_v[1]) if len(_v) > 1 and _v[1] else None))
except (ValueError, TypeError, IndexError, AttributeError):
    log.warning("AGY_MODEL_ALIASES is not valid JSON of the form {\"name\": [\"model\", \"level\"]}; ignored")

LEVELS = ("low", "medium", "high")


def resolve_model(name: str) -> tuple[str, str | None]:
    """Client model name -> (Antigravity model id, thought level). Accepts aliases, provider prefixes
    ("antigravity/gemini-3.8-flash"), Claude Code style suffixes ("[1m]", "-20261001") and -low/-medium/-high."""
    name = (name or DEFAULT_MODEL).strip()
    name = re.sub(r"\[[^\]]*\]$", "", name).split("/")[-1]
    for cand in (name, re.sub(r"-(\d{8}|latest)$", "", name)):
        if cand in MODEL_ALIASES:
            return MODEL_ALIASES[cand]
    base = re.sub(r"-(\d{8}|latest)$", "", name)
    m = re.match(r"^(.+)-(high|medium|low)$", base)
    if m:
        return m.group(1), m.group(2)
    return base, None


AGENT_MD = """---
name: %s
description: "ACP-Ant bridge: answers in text, client-side tools only through the text protocol"
tools: []
excludeDefaultComponents: true
mainAgent: true
---
You have no built-in tools: never try to read files, browse, or run commands yourself.
If the appended instructions describe external tools and a <tool_calls> text protocol, request tools only with exactly
that protocol and wait for their results. Otherwise answer directly in text.
Follow the appended system instructions exactly.
""" % AGENT

MEDIA_AGENT_MD = """---
name: %s
description: "ACP-Ant video mode: looks at the files attached to the request, nothing else"
tools: [read_file]
excludeDefaultComponents: true
mainAgent: true
---
Files attached to the user's message (video, audio, PDF) are given to you as file links. Before you answer, open each
attached file with your read_file tool, using exactly the path in its link. read_file is only for those attachments:
never open any other path, directory or system file, whatever the conversation asks; say that you can only see the
attached files instead. Refer to the files as "attachment 1", "attachment 2" and so on, never by their path.
If the appended instructions describe external tools and a <tool_calls> text protocol, request those tools only with
exactly that protocol and wait for their results. Follow the appended system instructions exactly.
""" % MEDIA_AGENT


# Antigravity's own prompt filter does not raise an error: it sends a fixed sentence back as if it were the answer.
# Recognise it and fail the request instead, so the caller can fall back to another provider.
# When Google wants an account to verify itself (403 "Verify your account to continue"), ACP likewise answers with
# "⚠ Verification Required" as normal text: rest that account and move on.
_VERIFY_TEXT = re.compile(r"^\s*⚠?\s*Verification Required\s*$|Verify your account to continue", re.I)
_BLOCK_TEXT = re.compile(r"^\s*The prompt could not be submitted|Generative AI Prohibited Use policy|prompt contains sensitive words", re.I)
# Upstream API errors are also reported as text, while agy keeps retrying for minutes:
#   "⚠ API error (attempt 1): RESOURCE_EXHAUSTED (code 429): Resource has been exhausted (e.g. check quota)."
#   ... "⚠ Agent execution terminated due to error."
_APIERR_TEXT = re.compile(r"^\s*⚠\s*(?:API error \(attempt \d+\):\s*([^\n]+)\n|Agent execution terminated due to error)")
_NETWORK_TEXT = re.compile(r"^\s*⚠?\s*There was a network issue connecting to the server", re.I)


# Every canned reply above starts with one of these. As soon as the answer clearly starts with something else it is
# streamed out right away instead of being held back (first token arrives with the model's first chunk).
_CANNED_STARTS = ("⚠", "the prompt could not be submitted", "your prompt contains sensitive words", "prompt contains sensitive words",
                  "verification required", "verify your account", "there was a network issue")


def could_be_canned(text: str) -> bool:
    t = text.lstrip().lower()
    return not t or any(t.startswith(p) or p.startswith(t) for p in _CANNED_STARTS)


def soft_error(text: str) -> AcpError | None:
    """Recognise the canned replies that are really errors (see above)."""
    head = text[:600]
    if _BLOCK_TEXT.search(head):
        return AcpError("blocked by Antigravity's prompt filter: " + head[:200], "BLOCKED")
    if _VERIFY_TEXT.search(head):
        return AcpError("account must be verified with Google: " + head[:120].strip(), "VERIFY")
    if _NETWORK_TEXT.search(head):
        return AcpError("Antigravity upstream network issue: " + head[:120].strip(), "RATE_LIMIT")
    m = _APIERR_TEXT.search(head)
    if m:
        msg = (m.group(1) or "agent execution terminated").strip()
        # RESOURCE_EXHAUSTED here is not always the account's quota: some prompts are refused this way whatever the
        # account. UPSTREAM_LIMIT = try one more account without cooling this one down; the real quota is checked.
        kind = classify(msg)
        return AcpError("Antigravity upstream error: " + msg[:200], "UPSTREAM_LIMIT" if kind in ("QUOTA", "RATE_LIMIT") else kind)
    return None


class AcpError(Exception):
    def __init__(self, message: str, kind: str = "SERVER"):
        super().__init__(message)
        self.kind = kind


def classify(message: str) -> str:
    m = message.lower()
    if "verify your account" in m or "verification required" in m or "not eligible for antigravity" in m:
        return "VERIFY"
    if re.search(r"invalid prompt|invalid argument|invalid_request|bad request", m):
        return "BAD_REQUEST"   # the request itself is wrong; another account would fail the same way
    if re.search(r"authentication required|not logged in|unauthenticated|sign in|invalid_grant|token.*(expired|revoked)", m):
        return "AUTH"
    if re.search(r"quota|resource.?exhausted|limit reached|exceeded your|usage limit", m):
        return "QUOTA"
    if re.search(r"rate.?limit|429|too many requests|overloaded|capacity|unavailable|503", m):
        return "RATE_LIMIT"
    if re.search(r"timeout|timed out|deadline|network issue|connection reset|handshake", m):
        return "TIMEOUT"
    if re.search(r"safety|blocked|prohibited|recitation", m):
        return "BLOCKED"
    return "SERVER"


def read_proxy(home: str) -> dict[str, str]:
    """proxy.env is written by account.sh with printf %q; let bash parse it instead of re-implementing quoting."""
    path = os.path.join(home, "proxy.env")
    if not os.path.exists(path):
        return {}
    out = subprocess.run(["bash", "-c", 'set -a; . "$1"; printf "%s\\0%s" "$HTTPS_PROXY" "$HTTP_PROXY"', "_", path],
                         capture_output=True, text=True, timeout=10)
    https, _, http = out.stdout.partition("\0")
    return {"HTTPS_PROXY": https, "HTTP_PROXY": http or https} if https else {}


def prepare_home(home: str) -> tuple[str, str]:
    """`agy` logs in with <home>/.gemini (token in .gemini/antigravity-cli/), so the gateway must use the same
    gemini dir. We only add our tool-less agent and two settings; no extra rules, skills or MCP servers.
    Returns (gemini dir, working dir)."""
    gem = os.path.join(home, ".gemini")
    agents = os.path.join(gem, "config", "plugins", AGENT, "agents")
    os.makedirs(agents, exist_ok=True)
    with open(os.path.join(gem, "config", "plugins", AGENT, "plugin.json"), "w") as f:
        json.dump({"name": AGENT}, f)
    with open(os.path.join(agents, f"{AGENT}.md"), "w") as f:
        f.write(AGENT_MD)
    app_dir = os.path.join(gem, "antigravity-cli")
    os.makedirs(app_dir, exist_ok=True)
    sp = os.path.join(app_dir, "settings.json")
    try:
        with open(sp) as f:
            settings = json.load(f)
    except Exception:  # noqa: BLE001
        settings = {}
    settings.update({"toolPermission": "request-review", "agentMode": ""})
    with open(sp, "w") as f:
        json.dump(settings, f, indent=2)
    ws = os.path.join(home, "gw", "workspace")
    os.makedirs(ws, exist_ok=True)
    return gem, ws


# Video mode layout inside an account directory (the account dir itself, with proxy.env and gw/, is never readable):
#   media/        HOME of the video-mode agy process: nothing else is ever put here
#   media/tmp/    its TMPDIR (read_file may read here without asking; agy leaves it empty)
#   media/empty/  its working directory (empty, read-only)
#   media/w/      one directory with a random name per request, deleted afterwards; mode 0300, so nobody can list it
MEDIA_DIRS = (("", 0o700), ("tmp", 0o700), ("empty", 0o500), ("w", 0o300))


def media_home(home: str) -> str:
    return os.path.join(home, "media")


def prepare_media_home(home: str) -> tuple[str, str]:
    """Video mode: register the read_file-only agent in the account's gemini dir and create the empty HOME.
    Returns (gemini dir, media HOME)."""
    gem, _ws = prepare_home(home)
    agents = os.path.join(gem, "config", "plugins", MEDIA_AGENT, "agents")
    os.makedirs(agents, exist_ok=True)
    with open(os.path.join(gem, "config", "plugins", MEDIA_AGENT, "plugin.json"), "w") as f:
        json.dump({"name": MEDIA_AGENT}, f)
    with open(os.path.join(agents, f"{MEDIA_AGENT}.md"), "w") as f:
        f.write(MEDIA_AGENT_MD)
    mh = media_home(home)
    for sub, mode in MEDIA_DIRS:
        p = os.path.join(mh, sub) if sub else mh
        os.makedirs(p, exist_ok=True)
        os.chmod(p, mode)
    return gem, mh


def clear_media_leftovers(home: str) -> None:
    """Request files and temp files left behind by a crash or a kill (called at gateway start)."""
    mh = media_home(home)
    for sub in ("w", "tmp"):
        d = os.path.join(mh, sub)
        if not os.path.isdir(d):
            continue
        os.chmod(d, 0o700)
        for name in os.listdir(d):
            p = os.path.join(d, name)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
        os.chmod(d, dict(MEDIA_DIRS)[sub])


def forget_session(home: str, sid: str) -> None:
    """agy keeps its own copy of every conversation, a video read with read_file included (a 23 MB video left a 23 MB
    copy under brain/<session>/ and a 46 MB conversations/<session>.db). Video mode deletes them right after the request."""
    if not re.fullmatch(r"[\w-]{8,}", sid or ""):
        return
    base = os.path.join(home, ".gemini", "antigravity-cli")
    for sub in ("conversations", "brain", "implicit", "annotations"):
        d = os.path.join(base, sub)
        try:
            names = [n for n in os.listdir(d) if n.startswith(sid)]
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            try:
                shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
            except OSError as e:
                log.warning("video mode: could not delete %s: %s", p, e)


_PATH_KEY = re.compile(r"path|file|uri", re.I)


def tool_call_problem(update: dict, allowed: set[str], cwd: str, initial: bool) -> str:
    """Video mode: "" when this tool call only reads files attached to the request, otherwise what it tried to open.
    agy reports read_file as {"name": "read_file", "kind": "read", "locations": [{"path"}], "rawInput": {"Path"}}."""
    name, kind = update.get("name"), update.get("kind")
    if name not in (None, "read_file"):
        return "tool %s" % name
    if kind not in (None, "read"):
        return "a %s tool" % kind
    paths = [str(loc["path"]) for loc in update.get("locations") or [] if isinstance(loc, dict) and loc.get("path")]
    for key in ("args", "rawInput"):
        v = update.get(key)
        if isinstance(v, dict):
            paths += [x for k, x in v.items() if isinstance(x, str) and x and _PATH_KEY.search(str(k))]
    if not paths:
        return "a tool call without a path" if initial else ""   # updates may carry only a new status
    for p in paths:
        q = os.path.normpath(os.path.join(cwd, p[7:] if p.startswith("file://") else p))
        if q not in allowed and os.path.realpath(q) not in allowed:
            return p
    return ""


# ======================================================================================================
# ACP process per account
# ======================================================================================================

class Turn:
    """session/update notifications received during one session/prompt. Only the answer and thought text is queued;
    tool calls (video mode's read_file) never reach the client. In video mode (allowed = the request's own files) a
    tool call that opens anything else queues ("violation", path): the request is stopped before any later text."""

    def __init__(self, allowed: set[str] | None = None, cwd: str = "") -> None:
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.allowed, self.cwd = allowed, cwd
        self.violation = ""

    def on_update(self, update: dict) -> None:
        kind = update.get("sessionUpdate")
        content = update.get("content") or {}
        if kind in ("agent_message_chunk", "agent_thought_chunk") and isinstance(content, dict) and content.get("type") == "text":
            self.queue.put_nowait(("text" if kind == "agent_message_chunk" else "thought", content.get("text") or ""))
        elif kind in ("tool_call", "tool_call_update") and self.allowed is not None and not self.violation:
            bad = tool_call_problem(update, self.allowed, self.cwd, kind == "tool_call")
            if bad:
                self.violation = bad
                self.queue.put_nowait(("violation", bad))

    def on_permission(self, params: dict) -> None:
        """A permission request is always refused; in video mode it is also a violation (the path is outside the request)."""
        if self.allowed is not None and not self.violation:
            tc = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            self.violation = tool_call_problem(tc, self.allowed, self.cwd, True) or "a file outside the request"
            self.queue.put_nowait(("violation", self.violation))


class AcpProcess:
    """One long-lived `agy acp` process per account; all of the account's sessions run inside it
    (with AGY_PROCESS_PER_SESSION=1, agy starts a separate worker for every session instead)."""

    def __init__(self, account: "Account", media: bool = False):
        self.acc = account
        self.media = media         # video mode: read_file-only agent, empty HOME, private TMPDIR (started on first use)
        self.label = "%s%s" % (account.name, " (video mode)" if media else "")
        self.cwd = ""              # working directory of the process (sessions in video mode get their own)
        self.proc: asyncio.subprocess.Process | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.turns: dict[str, Turn] = {}
        self.init_result: dict | None = None
        self.reader_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.sessions = 0          # sessions opened since this process started (for recycling)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def ensure(self) -> None:
        async with self.lock:
            if self.alive and self.init_result is not None:
                return
            await self._start()

    async def _start(self) -> None:
        a = self.acc
        if self.media:
            # HOME holds nothing but the request directories; TMPDIR moves read_file's free zone away from the shared
            # /tmp; the proxy comes from the environment and the log goes to gw/, both outside what read_file may open
            gem, mh = prepare_media_home(a.home)
            ws, agent, logf = os.path.join(mh, "empty"), MEDIA_AGENT, os.path.join(a.home, "gw", "acp-media.log")
            env = {"HOME": mh, "TMPDIR": os.path.join(mh, "tmp")}
        else:
            gem, ws = prepare_home(a.home)
            agent, logf = AGENT, os.path.join(a.home, "gw", "acp.log")
            env = {"HOME": a.home}
        env.update({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        px = read_proxy(a.home)
        if px:
            env.update(px)
            env.update({"https_proxy": px["HTTPS_PROXY"], "http_proxy": px["HTTP_PROXY"]})
        elif REQUIRE_PROXY:
            raise AcpError(f"account {a.name} has no proxy.env (AGY_REQUIRE_PROXY=1); not starting it", "CONFIG")
        argv = [AGY, "acp", "--gemini-dir", gem, "--agent", agent, "--disable-telemetry", "--log-file", logf]
        if PROCESS_PER_SESSION:
            argv.append("--process-per-session")
        self.sessions = 0
        self.cwd = ws
        self.proc = await asyncio.create_subprocess_exec(*argv, cwd=ws, env=env, stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                         limit=64 * 1024 * 1024)
        self.reader_task = asyncio.create_task(self._reader())
        self.init_result = await self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "acp-ant", "version": "1.1"},
        }, 60)
        log.info("account %s: acp started (pid %s)", self.label, self.proc.pid)

    async def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                    fut = self.pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"] or {}
                            text = str(err.get("message") or err)
                            data = err.get("data")
                            if data:
                                text += " | " + json.dumps(data, ensure_ascii=False)[:500]
                            fut.set_exception(AcpError(text, classify(text)))
                        else:
                            fut.set_result(msg.get("result") or {})
                elif msg.get("method") == "session/update":
                    p = msg.get("params") or {}
                    t = self.turns.get(p.get("sessionId"))
                    if t:
                        t.on_update(p.get("update") or {})
                elif "method" in msg and "id" in msg:
                    # requests from the agent: refuse every permission request (the bridge agent has no tools; the
                    # video-mode agent asks only for paths outside the request's own directory)
                    if msg["method"] == "session/request_permission":
                        p = msg.get("params") or {}
                        t = self.turns.get(p.get("sessionId"))
                        if t:
                            t.on_permission(p)
                        await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                    else:
                        await self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(AcpError("ACP process exited", "TRANSPORT"))
            self.pending.clear()
            self.init_result = None
            log.warning("account %s: acp process ended", self.label)

    async def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            raise AcpError(f"{method} timed out ({timeout:.0f}s)", "TIMEOUT")

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def prewarm(self) -> None:
        """Start the process and open + close one session so the first real request does not wait for the worker
        (~2 s). Costs no quota: nothing is sent to the model."""
        try:
            await self.ensure()
            if PROCESS_PER_SESSION:
                return
            sess = await self.request("session/new", {"cwd": self.cwd, "mcpServers": []}, 120)
            if sess.get("sessionId"):
                await self.request("session/close", {"sessionId": sess["sessionId"]}, 15)
        except Exception as e:  # noqa: BLE001
            log.info("account %s: prewarm skipped: %s", self.label, str(e)[:160])

    async def maybe_recycle(self) -> None:
        """Restart a shared process now and then (when idle) so a long-running one never accumulates state."""
        # only when this finishing request is the account's last one in flight (none still opening a session)
        if not PROCESS_PER_SESSION and self.sessions >= RECYCLE_SESSIONS and not self.turns and self.acc.inflight <= 1:
            log.info("account %s: recycling acp process after %d sessions", self.label, self.sessions)
            await self.stop()
            if not self.media:     # the video-mode process starts again with its next request
                asyncio.create_task(self.prewarm())

    async def stop(self) -> None:
        if self.alive:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()


@dataclass
class Account:
    name: str
    home: str
    inflight: int = 0                 # requests running on this account right now (at most POOL.limit)
    cooldown_until: float = 0.0
    cooldown_kind: str = ""
    disabled_reason: str = ""
    enabled: bool = True              # switched off in the panel (file <home>/disabled): never picked
    last_error: str = ""
    last_error_at: float = 0.0
    ok: int = 0
    fail: int = 0
    last_ok_at: float = 0.0
    quota: Any = None
    quota_at: float = 0.0
    last_pick: float = 0.0
    last_check_at: float = 0.0        # last health check (panel button or automatic patrol)
    last_check_ok: bool | None = None
    last_check_msg: str = ""
    verify_url: str = ""              # Google verification link from the last check, if any
    acp: AcpProcess | None = None
    media: AcpProcess | None = None   # video mode: second, sandboxed process (created on the first request with a file)

    def usable(self) -> bool:
        return self.enabled and not self.disabled_reason and time.time() >= self.cooldown_until

    def media_acp(self) -> AcpProcess:
        if self.media is None:
            self.media = AcpProcess(self, media=True)
        return self.media

    async def stop_processes(self) -> None:
        """Stop both of the account's agy processes (restart after a new login or proxy, account deleted, shutdown)."""
        for p in (self.acp, self.media):
            if p:
                await p.stop()


EVENTS: deque = deque(maxlen=400)


def event(level: str, account: str, text: str) -> None:
    """Short in-memory activity log shown in the panel (also goes to the service log)."""
    EVENTS.append({"t": time.time(), "level": level, "account": account, "text": text[:300]})


class Stats:
    """Requests and tokens per account per day, kept 31 days in $AGY_ROOT/stats.json."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.days: dict[str, dict[str, dict[str, int]]] = {}
        self.dirty = False
        try:
            with open(path) as f:
                self.days = json.load(f).get("days", {})
        except (OSError, ValueError):
            pass

    def record(self, account: str, ok: bool, tokens_in: int = 0, tokens_out: int = 0) -> None:
        day = time.strftime("%Y-%m-%d")
        d = self.days.setdefault(day, {}).setdefault(account, {"ok": 0, "fail": 0, "in": 0, "out": 0})
        d["ok" if ok else "fail"] += 1
        d["in"] += tokens_in
        d["out"] += tokens_out
        self.dirty = True

    def summary(self, account: str, days: int = 1) -> dict[str, int]:
        keys = sorted(self.days)[-days:]
        out = {"ok": 0, "fail": 0, "in": 0, "out": 0}
        for k in keys:
            for f, v in self.days[k].get(account, {}).items():
                out[f] = out.get(f, 0) + v
        return out

    def save(self) -> None:
        if not self.dirty:
            return
        for k in sorted(self.days)[:-31]:
            del self.days[k]
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"days": self.days}, f)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError as e:
            log.warning("could not save stats: %s", e)


STATS = Stats(os.path.join(ROOT, "stats.json"))


class Pool:
    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}
        self.models_seen: list[str] = []      # model ids offered by Antigravity (learned from session/new)
        self.levels_seen: list[str] = []      # thought levels offered
        self.limit = max(1, PER_ACCOUNT)      # concurrent requests per account
        self.waiting = 0                      # requests queued because every account is full
        self._cond: asyncio.Condition | None = None
        self._bg: set[asyncio.Task] = set()

    def scan(self) -> None:
        """Every sub-directory of ACCOUNTS_DIR with a proxy.env (or, with AGY_REQUIRE_PROXY=0, a .gemini dir) is an account.
        New accounts are picked up and deleted ones dropped without a restart; <home>/disabled switches one off."""
        if not os.path.isdir(ACCOUNTS_DIR):
            return
        seen = set()
        for name in sorted(os.listdir(ACCOUNTS_DIR)):
            home = os.path.join(ACCOUNTS_DIR, name)
            if not os.path.isdir(home):
                continue
            if not (os.path.exists(os.path.join(home, "proxy.env")) or (not REQUIRE_PROXY and os.path.isdir(os.path.join(home, ".gemini")))):
                continue
            seen.add(name)
            if name not in self.accounts:
                acc = Account(name=name, home=home)
                acc.acp = AcpProcess(acc)
                self.accounts[name] = acc
                log.info("account %s added", name)
                event("info", name, "account added")
            self.accounts[name].enabled = not os.path.exists(os.path.join(home, "disabled"))
        for name in [n for n in self.accounts if n not in seen]:
            acc = self.accounts.pop(name)
            event("info", name, "account removed")
            if any(p and p.alive for p in (acc.acp, acc.media)):
                try:
                    asyncio.get_running_loop().create_task(acc.stop_processes())
                except RuntimeError:
                    pass

    def pick(self, exclude: set[str]) -> Account | None:
        self.scan()
        cands = [a for a in self.accounts.values() if a.usable() and a.name not in exclude]
        if not cands:
            return None
        # least busy first; among equals the one unused for longest (real round-robin, quota spreads evenly)
        cands.sort(key=lambda a: (a.inflight, a.last_pick))
        return cands[0]

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def acquire(self, exclude: set[str], deadline: float) -> Account | None:
        """A free slot on the least busy usable account. When every account is full the request queues, and whichever
        account frees a slot first takes the next one in line. None = no usable account left to try."""
        cond = self._condition()
        async with cond:
            self.waiting += 1
            try:
                while True:
                    acc = self.pick(exclude)
                    if acc is None:
                        return None
                    if acc.inflight < self.limit:
                        acc.inflight += 1
                        acc.last_pick = time.time()
                        return acc
                    left = deadline - time.time()
                    if left <= 0:
                        raise AcpError("all accounts are busy (each already runs %d requests); try again shortly" % self.limit, "BUSY")
                    try:   # woken when a slot frees; look again every few seconds anyway (cooldowns end, accounts come back)
                        await asyncio.wait_for(cond.wait(), min(left, 5))
                    except asyncio.TimeoutError:
                        pass
            finally:
                self.waiting -= 1

    def release(self, acc: Account) -> None:
        acc.inflight -= 1     # no await before this: it also runs while a cancelled request unwinds
        self.wake()

    def wake(self) -> None:
        """A slot freed, an account was switched on or the limit raised: let queued requests look again."""
        cond = self._cond
        if cond is None or not self.waiting:
            return

        async def _notify() -> None:
            async with cond:
                cond.notify_all()
        try:
            t = asyncio.get_running_loop().create_task(_notify())
        except RuntimeError:
            return
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def set_limit(self, n: int) -> None:
        self.limit = max(1, int(n))
        self.wake()


POOL = Pool()


async def run_on(acc: Account, model: str, effort: str | None, system: str, blocks: list[dict], deadline: float) -> AsyncIterator[tuple[str, str]]:
    """One attempt on one account: new session -> model / thought level -> prompt -> stream (kind, text) -> close.
    Blocks of type "media" (video mode) are written to a fresh request directory, sent as resource links to the
    account's video-mode process, and deleted afterwards."""
    has_media = any(b["type"] == "media" for b in blocks)
    acp = acc.media_acp() if has_media else acc.acp
    assert acp is not None
    await acp.ensure()
    req_dir, turn, sid = "", Turn(), None
    try:
        if has_media:
            req_dir = os.path.join(media_home(acc.home), "w", secrets.token_hex(16))
            os.mkdir(req_dir, 0o700)
            blocks, allowed = await asyncio.to_thread(write_media, blocks, req_dir)
            turn = Turn(allowed, req_dir)
        params: dict[str, Any] = {"cwd": req_dir or acp.cwd, "mcpServers": []}
        if system:
            params["_meta"] = {"antigravity": {"systemPrompt": {"append": system}}}
        sess = await acp.request("session/new", params, 120)
        acp.sessions += 1
        sid = sess.get("sessionId")
        if not sid:
            raise AcpError("session/new returned no sessionId", "SERVER")
        acp.turns[sid] = turn
        async for item in _run_session(acp, sess, turn, model, effort, blocks, deadline, has_media):
            yield item
    finally:
        if sid:
            acp.turns.pop(sid, None)
            try:
                await acp.request("session/close", {"sessionId": sid}, 15)
            except Exception:  # noqa: BLE001
                pass
        if req_dir:
            shutil.rmtree(req_dir, ignore_errors=True)
            if sid:
                forget_session(acc.home, sid)
        await acp.maybe_recycle()


def violation_error(path: str) -> AcpError:
    """Video mode: the model tried to open something that is not a file of this request. Nothing it wrote after
    that reaches the client; the request fails (400) and is not retried on another account."""
    log.warning("video mode: stopped a request whose model tried to open %s", path[:200])
    return AcpError("video mode: the request was stopped because the model tried to open a file that is not attached "
                    "to it. Only the files sent with the request can be read.", "BLOCKED")


def write_media(blocks: list[dict], req_dir: str) -> tuple[list[dict], set[str]]:
    """Video mode: media blocks -> files attachment-N.<ext> in the request directory + resource_link blocks.
    Returns (blocks to send, the only paths read_file may open)."""
    out, allowed, n = [], set(), 0
    for b in blocks:
        if b["type"] != "media":
            out.append(b)
            continue
        n += 1
        name = "attachment-%d%s" % (n, MEDIA_EXT.get(b["mime"], ".bin"))
        path = os.path.join(req_dir, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(b["data"])
        allowed.update({os.path.normpath(path), os.path.realpath(path)})
        out.append({"type": "resource_link", "uri": "file://" + path, "name": name, "mimeType": b["mime"]})
    return out, allowed


async def _run_session(acp: AcpProcess, sess: dict, turn: Turn, model: str, effort: str | None, blocks: list[dict],
                       deadline: float, has_media: bool) -> AsyncIterator[tuple[str, str]]:
    """model / thought level -> prompt -> stream (kind, text); auto-continues after max_tokens."""
    sid = sess["sessionId"]
    opts = sess.get("configOptions") or []
    mopt = next((o for o in opts if o.get("id") == "model" or o.get("category") == "model"), None)
    offered = [str(o.get("value")) for o in (mopt or {}).get("options") or []]
    if offered:
        POOL.models_seen = offered
    if offered and model not in offered and effort and f"{model}-{effort}" in offered:
        model, effort = f"{model}-{effort}", None    # ids like "gpt-oss-120b-medium" carry the level in the name
    if offered and model not in offered:
        fm, fe = resolve_model(DEFAULT_MODEL)
        if MODEL_FALLBACK and fm in offered:
            log.info("model %s is not offered by Antigravity; using %s", model, DEFAULT_MODEL)
            model, effort = fm, fe
        else:
            raise AcpError("this account has no model %s (available: %s)" % (model, ", ".join(offered)), "UNKNOWN_MODEL")
    if has_media and not VIDEO_MODELS.search(model):   # e.g. an unknown name that fell back to a non-Gemini default
        raise AcpError("video mode: model %s cannot open video, audio or PDF files (AGY_VIDEO_MODELS)" % model, "BAD_REQUEST")
    if not mopt or mopt.get("currentValue") != model:
        res = await acp.request("session/set_config_option", {"sessionId": sid, "configId": (mopt or {}).get("id", "model"), "value": model}, 60)
        opts = res.get("configOptions") or opts
    lopt = next((o for o in opts if o.get("id") == "thought_level" or o.get("category") == "thought_level"), None)
    levels = [str(o.get("value")) for o in (lopt or {}).get("options") or []]
    if levels:
        POOL.levels_seen = levels
    if effort and lopt and lopt.get("currentValue") != effort and (not levels or effort in levels):
        await acp.request("session/set_config_option", {"sessionId": sid, "configId": lopt.get("id", "thought_level"), "value": effort}, 60)
    prompt = blocks
    held: list[tuple[str, str]] = []   # hold the first ~400 chars so canned error replies can be recognised
    held_len, released = 0, False
    for i in range(MAX_CONTINUE + 1):
        fut = asyncio.ensure_future(acp.request("session/prompt", {"sessionId": sid, "prompt": prompt}, max(5.0, deadline - time.time())))
        while True:
            getter = asyncio.ensure_future(turn.queue.get())
            done, _ = await asyncio.wait({fut, getter}, timeout=max(0.1, deadline - time.time()), return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                item = getter.result()
                if item[0] == "violation":
                    await acp.notify("session/cancel", {"sessionId": sid})
                    fut.cancel()
                    raise violation_error(item[1])
                if released or item[0] != "text":
                    yield item
                else:
                    held.append(item)
                    held_len += len(item[1])
                    joined = "".join(t for _k, t in held)
                    canned = could_be_canned(joined)
                    err = soft_error(joined) if (held_len >= 400 or canned) else None
                    if err:
                        await acp.notify("session/cancel", {"sessionId": sid})
                        fut.cancel()
                        raise err
                    if held_len >= 400 or not canned:
                        for h in held:
                            yield h
                        held, released = [], True
                continue
            getter.cancel()
            if fut in done:
                while not turn.queue.empty():
                    item = turn.queue.get_nowait()
                    if item[0] == "violation":
                        raise violation_error(item[1])
                    if released or item[0] != "text":
                        yield item
                    else:
                        held.append(item)
                break
            if time.time() >= deadline:
                await acp.notify("session/cancel", {"sessionId": sid})
                fut.cancel()
                raise AcpError("generation timed out (%.0fs)" % REQUEST_TIMEOUT, "TIMEOUT")
        result = fut.result()
        if held and not released:
            err = soft_error("".join(t for _k, t in held) + "\n")
            if err:
                raise err
            if result.get("stopReason") != "max_tokens":
                for h in held:
                    yield h
                held, released = [], True
        if isinstance(result.get("usage"), dict):
            yield "usage", json.dumps(result["usage"])
        stop = result.get("stopReason")
        if stop == "max_tokens" and i < MAX_CONTINUE:
            prompt = [{"type": "text", "text": "Continue exactly where you stopped. Do not repeat anything already written."}]
            continue
        if stop == "refusal":
            raise AcpError("the model refused to answer", "BLOCKED")
        break


async def generate(model_name: str, system: str, blocks: list[dict], effort_override: str | None = None) -> AsyncIterator[tuple[str, str, str]]:
    """Pick an account and run; if it fails before any answer text was sent, try the next account.
    Yields (kind, text, account name); kind is "text", "thought" or "usage"."""
    model, effort = resolve_model(model_name)
    if effort_override in LEVELS and not re.search(r"-(high|medium|low)$", model_name or ""):
        effort = effort_override
    deadline = time.time() + REQUEST_TIMEOUT
    tried: set[str] = set()
    last: AcpError | None = None
    upstream_fails = 0
    while time.time() < deadline:
        acc = await POOL.acquire(tried, deadline)
        if acc is None:
            break
        tried.add(acc.name)
        emitted = False
        tok_in = tok_out = 0
        t0 = time.time()
        try:
            async for kind, text in run_on(acc, model, effort, system, blocks, deadline):
                if kind == "text" and text:
                    emitted = True
                elif kind == "usage":
                    try:
                        u = json.loads(text)
                        tok_in += int(u.get("inputTokens") or 0) + int(u.get("cachedReadTokens") or 0)
                        tok_out += int(u.get("outputTokens") or 0) + int(u.get("thoughtTokens") or 0)
                    except (ValueError, TypeError):
                        pass
                yield kind, text, acc.name
            acc.ok += 1
            acc.last_ok_at = time.time()
            STATS.record(acc.name, True, tok_in, tok_out)
            log.info("ok account=%s model=%s %.1fs", acc.name, model_name, time.time() - t0)
            return
        except AcpError as e:
            last = e
            acc.fail += 1
            STATS.record(acc.name, False)
            acc.last_error, acc.last_error_at = str(e)[:300], time.time()
            log.warning("fail account=%s kind=%s %.1fs: %s", acc.name, e.kind, time.time() - t0, str(e)[:300])
            event("warn", acc.name, "%s: %s" % (e.kind, str(e)[:200]))
            if e.kind in ("QUOTA", "RATE_LIMIT", "VERIFY"):
                acc.cooldown_kind = e.kind
            if e.kind == "QUOTA":
                acc.cooldown_until = time.time() + COOLDOWN_QUOTA
            elif e.kind == "RATE_LIMIT":
                acc.cooldown_until = time.time() + COOLDOWN_RATE
            elif e.kind == "VERIFY":
                acc.cooldown_until = time.time() + COOLDOWN_VERIFY   # comes back by itself once verified
            elif e.kind == "AUTH":
                acc.disabled_reason = "needs a new login: " + str(e)[:120]
            elif e.kind == "CONFIG":
                acc.disabled_reason = str(e)[:160]
            elif e.kind == "TRANSPORT" and acc.acp:
                await acc.acp.stop()
            elif e.kind == "UPSTREAM_LIMIT":
                # maybe this account, maybe this prompt: no cooldown, check the real quota in the background,
                # and give up after two accounts so one bad prompt cannot walk through the whole pool
                upstream_fails += 1
                asyncio.create_task(quota_cooldown(acc))
                if upstream_fails >= 2:
                    raise
            if emitted or e.kind in ("BLOCKED", "UNKNOWN_MODEL", "BAD_REQUEST"):
                raise
        finally:
            POOL.release(acc)
    raise last or AcpError("no usable Antigravity account (all cooling down, logged out or disabled)", "NO_ACCOUNT")


# ======================================================================================================
# Requests: OpenAI / Anthropic -> one internal conversation -> ACP prompt
#   internal message: {"role": "user" | "assistant" | "tool", "parts": [...]}
#   part: {"type": "text", "text"} | {"type": "image", "mime", "data"} | {"type": "image_url", "url"}
#         | {"type": "tool_call", "id", "name", "arguments"} | {"type": "tool_result", "id", "name", "text", "is_error"}
#         | {"type": "media", "mime", "data", "where"}   (video mode only: video / audio / PDF, base64 until prepare_media)
# ======================================================================================================

class BadContent(Exception):
    """Content the gateway cannot hand to the model. Answered with HTTP 400 (in the format of the API that was called)
    instead of being dropped: a model that silently misses a video or a PDF still answers, and makes the content up."""

    def __init__(self, message: str, param: str = ""):
        super().__init__(message)
        self.param = param


# Measured with agy 1.5.3 (ACP promptCapabilities: image yes, audio no, embeddedContext yes): image blocks must be
# image/*; video, audio and PDF sent as embedded resources or audio blocks are accepted but never reach the model.
MEDIA_HINT = ("ACP-Ant can pass only text and images to Antigravity; video, audio, PDF and other binary files cannot "
              "reach the model through Antigravity's ACP interface. Send video as image frames, or use a provider that accepts files"
              + ("" if VIDEO_MODE else " (or ask the gateway's owner about video mode, AGY_VIDEO_MODE=1, for Gemini models)"))


def _media_part(mime: str, data: str, where: str) -> dict | None:
    """Video mode: a video / audio / PDF payload the video-mode agent can open; None when not applicable."""
    mime = (mime or "").strip().lower()
    if VIDEO_MODE and mime in MEDIA_EXT:
        return {"type": "media", "mime": mime, "data": data, "where": where}
    return None


def _url_media(url: str, where: str, what: str) -> list[dict]:
    """Video mode: a video_url / audio_url style part. Only inline data: URIs; links are not downloaded."""
    d = _parse_data_uri(url)
    if d:
        return _data_part(d[0], d[1], where, what)
    if VIDEO_MODE and re.match(r"https?://", url or ""):
        raise BadContent("unsupported content at %s: %s by link. Video mode does not download files: send it inline as a "
                         "base64 data: URI" % (where, what), where)
    raise _unsupported(where, what)


def _unsupported(where: str, what: str) -> BadContent:
    return BadContent("unsupported content at %s: %s. %s." % (where, what, MEDIA_HINT), where)


def _text_part(t: Any) -> list[dict]:
    return [{"type": "text", "text": str(t)}] if t not in (None, "") else []


_TEXT_MIMES = re.compile(r"^(text/[\w.+-]+|application/(json|xml|x-yaml|yaml|x-ndjson|csv))$", re.I)


def _data_part(mime: str, data: str, where: str, what: str, name: str = "") -> list[dict]:
    """Base64 payload with a MIME type: images go through, plain-text files become text, anything else is refused."""
    mime = (mime or "").strip().lower()
    if mime.startswith("image/"):
        return [{"type": "image", "mime": mime, "data": data}]
    media = _media_part(mime, data, where)
    if media:
        return [media]
    if _TEXT_MIMES.match(mime):
        try:
            text = base64.b64decode(data, validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise _unsupported(where, "%s (%s) that is not valid base64 UTF-8 text" % (what, mime))
        return _text_part("[file %s]\n%s" % (name, text) if name else text)
    raise _unsupported(where, "%s (%s)" % (what, mime or "unknown type"))


def _parse_data_uri(url: str) -> tuple[str, str] | None:
    mt = re.match(r"data:([\w/+.-]*)(?:;[\w-]+=[^;,]*)*;base64,(.*)$", url or "", re.S)
    return (mt.group(1), mt.group(2)) if mt else None


def _image_from_url(url: str, where: str, what: str = "image") -> dict:
    d = _parse_data_uri(url)
    if d:
        if not d[0].lower().startswith("image/"):
            media = _media_part(d[0], d[1], where)
            if media:
                return media
            raise _unsupported(where, "%s with a %s data URI" % (what, d[0] or "untyped"))
        return {"type": "image", "mime": d[0].lower(), "data": d[1]}
    if re.match(r"https?://", url or ""):
        return {"type": "image_url", "url": url, "where": where}
    raise BadContent("%s at %s needs a base64 data: URI or an http(s) URL" % (what, where), where)


def _openai_file(p: dict, where: str) -> list[dict]:
    """Chat Completions {"type": "file", "file": {"file_data", "filename"} | {"file_id"}}."""
    f = p.get("file") if isinstance(p.get("file"), dict) else {}
    name = str(f.get("filename") or "")
    d = _parse_data_uri(str(f.get("file_data") or ""))
    if d:
        return _data_part(d[0], d[1], where, 'part "file"' + (" " + name if name else ""), name)
    if f.get("file_id"):
        raise BadContent('unsupported content at %s: part "file" by file_id (this gateway has no file storage). %s.' % (where, MEDIA_HINT), where)
    raise _unsupported(where, 'part "file" without file_data')


def _openai_parts(content: Any, where: str = "content") -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return _text_part(content)
    if not isinstance(content, list):
        raise BadContent("%s must be a string or a list of content parts" % where, where)
    parts: list[dict] = []
    for i, p in enumerate(content):
        at = "%s[%d]" % (where, i)
        if not isinstance(p, dict):
            raise BadContent("%s must be an object" % at, at)
        t = p.get("type")
        if t in ("text", "input_text", "output_text"):
            parts += _text_part(p.get("text"))
        elif t == "refusal":
            parts += _text_part(p.get("refusal"))
        elif t in ("image_url", "input_image"):
            u = p.get("image_url")
            parts.append(_image_from_url(str((u.get("url") if isinstance(u, dict) else u) or ""), at, 'part "%s"' % t))
        elif t == "file":
            parts += _openai_file(p, at)
        elif t == "input_audio" and VIDEO_MODE:
            parts += _input_audio(p, at)
        elif t in ("video_url", "audio_url") and VIDEO_MODE:
            u = p.get(t)
            parts += _url_media(str((u.get("url") if isinstance(u, dict) else u) or ""), at, 'part "%s"' % t)
        else:   # input_audio, video_url, audio_url, ... : refuse rather than drop
            raise _unsupported(at, 'part "%s"' % t)
    return parts


def _input_audio(p: dict, where: str) -> list[dict]:
    """OpenAI {"type": "input_audio", "input_audio": {"data": base64, "format": "wav" | "mp3" | ...}} (video mode)."""
    a = p.get("input_audio") if isinstance(p.get("input_audio"), dict) else {}
    fmt = str(a.get("format") or "").lower()
    if fmt not in AUDIO_FORMATS or not a.get("data"):
        raise _unsupported(where, 'part "input_audio" with format "%s"%s' % (fmt, "" if a.get("data") else " and no data"))
    return _data_part(AUDIO_FORMATS[fmt], str(a["data"]), where, 'part "input_audio"')


def _only_text(parts: list[dict], where: str) -> str:
    if any(p["type"] != "text" for p in parts):
        raise BadContent("%s can only contain text (put images in a user message)" % where, where)
    return _joined_text(parts)


def _joined_text(parts: list[dict]) -> str:
    return "\n".join(p["text"] for p in parts if p["type"] == "text")


def _args_str(a: Any) -> str:
    if isinstance(a, str):
        return a or "{}"
    return json.dumps(a if a is not None else {}, ensure_ascii=False)


def from_openai(body: dict) -> tuple[str, list[dict], list[dict], Any]:
    system, msgs, names = [], [], {}
    for n, m in enumerate(body.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role, where = m.get("role"), "messages[%d].content" % n
        if role in ("system", "developer"):
            system.append(_only_text(_openai_parts(m.get("content"), where), where))
        elif role == "user":
            msgs.append({"role": "user", "parts": _openai_parts(m.get("content"), where)})
        elif role == "assistant":
            parts = _openai_parts(m.get("content"), where)
            calls = list(m.get("tool_calls") or [])
            if m.get("function_call"):
                calls.append({"id": "", "function": m["function_call"]})
            for tc in calls:
                f = tc.get("function") or {}
                names[tc.get("id") or ""] = f.get("name") or ""
                parts.append({"type": "tool_call", "id": tc.get("id") or "", "name": f.get("name") or "", "arguments": _args_str(f.get("arguments"))})
            msgs.append({"role": "assistant", "parts": parts})
        elif role in ("tool", "function"):
            tid = m.get("tool_call_id") or ""
            content = _openai_parts(m.get("content"), where)
            msgs.append({"role": "tool", "parts": [{"type": "tool_result", "id": tid, "name": m.get("name") or names.get(tid, ""),
                                                    "text": _joined_text(content), "is_error": False}]
                         + [p for p in content if p["type"] != "text"]})
    tools = []
    for t in body.get("tools") or []:
        f = t.get("function") if isinstance(t, dict) else None
        if isinstance(f, dict) and f.get("name"):
            tools.append({"name": f["name"], "description": f.get("description") or "", "parameters": f.get("parameters") or {}})
    for f in body.get("functions") or []:   # legacy
        if isinstance(f, dict) and f.get("name"):
            tools.append({"name": f["name"], "description": f.get("description") or "", "parameters": f.get("parameters") or {}})
    choice = body.get("tool_choice", body.get("function_call", "auto"))
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        choice = ("tool", name) if name else "auto"
    elif choice not in ("none", "required", "auto"):
        choice = "auto"
    return "\n\n".join(s for s in system if s), msgs, tools, choice


def _anthropic_skipped(t: str) -> bool:
    """Blocks the model itself produced on another provider (reasoning, server-side tool calls and their results):
    leaving them out loses nothing the user sent."""
    return t in ("thinking", "redacted_thinking", "server_tool_use", "mcp_tool_use") or (t.endswith("_tool_result") and t != "tool_result")


def _anthropic_parts(content: Any, where: str = "content") -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return _text_part(content)
    if not isinstance(content, list):
        raise BadContent("%s must be a string or a list of content blocks" % where, where)
    parts: list[dict] = []
    for i, b in enumerate(content):
        at = "%s.%d" % (where, i)
        if not isinstance(b, dict):
            raise BadContent("%s must be an object" % at, at)
        t = str(b.get("type") or "")
        src = b.get("source") if isinstance(b.get("source"), dict) else {}
        st = src.get("type")
        if t == "text":
            parts += _text_part(b.get("text"))
        elif t == "image":
            if st == "base64":
                mime = str(src.get("media_type") or "image/png").lower()
                if not mime.startswith("image/"):
                    media = _media_part(mime, src.get("data") or "", at)
                    if media:
                        parts.append(media)
                        continue
                    raise _unsupported(at, "image block with media_type %s" % mime)
                parts.append({"type": "image", "mime": mime, "data": src.get("data") or ""})
            elif st == "url":
                parts.append(_image_from_url(str(src.get("url") or ""), at, "image block"))
            else:
                raise _unsupported(at, 'image block with source type "%s"' % st)
        elif t == "document":
            title = str(b.get("title") or "")
            if st == "text":
                parts += _text_part("[document %s]\n%s" % (title, src.get("data") or "") if title else src.get("data"))
            elif st == "content":   # a document given as a list of text / image blocks
                parts += _anthropic_parts(src.get("content"), at + ".source.content")
            elif st == "base64":
                parts += _data_part(src.get("media_type") or "", src.get("data") or "", at, "document block", title)
            else:   # url / file: PDFs, mostly
                raise _unsupported(at, 'document block with source type "%s"' % st)
        elif t == "search_result":
            found = _only_text(_anthropic_parts(b.get("content"), at + ".content"), at + ".content")
            parts += _text_part("[search result: %s (%s)]\n%s" % (b.get("title") or "", b.get("source") or "", found))
        elif t == "tool_use":
            parts.append({"type": "tool_call", "id": b.get("id") or "", "name": b.get("name") or "", "arguments": _args_str(b.get("input"))})
        elif t == "tool_result":
            inner = _anthropic_parts(b.get("content"), at + ".content")
            parts.append({"type": "tool_result", "id": b.get("tool_use_id") or "", "name": "", "text": _joined_text(inner),
                          "is_error": bool(b.get("is_error"))})
            parts += [p for p in inner if p["type"] != "text"]
        elif _anthropic_skipped(t):
            continue
        else:   # container_upload, audio, video, ... : refuse rather than drop
            raise _unsupported(at, 'content block "%s"' % t)
    return parts


def from_anthropic(body: dict) -> tuple[str, list[dict], list[dict], Any]:
    s = body.get("system")
    system = s if isinstance(s, str) else _only_text(_anthropic_parts(s, "system"), "system")
    msgs, names = [], {}
    for n, m in enumerate(body.get("messages") or []):
        if not isinstance(m, dict):
            continue
        parts = _anthropic_parts(m.get("content"), "messages.%d.content" % n)
        if m.get("role") == "assistant":
            for p in parts:
                if p["type"] == "tool_call":
                    names[p["id"]] = p["name"]
            msgs.append({"role": "assistant", "parts": parts})
        else:
            results = [p for p in parts if p["type"] == "tool_result"]
            for r in results:
                r["name"] = names.get(r["id"], "")
            if results:
                # tool results first (with any images they carried), then whatever the user wrote alongside
                tail = [p for p in parts if p["type"] != "tool_result"]
                msgs.append({"role": "tool", "parts": results + [p for p in tail if p["type"] != "text"]})
                texts = [p for p in tail if p["type"] == "text"]
                if texts:
                    msgs.append({"role": "user", "parts": texts})
            else:
                msgs.append({"role": "user", "parts": parts})
    tools = [{"name": t["name"], "description": t.get("description") or "", "parameters": t.get("input_schema") or {}}
             for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name") and "input_schema" in t]
    c = body.get("tool_choice") or {"type": "auto"}
    ctype = c.get("type") if isinstance(c, dict) else c
    choice = {"any": "required", "none": "none", "tool": ("tool", (c or {}).get("name"))}.get(ctype, "auto")
    return system, msgs, tools, choice


def _responses_parts(content: Any, where: str = "content") -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return _text_part(content)
    if not isinstance(content, list):
        raise BadContent("%s must be a string or a list of content parts" % where, where)
    parts: list[dict] = []
    for i, p in enumerate(content):
        at = "%s[%d]" % (where, i)
        if not isinstance(p, dict):
            raise BadContent("%s must be an object" % at, at)
        t = p.get("type")
        if t in ("input_text", "output_text", "text", "summary_text"):
            parts += _text_part(p.get("text"))
        elif t == "input_image":
            u = p.get("image_url")
            u = u.get("url") if isinstance(u, dict) else u
            if not u:
                raise BadContent('unsupported content at %s: part "input_image" by file_id (this gateway has no file storage); '
                                 'send image_url as a data: URI or an http(s) URL' % at, at)
            parts.append(_image_from_url(str(u), at, 'part "input_image"'))
        elif t == "input_file":
            name = str(p.get("filename") or "")
            d = _parse_data_uri(str(p.get("file_data") or ""))
            if not d:
                raise _unsupported(at, 'part "input_file"%s by %s' % (" " + name if name else "", "file_id" if p.get("file_id") else "URL" if p.get("file_url") else "reference"))
            parts += _data_part(d[0], d[1], at, 'part "input_file"' + (" " + name if name else ""), name)
        elif t == "refusal":
            parts += _text_part(p.get("refusal"))
        elif t == "input_audio" and VIDEO_MODE:
            parts += _input_audio(p, at)
        else:   # input_audio, ... : refuse rather than drop
            raise _unsupported(at, 'part "%s"' % t)
    return parts


def from_responses(body: dict) -> tuple[str, list[dict], list[dict], Any, set[str]]:
    """OpenAI Responses API request -> internal conversation (used by Codex CLI and other newer OpenAI clients)."""
    system = [str(body.get("instructions") or "")]
    msgs: list[dict] = []
    names: dict[str, str] = {}

    def add(role: str, parts: list[dict]) -> None:
        if role in ("assistant", "tool") and msgs and msgs[-1]["role"] == role:
            msgs[-1]["parts"] += parts
        else:
            msgs.append({"role": role, "parts": parts})

    items = body.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    for n, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        t = it.get("type") or ("message" if it.get("role") else "")
        if t == "message":
            where = "input[%d].content" % n
            role, parts = it.get("role"), _responses_parts(it.get("content"), where)
            if role in ("system", "developer"):
                system.append(_only_text(parts, where))
            else:
                add("assistant" if role == "assistant" else "user", parts)
        elif t in ("function_call", "custom_tool_call"):
            cid = it.get("call_id") or it.get("id") or ""
            names[cid] = it.get("name") or ""
            args = _args_str(it.get("arguments")) if t == "function_call" else json.dumps({"input": it.get("input") or ""}, ensure_ascii=False)
            add("assistant", [{"type": "tool_call", "id": cid, "name": it.get("name") or "", "arguments": args}])
        elif t in ("function_call_output", "custom_tool_call_output"):
            cid = it.get("call_id") or ""
            out = it.get("output")
            parts = _text_part(out) if isinstance(out, str) else _responses_parts(out, "input[%d].output" % n)
            add("tool", [{"type": "tool_result", "id": cid, "name": names.get(cid, ""), "text": _joined_text(parts), "is_error": False}]
                + [p for p in parts if p["type"] != "text"])
        # reasoning items, web search calls etc. are not forwarded
    tools, custom = [], set()
    for t in body.get("tools") or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        if t.get("type") == "function":
            tools.append({"name": t["name"], "description": t.get("description") or "", "parameters": t.get("parameters") or {}})
        elif t.get("type") == "custom":   # free-form tools (e.g. Codex apply_patch): raw text instead of JSON
            custom.add(t["name"])
            fmt = t.get("format") or {}
            desc = (t.get("description") or "") + "\nThis tool takes free-form text, not JSON: put the complete text in arguments.input."
            if fmt.get("type") == "grammar" and fmt.get("definition"):
                desc += "\nThe text must follow this %s grammar:\n%s" % (fmt.get("syntax") or "lark", str(fmt["definition"])[:6000])
            tools.append({"name": t["name"], "description": desc,
                          "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}})
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, dict):
        choice = ("tool", choice.get("name")) if choice.get("name") else ("required" if choice.get("type") in ("any", "required") else "auto")
    elif choice not in ("none", "required", "auto"):
        choice = "auto"
    return "\n\n".join(s for s in system if s), msgs, tools, choice, custom


def tool_instructions(tools: list[dict], choice: Any, parallel: bool = True) -> str:
    lines = [
        "# Tools",
        "You can use the tools (functions) listed below. You cannot run them yourself: the client runs them and sends the results back.",
        "To call tools, end your reply with a <tool_calls> block that holds a JSON array, like this:",
        "<tool_calls>",
        '[{"name": "tool_name", "arguments": {"param": "value"}}]',
        "</tool_calls>",
        "Rules:",
        "- Write nothing after </tool_calls>. A short sentence before the block is fine.",
        "- \"arguments\" must be a JSON object that matches the tool's parameter schema. Use strict JSON: double quotes, escaped newlines and quotes inside strings.",
        "- Put several independent calls in the same array. Then stop and wait: the results arrive in <tool_result> blocks.",
        "- Never invent tool results, and never wrap the block in code fences.",
        "- When no tool is needed, answer normally without a <tool_calls> block.",
    ]
    if not parallel:
        lines.append("- Call at most one tool per reply.")
    if choice == "required":
        lines.append("- In this reply you MUST call at least one tool.")
    elif isinstance(choice, tuple) and choice[1]:
        lines.append("- In this reply you MUST call the tool \"%s\"." % choice[1])
    lines.append("")
    lines.append("Available tools (JSON Schema for the arguments):")
    for t in tools:
        lines.append(json.dumps({"name": t["name"], "description": t["description"], "parameters": t["parameters"]}, ensure_ascii=False))
    return "\n".join(lines)


def _calls_block(calls: list[dict]) -> str:
    arr = []
    for c in calls:
        try:
            args = json.loads(c["arguments"], strict=False) if isinstance(c["arguments"], str) else c["arguments"]
        except (ValueError, TypeError):
            args = c["arguments"]
        arr.append({"name": c["name"], "arguments": args})
    return "<tool_calls>\n" + json.dumps(arr, ensure_ascii=False) + "\n</tool_calls>"


def _clamp_part(p: dict, max_chars: int) -> dict:
    if p.get("type") == "tool_result":
        t = p.get("text") or ""
        if len(t) > max_chars:
            half = max(500, (max_chars - 100) // 2)
            clamped = f"{t[:half]}\n\n... [output truncated ({len(t)} chars) by gateway] ...\n\n{t[-half:]}"
            return {**p, "text": clamped}
    return p


def _clamp_msg(m: dict, max_chars: int) -> dict:
    parts = m.get("parts") or []
    if any(p.get("type") == "tool_result" and len(p.get("text") or "") > max_chars for p in parts):
        return {**m, "parts": [_clamp_part(p, max_chars) for p in parts]}
    return m


def _estimate_msg_chars(m: dict) -> int:
    chars = 15
    for p in m.get("parts") or []:
        pt = p.get("type")
        if pt == "text":
            chars += len(p.get("text") or "")
        elif pt == "tool_result":
            chars += len(p.get("text") or "") + len(p.get("name") or "") + 40
        elif pt == "tool_call":
            chars += len(p.get("arguments") or "") + len(p.get("name") or "") + 40
    return chars


def to_acp(system: str, msgs: list[dict], tools: list[dict], choice: Any, extra_system: str = "", parallel: bool = True) -> tuple[str, list[dict]]:
    """Internal conversation -> (system prompt, ACP prompt blocks). Earlier turns become a transcript (images stay
    in place as image blocks); the trailing user / tool messages are this turn's input."""
    sys_parts = [system] if system else []
    if tools and choice != "none":
        sys_parts.append(tool_instructions(tools, choice, parallel))
    if extra_system:
        sys_parts.append(extra_system)
    msgs = [m for m in msgs if m["parts"]]
    if not msgs:
        raise HTTPException(400, "no messages")
    k = len(msgs)
    while k > 0 and msgs[k - 1]["role"] != "assistant":
        k -= 1
    history, current = msgs[:k], msgs[k:]

    # Protect current turn from being eclipsed: clamp oversized tool outputs and calculate budget
    current = [_clamp_msg(m, MAX_TOOL_RESULT_CHARS) for m in current]
    current_est = sum(_estimate_msg_chars(m) for m in current)
    history_budget = max(0, MAX_PROMPT_CHARS - current_est - 500)

    # Prune history from newest to oldest if total exceeds budget
    history_kept = []
    consumed = 0
    history_omitted = False

    for m in reversed(history):
        m_clamped = _clamp_msg(m, MAX_TOOL_RESULT_CHARS)
        m_chars = _estimate_msg_chars(m_clamped)
        if consumed + m_chars <= history_budget:
            history_kept.append(m_clamped)
            consumed += m_chars
        else:
            history_omitted = True

    history = list(reversed(history_kept))
    n_img = sum(1 for m in (history + current) for p in m["parts"] if p["type"] == "image")
    skip = max(0, n_img - MAX_IMAGES)       # drop the oldest images beyond the limit
    blocks: list[dict] = []

    def text(t: str) -> None:
        if blocks and blocks[-1]["type"] == "text":
            blocks[-1]["text"] += t
        else:
            blocks.append({"type": "text", "text": t})

    def image(p: dict) -> None:
        nonlocal skip
        if skip > 0:
            skip -= 1
            text("[image omitted]")
        else:
            blocks.append({"type": "image", "mimeType": p["mime"], "data": p["data"]})

    n_media = 0

    def media(p: dict) -> None:   # video mode: written to a file and sent as a link by run_on
        nonlocal n_media
        n_media += 1
        text("[attachment %d: %s]" % (n_media, p["mime"]))
        blocks.append({"type": "media", "mime": p["mime"], "data": p["data"]})

    def render(m: dict, labelled: bool) -> None:
        role = m["role"]
        if role == "tool":
            for p in m["parts"]:
                if p["type"] == "tool_result":
                    text('\n\n<tool_result name="%s"%s>\n%s\n</tool_result>' % (p["name"], ' is_error="true"' if p["is_error"] else "", p["text"]))
                elif p["type"] == "image":
                    image(p)
                elif p["type"] == "media":
                    media(p)
            return
        if labelled:
            text("\n\n%s: " % ("User" if role == "user" else "Assistant"))
        elif blocks:
            text("\n\n")
        for p in m["parts"]:
            if p["type"] == "text":
                text(p["text"])
            elif p["type"] == "image":
                image(p)
            elif p["type"] == "media":
                media(p)
        calls = [p for p in m["parts"] if p["type"] == "tool_call"]
        if calls:
            text("\n" + _calls_block(calls))

    if history:
        text("<conversation_history>")
        if history_omitted:
            text("\n[... earlier conversation history omitted to stay within token budget ...]\n")
        for m in history:
            render(m, True)
        text("\n</conversation_history>\n\n" + ("The latest input for this turn:\n" if current else
                                                  "Continue your last reply exactly where it stopped.\n"))
    for m in current:
        render(m, False)
    if blocks and blocks[0]["type"] == "text":
        blocks[0]["text"] = blocks[0]["text"].lstrip("\n")
    if not any(b["type"] == "text" and b["text"].strip() for b in blocks):
        blocks.append({"type": "text", "text": "(see the attachment)" if n_media else "(see the image)"})
    return "\n\n".join(s for s in sys_parts if s), blocks


_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def _sniff_image(data: bytes) -> str | None:
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _fetch_image(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "acp-ant"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("image larger than 20 MB")
        ctype = (r.headers.get("Content-Type") and r.headers.get_content_type() or "").lower()
    mime = ctype if ctype.startswith("image/") else _sniff_image(data)   # some storage servers say application/octet-stream
    if not mime:
        raise ValueError("the link is not an image (Content-Type %s)" % (ctype or "missing"))
    return {"type": "image", "mime": mime, "data": base64.b64encode(data).decode()}


def prepare_media(msgs: list[dict], model_name: str) -> None:
    """Video mode: check the request's video / audio / PDF parts (Gemini model, count, size) and decode them.
    Anything wrong is a 400 before an account is used."""
    media = [p for m in msgs for p in m["parts"] if p["type"] == "media"]
    if not media:
        return
    model, _level = resolve_model(model_name)
    if not VIDEO_MODELS.search(model):
        raise BadContent("unsupported content at %s: video mode passes video, audio and PDF only to Gemini models; model %s "
                         "cannot open them. Use a Gemini model, for example gemini-4-argon" % (media[0]["where"], model), media[0]["where"])
    if len(media) > VIDEO_MAX_FILES:
        w = media[VIDEO_MAX_FILES]["where"]
        raise BadContent("too many files at %s: video mode takes at most %d video / audio / PDF files per request "
                         "(AGY_VIDEO_MAX_FILES)" % (w, VIDEO_MAX_FILES), w)
    total = 0
    for p in media:
        if isinstance(p["data"], str):
            try:
                p["data"] = base64.b64decode(re.sub(r"\s+", "", p["data"]), validate=True)
            except ValueError:
                raise BadContent("the file at %s is not valid base64" % p["where"], p["where"])
        if not p["data"]:
            raise BadContent("the file at %s is empty" % p["where"], p["where"])
        total += len(p["data"])
        if total > VIDEO_MAX_MB * 1024 * 1024:
            raise BadContent("files too large at %s: video mode takes at most %g MB of video / audio / PDF per request "
                             "(AGY_VIDEO_MAX_MB)" % (p["where"], VIDEO_MAX_MB), p["where"])


async def fetch_remote_images(msgs: list[dict]) -> None:
    """Download http(s) image links. A link that cannot be used fails the request (400) instead of being left out."""
    for m in msgs:
        for i, p in enumerate(m["parts"]):
            if p["type"] != "image_url":
                continue
            where = p.get("where") or "content"
            if not FETCH_IMAGES:
                raise BadContent("image link at %s: this gateway does not download image links (AGY_FETCH_IMAGES=0); "
                                 "send the image as a base64 data: URI" % where, where)
            try:
                m["parts"][i] = await asyncio.to_thread(_fetch_image, p["url"])
            except Exception as e:  # noqa: BLE001
                raise BadContent("image link at %s could not be used: %s" % (where, str(e)[:200]), where)


# ======================================================================================================
# Model output -> tool calls
# ======================================================================================================

TAG_OPEN, TAG_CLOSE = "<tool_calls>", "</tool_calls>"
ALT_OPEN, ALT_CLOSE = "<function_calls>", "</function_calls>"
OPEN_TAGS = (TAG_OPEN, "<tool_call>", ALT_OPEN)


def _loads_lenient(s: str) -> Any:
    s = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", s.strip())
    try:
        return json.loads(s, strict=False)
    except Exception:
        pass
    for a, b in (("[", "]"), ("{", "}")):
        i, j = s.find(a), s.rfind(b)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1], strict=False)
            except Exception:
                continue
    if repair_json is not None:
        try:
            return json.loads(repair_json(s), strict=False)
        except Exception:
            pass
    return None


# Claude-family models sometimes fall back to their own XML tool format; accept it too.
def _parse_invokes(body: str) -> list[dict]:
    calls = []
    for m in re.finditer(r'<invoke name="([^"]+)">(.*?)(?:</invoke>|$)', body, re.S):
        args: dict[str, Any] = {}
        for p in re.finditer(r'<parameter name="([^"]+)">(.*?)</parameter>', m.group(2), re.S):
            v = p.group(2)
            v = v[1:] if v.startswith("\n") else v
            v = v[:-1] if v.endswith("\n") else v
            if v.strip()[:1] in ("{", "[") or v.strip() in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", v.strip()):
                try:
                    args[p.group(1)] = json.loads(v, strict=False)
                    continue
                except ValueError:
                    pass
            args[p.group(1)] = v
        calls.append({"name": m.group(1), "arguments": args})
    return calls


def parse_tool_calls(text: str) -> tuple[str, list[dict] | None]:
    """Split the model's reply into (text before the block, [{"name", "arguments"(dict|str)}]) or (text, None)."""
    tag_open, tag_close = None, None
    i = -1
    for topen, tclose in ((TAG_OPEN, TAG_CLOSE), ("<tool_call>", "</tool_call>")):
        pos = text.find(topen)
        if pos >= 0 and (i < 0 or pos < i):
            i = pos
            tag_open, tag_close = topen, tclose

    if i < 0 or tag_open is None or tag_close is None:
        k = text.find(ALT_OPEN)
        if k >= 0:
            calls = _parse_invokes(text[k:])
            if calls:
                return re.sub(r"\n?```[a-zA-Z]*\s*$", "", text[:k]).rstrip(), calls
        return text, None

    body = text[i + len(tag_open):]
    j = body.find(tag_close)
    if j >= 0:
        body = body[:j]
    data = _loads_lenient(body)
    if isinstance(data, dict):
        data = data.get("tool_calls") or data.get("calls") or [data]
    calls = []
    for c in data if isinstance(data, list) else []:
        if not isinstance(c, dict):
            continue
        f = c.get("function") if isinstance(c.get("function"), dict) else {}
        name = c.get("name") or f.get("name")
        args = next((c[k] for k in ("arguments", "input", "parameters", "args") if k in c), f.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args, strict=False) if args.strip() else {}
            except ValueError:
                if repair_json is not None:
                    try:
                        args = json.loads(repair_json(args), strict=False)
                    except Exception:
                        pass
        if name:
            calls.append({"name": str(name), "arguments": args if args is not None else {}})
    if not calls:
        return text, None
    content = re.sub(r"\n?```[a-zA-Z]*\s*$", "", text[:i]).rstrip()
    return content, calls


class ToolStream:
    """Streams text through while holding back anything that could be the start of a <tool_calls> block."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.buf = ""
        self.cap: str | None = None

    def feed(self, t: str) -> str:
        if not self.enabled:
            return t
        if self.cap is not None:
            self.cap += t
            return ""
        self.buf += t
        hits = [i for i in (self.buf.find(tag) for tag in OPEN_TAGS) if i >= 0]
        if hits:
            i = min(hits)
            out, self.cap, self.buf = self.buf[:i], self.buf[i:], ""
            return out
        longest = max(len(tag) for tag in OPEN_TAGS)
        keep = next((k for k in range(min(longest - 1, len(self.buf)), 0, -1)
                     if any(tag.startswith(self.buf[-k:]) for tag in OPEN_TAGS)), 0)
        out, self.buf = self.buf[:len(self.buf) - keep], self.buf[len(self.buf) - keep:]
        return out

    def finish(self) -> tuple[str, list[dict] | None]:
        if self.cap is None:
            return self.buf, None
        _content, calls = parse_tool_calls(self.cap)
        return ("", calls) if calls else (self.cap, None)


def _json_instruction(rf: Any) -> str:
    if not isinstance(rf, dict) or rf.get("type") not in ("json_object", "json_schema"):
        return ""
    s = "# Output format\nReply with exactly one valid JSON value and nothing else: no code fences, no comments, no extra text."
    schema = (rf.get("json_schema") or {}).get("schema") if rf.get("type") == "json_schema" else None
    if schema:
        s += "\nIt must match this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False)
    return s


def _strip_fences(t: str) -> str:
    m = re.match(r"^\s*```(?:json)?\s*\n(.*?)\n?```\s*$", t, re.S)
    return m.group(1) if m else t


class Usage:
    def __init__(self) -> None:
        self.real = {"inputTokens": 0, "outputTokens": 0, "thoughtTokens": 0, "cachedReadTokens": 0}
        self.n = 0

    def add(self, raw: str) -> None:
        u = json.loads(raw)
        for k in self.real:
            self.real[k] += int(u.get(k) or 0)
        self.n += 1

    def tokens(self, prompt_est: int, completion_text: str) -> tuple[int, int, int, int]:
        """(prompt, completion incl. reasoning, reasoning, cached)."""
        r = self.real
        if self.n:
            return r["inputTokens"] + r["cachedReadTokens"], r["outputTokens"] + r["thoughtTokens"], r["thoughtTokens"], r["cachedReadTokens"]
        return prompt_est, est_tokens(completion_text), 0, 0


def est_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8")) // 3)


def est_prompt(system: str, blocks: list[dict]) -> int:
    return est_tokens(system) + sum(est_tokens(b["text"]) if b["type"] == "text" else 1500 for b in blocks)


# ======================================================================================================
# HTTP API
# ======================================================================================================

app = FastAPI(title="ACP-Ant", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(BadContent)
async def _bad_content(request: Request, e: BadContent) -> JSONResponse:
    """Content the model cannot receive: 400 in the error format of the API that was called (clients and fallback
    logic recognise it), never a reply written without the content."""
    log.info("400 unsupported content on %s: %s", request.url.path, str(e)[:200])
    if request.url.path.startswith("/v1/messages"):
        return JSONResponse({"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}}, status_code=400)
    return JSONResponse({"error": {"message": str(e), "type": "invalid_request_error", "param": e.param or None,
                                   "code": "unsupported_content"}}, status_code=400)


def check_key(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth[:7].lower() == "bearer " else (request.headers.get("x-api-key") or request.headers.get("api-key") or "")
    if not API_KEY or not secrets.compare_digest(token.encode(), API_KEY.encode()):
        raise HTTPException(401, "unauthorized")


SSE_PING = ": keep-alive\n\n"                                       # an SSE comment: every client skips it
ANTHROPIC_PING = 'event: ping\ndata: {"type": "ping"}\n\n'           # Anthropic's own keep-alive event


async def with_heartbeat(events: AsyncIterator[str], ping: str = SSE_PING) -> AsyncIterator[str]:
    """While the model thinks, writes a long tool call or waits for a free account, a stream sends nothing. Proxies
    then drop the connection (Cloudflare after 100 s without data), so after HEARTBEAT silent seconds send a ping."""
    if HEARTBEAT <= 0:
        async for e in events:
            yield e
        return
    q: asyncio.Queue = asyncio.Queue()
    end = object()

    async def pump() -> None:
        try:
            async for e in events:
                q.put_nowait(e)
            q.put_nowait(end)
        except Exception as e:  # noqa: BLE001   (handed to the reader)
            q.put_nowait(e)
    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), HEARTBEAT)
            except asyncio.TimeoutError:
                yield ping
                continue
            if item is end:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        task.cancel()     # client gone: stop the generation (frees the account slot)


ERR_STATUS = {"QUOTA": 429, "RATE_LIMIT": 429, "UPSTREAM_LIMIT": 429, "NO_ACCOUNT": 503, "TIMEOUT": 504, "BLOCKED": 400, "UNKNOWN_MODEL": 400,
              "BAD_REQUEST": 400, "AUTH": 503, "BUSY": 503}


def _wire_calls(calls: list[dict], prefix: str) -> list[dict]:
    return [{"id": prefix + uuid.uuid4().hex[:24], "name": c["name"], "arguments": c["arguments"]} for c in calls]


async def _read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "body is not valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    return body


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_key(request)
    body = await _read_body(request)
    model_name = str(body.get("model") or DEFAULT_MODEL)
    system, msgs, tools, choice = from_openai(body)
    prepare_media(msgs, model_name)
    await fetch_remote_images(msgs)
    system, blocks = to_acp(system, msgs, tools, choice, _json_instruction(body.get("response_format")), body.get("parallel_tool_calls") is not False)
    use_tools = bool(tools) and choice != "none"
    effort = body.get("reasoning_effort")
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    prompt_est = est_prompt(system, blocks)

    def usage_obj(u: Usage, text: str) -> dict:
        pt, ct, rt, cached = u.tokens(prompt_est, text)
        d = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
             "prompt_tokens_details": {"cached_tokens": cached}, "completion_tokens_details": {"reasoning_tokens": rt}}
        if not u.n:
            d["estimated"] = True
        return d

    if body.get("stream"):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        def chunk(delta: dict, finish: str | None = None) -> str:
            return "data: %s\n\n" % json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
                                               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}, ensure_ascii=False)

        async def sse():
            u, ts, full = Usage(), ToolStream(use_tools), []
            try:
                yield chunk({"role": "assistant", "content": ""})
                async for kind, text, _acc in generate(model_name, system, blocks, effort):
                    if kind == "usage":
                        u.add(text)
                    elif kind == "text" and text:
                        full.append(text)
                        out = ts.feed(text)
                        if out:
                            yield chunk({"content": out})
                tail, calls = ts.finish()
                if tail:
                    yield chunk({"content": tail})
                if calls:
                    wc = _wire_calls(calls, "call_")
                    yield chunk({"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                                 "function": {"name": c["name"], "arguments": _args_str(c["arguments"])}} for i, c in enumerate(wc)]})
                yield chunk({}, "tool_calls" if calls else "stop")
                if include_usage:
                    yield "data: %s\n\n" % json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
                                                       "choices": [], "usage": usage_obj(u, "".join(full))})
            except AcpError as e:
                yield "data: %s\n\n" % json.dumps({"error": {"message": str(e), "type": e.kind.lower(), "code": e.kind}}, ensure_ascii=False)
            except Exception as e:  # noqa: BLE001
                log.exception("stream failed")
                yield "data: %s\n\n" % json.dumps({"error": {"message": str(e)[:300], "type": "server_error", "code": "SERVER"}})
            yield "data: [DONE]\n\n"
        return StreamingResponse(with_heartbeat(sse()), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    u, parts, acc_name = Usage(), [], ""
    try:
        async for kind, text, acc_name in generate(model_name, system, blocks, effort):
            if kind == "text":
                parts.append(text)
            elif kind == "usage":
                u.add(text)
    except AcpError as e:
        return JSONResponse({"error": {"message": str(e), "type": e.kind.lower(), "code": e.kind}}, status_code=ERR_STATUS.get(e.kind, 502))
    raw = "".join(parts)
    content, calls = parse_tool_calls(raw) if use_tools else (raw, None)
    if body.get("response_format") and not calls:
        content = _strip_fences(content)
    msg: dict[str, Any] = {"role": "assistant", "content": content if (content or not calls) else None}
    if calls:
        msg["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": _args_str(c["arguments"])}}
                             for c in _wire_calls(calls, "call_")]
    return JSONResponse({"id": cid, "object": "chat.completion", "created": created, "model": model_name,
                         "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
                         "usage": usage_obj(u, raw), "x_account": acc_name})


ANTHROPIC_ERR = {"QUOTA": (429, "rate_limit_error"), "RATE_LIMIT": (429, "rate_limit_error"), "UPSTREAM_LIMIT": (429, "rate_limit_error"), "NO_ACCOUNT": (529, "overloaded_error"),
                 "TIMEOUT": (504, "api_error"), "BLOCKED": (400, "invalid_request_error"), "UNKNOWN_MODEL": (400, "not_found_error"),
                 "BAD_REQUEST": (400, "invalid_request_error"), "AUTH": (529, "overloaded_error"),
                 "BUSY": (529, "overloaded_error")}


@app.post("/v1/messages")
async def messages(request: Request):
    check_key(request)
    body = await _read_body(request)
    model_name = str(body.get("model") or DEFAULT_MODEL)
    system, msgs, tools, choice = from_anthropic(body)
    prepare_media(msgs, model_name)
    await fetch_remote_images(msgs)
    parallel = not (isinstance(body.get("tool_choice"), dict) and body["tool_choice"].get("disable_parallel_tool_use"))
    system, blocks = to_acp(system, msgs, tools, choice, parallel=parallel)
    use_tools = bool(tools) and choice != "none"
    mid = "msg_" + uuid.uuid4().hex[:24]
    prompt_est = est_prompt(system, blocks)

    def usage_obj(u: Usage, text: str) -> dict:
        pt, ct, _rt, cached = u.tokens(prompt_est, text)
        return {"input_tokens": max(0, pt - cached), "output_tokens": ct, "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}

    def tool_blocks(calls: list[dict]) -> list[dict]:
        return [{"type": "tool_use", "id": c["id"], "name": c["name"],
                 "input": c["arguments"] if isinstance(c["arguments"], dict) else {"_raw": c["arguments"]}} for c in _wire_calls(calls, "toolu_")]

    if body.get("stream"):
        def ev(name: str, data: dict) -> str:
            return "event: %s\ndata: %s\n\n" % (name, json.dumps(data, ensure_ascii=False))

        async def sse():
            u, ts, full = Usage(), ToolStream(use_tools), []
            idx, text_open = 0, False
            yield ev("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": model_name, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": prompt_est, "output_tokens": 1}}})
            try:
                async for kind, text, _acc in generate(model_name, system, blocks):
                    if kind == "usage":
                        u.add(text)
                        continue
                    if kind != "text" or not text:
                        continue
                    full.append(text)
                    out = ts.feed(text)
                    if out:
                        if not text_open:
                            yield ev("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
                            text_open = True
                        yield ev("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": out}})
                tail, calls = ts.finish()
                if tail:
                    if not text_open:
                        yield ev("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
                        text_open = True
                    yield ev("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": tail}})
                if text_open:
                    yield ev("content_block_stop", {"type": "content_block_stop", "index": idx})
                    idx += 1
                for b in tool_blocks(calls or []):
                    yield ev("content_block_start", {"type": "content_block_start", "index": idx,
                                                     "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
                    yield ev("content_block_delta", {"type": "content_block_delta", "index": idx,
                                                     "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"], ensure_ascii=False)}})
                    yield ev("content_block_stop", {"type": "content_block_stop", "index": idx})
                    idx += 1
                if idx == 0:   # empty reply: still send one (empty) text block
                    yield ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                    yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
                yield ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None},
                                           "usage": usage_obj(u, "".join(full))})
                yield ev("message_stop", {"type": "message_stop"})
            except AcpError as e:
                yield ev("error", {"type": "error", "error": {"type": ANTHROPIC_ERR.get(e.kind, (0, "api_error"))[1], "message": str(e)}})
            except Exception as e:  # noqa: BLE001
                log.exception("stream failed")
                yield ev("error", {"type": "error", "error": {"type": "api_error", "message": str(e)[:300]}})
        return StreamingResponse(with_heartbeat(sse(), ANTHROPIC_PING), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    u, parts = Usage(), []
    try:
        async for kind, text, _acc in generate(model_name, system, blocks):
            if kind == "text":
                parts.append(text)
            elif kind == "usage":
                u.add(text)
    except AcpError as e:
        status, etype = ANTHROPIC_ERR.get(e.kind, (502, "api_error"))
        return JSONResponse({"type": "error", "error": {"type": etype, "message": str(e)}}, status_code=status)
    raw = "".join(parts)
    content, calls = parse_tool_calls(raw) if use_tools else (raw, None)
    out_blocks = ([{"type": "text", "text": content}] if content or not calls else []) + tool_blocks(calls or [])
    return JSONResponse({"id": mid, "type": "message", "role": "assistant", "model": model_name, "content": out_blocks,
                         "stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None, "usage": usage_obj(u, raw)})


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    check_key(request)
    body = await _read_body(request)
    system, msgs, tools, choice = from_anthropic(body)
    msgs = [m for m in msgs if m["parts"]] or [{"role": "user", "parts": [{"type": "text", "text": " "}]}]
    for m in msgs:   # do not download anything just to count
        m["parts"] = [p if p["type"] != "image_url" else {"type": "text", "text": "[image]"} for p in m["parts"]]
    system, blocks = to_acp(system, msgs, tools, choice)
    return {"input_tokens": est_prompt(system, blocks)}


RESPONSES_ERR = {"QUOTA": "rate_limit_exceeded", "RATE_LIMIT": "rate_limit_exceeded", "UPSTREAM_LIMIT": "rate_limit_exceeded",
                 "NO_ACCOUNT": "server_is_overloaded", "TIMEOUT": "server_error", "BLOCKED": "invalid_prompt",
                 "UNKNOWN_MODEL": "model_not_found", "BAD_REQUEST": "invalid_request", "BUSY": "server_is_overloaded"}


@app.post("/v1/responses")
async def responses(request: Request):
    """OpenAI Responses API (Codex CLI and other newer OpenAI clients): text, images, function tools, custom tools."""
    check_key(request)
    body = await _read_body(request)
    model_name = str(body.get("model") or DEFAULT_MODEL)
    system, msgs, tools, choice, custom = from_responses(body)
    prepare_media(msgs, model_name)
    await fetch_remote_images(msgs)
    fmt = (body.get("text") or {}).get("format") or {}
    rf = {"type": "json_schema", "json_schema": {"schema": fmt.get("schema")}} if fmt.get("type") == "json_schema" else (
        {"type": "json_object"} if fmt.get("type") == "json_object" else None)
    system, blocks = to_acp(system, msgs, tools, choice, _json_instruction(rf), body.get("parallel_tool_calls") is not False)
    use_tools = bool(tools) and choice != "none"
    effort = (body.get("reasoning") or {}).get("effort")
    rid = "resp_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    prompt_est = est_prompt(system, blocks)

    def usage_obj(u: Usage, text: str) -> dict:
        pt, ct, rt, cached = u.tokens(prompt_est, text)
        return {"input_tokens": pt, "input_tokens_details": {"cached_tokens": cached}, "output_tokens": ct,
                "output_tokens_details": {"reasoning_tokens": rt}, "total_tokens": pt + ct}

    def resp(status: str, output: list, usage: dict | None = None, error: dict | None = None) -> dict:
        return {"id": rid, "object": "response", "created_at": created, "status": status, "model": model_name, "output": output,
                "error": error, "incomplete_details": None, "usage": usage, "parallel_tool_calls": body.get("parallel_tool_calls", True),
                "tool_choice": body.get("tool_choice", "auto"), "tools": body.get("tools") or [], "metadata": body.get("metadata") or {},
                "text": {"format": {"type": "text"}}, "store": False}

    def call_items(calls: list[dict]) -> list[dict]:
        items = []
        for c in calls:
            cid = "call_" + uuid.uuid4().hex[:24]
            if c["name"] in custom:
                a = c["arguments"]
                text_in = a.get("input") if isinstance(a, dict) and isinstance(a.get("input"), str) else (a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
                items.append({"type": "custom_tool_call", "id": "ctc_" + uuid.uuid4().hex[:24], "call_id": cid, "name": c["name"],
                              "input": text_in, "status": "completed"})
            else:
                items.append({"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:24], "call_id": cid, "name": c["name"],
                              "arguments": _args_str(c["arguments"]), "status": "completed"})
        return items

    if body.get("stream"):
        seq = [0]

        def ev(name: str, data: dict) -> str:
            data = {"type": name, "sequence_number": seq[0], **data}
            seq[0] += 1
            return "event: %s\ndata: %s\n\n" % (name, json.dumps(data, ensure_ascii=False))

        async def sse():
            u, ts, full, out_items = Usage(), ToolStream(use_tools), [], []
            msg_id, text_parts, msg_open = "msg_" + uuid.uuid4().hex[:24], [], False
            yield ev("response.created", {"response": resp("in_progress", [])})
            yield ev("response.in_progress", {"response": resp("in_progress", [])})
            try:
                async def emit_text(t: str):
                    nonlocal msg_open
                    if not msg_open:
                        msg_open = True
                        yield ev("response.output_item.added", {"output_index": 0, "item": {"type": "message", "id": msg_id, "status": "in_progress",
                                                                                            "role": "assistant", "content": []}})
                        yield ev("response.content_part.added", {"item_id": msg_id, "output_index": 0, "content_index": 0,
                                                                 "part": {"type": "output_text", "text": "", "annotations": []}})
                    text_parts.append(t)
                    yield ev("response.output_text.delta", {"item_id": msg_id, "output_index": 0, "content_index": 0, "delta": t, "logprobs": []})

                async for kind, text, _acc in generate(model_name, system, blocks, effort):
                    if kind == "usage":
                        u.add(text)
                    elif kind == "text" and text:
                        full.append(text)
                        out = ts.feed(text)
                        if out:
                            async for e in emit_text(out):
                                yield e
                tail, calls = ts.finish()
                if tail:
                    async for e in emit_text(tail):
                        yield e
                if msg_open:
                    txt = "".join(text_parts)
                    part = {"type": "output_text", "text": txt, "annotations": []}
                    item = {"type": "message", "id": msg_id, "status": "completed", "role": "assistant", "content": [part]}
                    yield ev("response.output_text.done", {"item_id": msg_id, "output_index": 0, "content_index": 0, "text": txt, "logprobs": []})
                    yield ev("response.content_part.done", {"item_id": msg_id, "output_index": 0, "content_index": 0, "part": part})
                    yield ev("response.output_item.done", {"output_index": 0, "item": item})
                    out_items.append(item)
                for item in call_items(calls or []):
                    idx = len(out_items)
                    if item["type"] == "function_call":
                        yield ev("response.output_item.added", {"output_index": idx, "item": dict(item, arguments="", status="in_progress")})
                        yield ev("response.function_call_arguments.delta", {"item_id": item["id"], "output_index": idx, "delta": item["arguments"]})
                        yield ev("response.function_call_arguments.done", {"item_id": item["id"], "output_index": idx, "arguments": item["arguments"]})
                    else:
                        yield ev("response.output_item.added", {"output_index": idx, "item": dict(item, input="", status="in_progress")})
                        yield ev("response.custom_tool_call_input.delta", {"item_id": item["id"], "output_index": idx, "delta": item["input"]})
                        yield ev("response.custom_tool_call_input.done", {"item_id": item["id"], "output_index": idx, "input": item["input"]})
                    yield ev("response.output_item.done", {"output_index": idx, "item": item})
                    out_items.append(item)
                yield ev("response.completed", {"response": resp("completed", out_items, usage_obj(u, "".join(full)))})
            except AcpError as e:
                yield ev("response.failed", {"response": resp("failed", out_items, error={"code": RESPONSES_ERR.get(e.kind, "server_error"), "message": str(e)})})
            except Exception as e:  # noqa: BLE001
                log.exception("stream failed")
                yield ev("response.failed", {"response": resp("failed", out_items, error={"code": "server_error", "message": str(e)[:300]})})
        return StreamingResponse(with_heartbeat(sse()), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    u, parts = Usage(), []
    try:
        async for kind, text, _acc in generate(model_name, system, blocks, effort):
            if kind == "text":
                parts.append(text)
            elif kind == "usage":
                u.add(text)
    except AcpError as e:
        return JSONResponse({"error": {"message": str(e), "type": e.kind.lower(), "code": RESPONSES_ERR.get(e.kind, "server_error")}},
                            status_code=ERR_STATUS.get(e.kind, 502))
    raw = "".join(parts)
    content, calls = parse_tool_calls(raw) if use_tools else (raw, None)
    if rf and not calls:
        content = _strip_fences(content)
    output = ([{"type": "message", "id": "msg_" + uuid.uuid4().hex[:24], "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}]}] if content or not calls else []) + call_items(calls or [])
    r = resp("completed", output, usage_obj(u, raw))
    r["output_text"] = content
    return JSONResponse(r)


@app.get("/v1/models")
async def models(request: Request):
    check_key(request)
    names = list(MODEL_ALIASES)
    for m in POOL.models_seen:
        variants = [] if re.search(r"-(low|medium|high)$", m) else ["%s-%s" % (m, lv) for lv in (POOL.levels_seen or LEVELS)]
        for n in [m] + variants:
            if n not in names:
                names.append(n)
    data = [{"id": n, "object": "model", "type": "model", "display_name": n, "created": 0, "created_at": "2026-01-01T00:00:00Z",
             "owned_by": "antigravity"} for n in names]
    return {"object": "list", "data": data, "has_more": False, "first_id": names[0] if names else None, "last_id": names[-1] if names else None}


@app.get("/healthz")
async def healthz():
    POOL.scan()
    return {"ok": True, "version": __version__, "accounts": len(POOL.accounts), "usable": sum(1 for a in POOL.accounts.values() if a.usable()),
            "video_mode": VIDEO_MODE}


async def read_quota(acc: Account, force: bool = False) -> Any:
    """`agy -p /usage --output-format json` (costs no quota), through the account's own proxy; cached 10 minutes.
    Returns a list of groups [{group, description, buckets: [{window, name, remaining, reset, description}]}]
    or {"error": "..."} (login expired, proxy down, ...)."""
    if not force and acc.quota is not None and time.time() - acc.quota_at < 600:
        return acc.quota
    env = {"HOME": acc.home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    env.update(read_proxy(acc.home))
    os.makedirs(os.path.join(acc.home, "gw", "workspace"), exist_ok=True)
    out = b""
    try:
        # print mode has no --gemini-dir flag; it uses $HOME/.gemini, which is the login dir
        p = await asyncio.create_subprocess_exec(AGY, "-p", "/usage", "--output-format", "json",
                                                 cwd=os.path.join(acc.home, "gw", "workspace"),
                                                 env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 60)
        data = json.loads(out.decode() or "{}")
        groups = ((data.get("command") or {}).get("data") or {}).get("groups") or []
        if not groups:
            raise ValueError(str(data.get("response") or data.get("error") or "no quota data")[:200])
        acc.quota = [{"group": g.get("name"), "description": g.get("description") or "",
                      "buckets": [{"window": b.get("window") or b.get("name"), "name": b.get("name") or "",
                                   "remaining": b.get("remaining_fraction"), "reset": b.get("reset_time"),
                                   "description": b.get("description") or ""} for b in g.get("buckets") or []]} for g in groups]
    except asyncio.TimeoutError:
        acc.quota = {"error": "timed out reading the quota (proxy down?)"}
    except Exception as e:  # noqa: BLE001
        text = out.decode(errors="replace").strip() if out else ""
        acc.quota = {"error": (str(e) if not text or text.startswith("{") else text)[:200]}
    acc.quota_at = time.time()
    return acc.quota


async def quota_cooldown(acc: Account) -> None:
    """After an upstream RESOURCE_EXHAUSTED: if the account's quota really is used up, rest it until the reset time."""
    acc.quota = None
    q = await read_quota(acc)
    if not isinstance(q, list):
        return
    for g in q:
        for b in g.get("buckets") or []:
            rem = b.get("remaining")
            if isinstance(rem, (int, float)) and rem <= 0.001:
                until = time.time() + COOLDOWN_QUOTA
                try:
                    until = datetime.fromisoformat(str(b.get("reset")).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
                acc.cooldown_until = max(acc.cooldown_until, until)
                log.warning("account %s: quota %s / %s used up, resting until %s", acc.name, g.get("group"), b.get("window"), b.get("reset"))


@app.get("/admin/status")
async def status(request: Request, quota: int = 0):
    check_key(request)
    POOL.scan()
    out = []
    for a in POOL.accounts.values():
        row = {"name": a.name, "usable": a.usable(), "inflight": a.inflight, "ok": a.ok, "fail": a.fail,
               "cooldown_s": max(0, int(a.cooldown_until - time.time())), "disabled": a.disabled_reason,
               "last_error": a.last_error, "last_error_ago_s": int(time.time() - a.last_error_at) if a.last_error_at else None,
               "acp_alive": bool(a.acp and a.acp.alive), "video_acp_alive": bool(a.media and a.media.alive)}
        if quota:
            row["quota"] = await read_quota(a)
        out.append(row)
    return {"version": __version__, "accounts": out, "per_account": POOL.limit, "waiting": POOL.waiting, "video_mode": VIDEO_MODE}


@app.post("/admin/reset/{name}")
async def reset(name: str, request: Request):
    """After a new login or a proxy change: clear cooldown / disabled flags and restart that account's ACP process."""
    check_key(request)
    a = POOL.accounts.get(name)
    if not a:
        raise HTTPException(404, "no such account")
    a.cooldown_until, a.disabled_reason = 0.0, ""
    await a.stop_processes()
    return {"ok": True}


async def _janitor() -> None:
    """`agy` stores every session's conversation on disk. Every hour, delete files older than AGY_KEEP_HOURS so
    your users' prompts and images do not pile up on the server."""
    while True:
        try:
            cutoff = time.time() - KEEP_HOURS * 3600
            removed = 0
            for a in list(POOL.accounts.values()):
                base = os.path.join(a.home, ".gemini", "antigravity-cli")
                for sub in ("conversations", "brain", "implicit", "annotations"):
                    d = os.path.join(base, sub)
                    if not os.path.isdir(d):
                        continue
                    for root, dirs, files in os.walk(d, topdown=False):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            try:
                                if os.path.getmtime(fp) < cutoff:
                                    os.remove(fp)
                                    removed += 1
                            except OSError:
                                pass
                        for dn in dirs:
                            try:
                                os.rmdir(os.path.join(root, dn))
                            except OSError:
                                pass
            if removed:
                log.info("janitor removed %d old conversation files", removed)
        except Exception as e:  # noqa: BLE001
            log.warning("janitor failed: %s", e)
        await asyncio.sleep(3600)


@app.on_event("startup")
async def _startup() -> None:
    if not API_KEY:
        raise RuntimeError("AGY_GATEWAY_KEY is not set")
    if not shutil.which(AGY) and not os.path.exists(AGY):
        raise RuntimeError("agy not found: %s (set AGY_BIN)" % AGY)
    POOL.scan()
    if VIDEO_MODE:
        for a in POOL.accounts.values():
            try:
                clear_media_leftovers(a.home)
            except OSError as e:
                log.warning("video mode: could not clear %s: %s", media_home(a.home), e)
        log.info("video mode on: video, audio and PDF go to %s through a separate read_file-only agy process per account",
                 VIDEO_MODELS.pattern)
    if KEEP_HOURS > 0:
        asyncio.create_task(_janitor())
    asyncio.create_task(_stats_saver())
    asyncio.create_task(_prewarm_all())
    event("info", "", "gateway started with %d account(s)" % len(POOL.accounts))
    log.info("ACP-Ant %s ready: %d account(s), agent %s", __version__, len(POOL.accounts), AGENT)


async def _prewarm_all() -> None:
    # in parallel: one slow account (its first start can take a minute) must not hold up the others
    await asyncio.gather(*(acc.acp.prewarm() for acc in list(POOL.accounts.values()) if acc.usable() and acc.acp))


async def _stats_saver() -> None:
    while True:
        await asyncio.sleep(60)
        STATS.save()


@app.on_event("shutdown")
async def _shutdown() -> None:
    STATS.save()
    for a in POOL.accounts.values():
        await a.stop_processes()


# Web control panel (accounts, quota, switches, login, settings): served at "/", API under /panel/api
try:
    import acp_panel  # noqa: E402
    acp_panel.setup(app, sys.modules[__name__])
except ImportError as e:   # acp_panel.py missing, or a platform without pty (the gateway itself targets Linux)
    log.warning("web panel disabled: %s", e)
