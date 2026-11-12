# -*- coding: utf-8 -*-
"""ACP-Ant web control panel.

Served by the gateway itself: GET / is the panel page (panel.html), its API lives under /panel/api/*.
Everything that used to need a shell (account.sh) can be done here: add an account (proxy test + Google login with a
paste box), re-login, change proxy, health check (with the Google verification link when needed), per-account on/off
switch, live quota per account, request statistics, connection info, a test chat, settings and an activity log.

Security: one admin password (PBKDF2, stored in $AGY_ROOT/panel.json, mode 600). On first start a random password is
written to $AGY_ROOT/panel-initial-password.txt (mode 600) and deleted once you change it in the panel. Sessions are
signed HttpOnly SameSite=Strict cookies; every state-changing call also needs the X-ACP-Panel header; failed logins are
rate limited per IP. The API key for clients (AGY_GATEWAY_KEY) is separate from the panel password.

Automatic patrol ("keep-alive"): every few hours each enabled account reads its quota (free: `agy -p /usage`), which
also refreshes the Google token and proves the proxy and the login still work. Optionally, once a day, one tiny prompt
per account surfaces "verification required" before real traffic hits it.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import pty
import re
import secrets
import select
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

core: Any = None          # the acp_ant module (set by setup)
HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "panel.html")
COOKIE = "acpant_panel"
SESSION_SECONDS = 7 * 24 * 3600
NAME_RE = re.compile(r"^[a-zA-Z0-9_@+][a-zA-Z0-9_.@+-]{0,63}$")   # e-mail addresses are fine as account names
PROXY_RE = re.compile(r"^(https?|socks5h?)://\S+:[0-9]{2,5}/?$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")
DEFAULT_SETTINGS = {"auto_check": True, "check_hours": 6, "prompt_check": False, "update_check": True}
MAX_PER_ACCOUNT = 10      # highest "requests at once per account" the panel offers
STATE: dict[str, Any] = {}
FAILS: dict[str, list[float]] = {}
LOGINS: dict[str, "LoginSession"] = {}
STARTED = time.time()
router = APIRouter()


def panel_file() -> str:
    return os.path.join(core.ROOT, "panel.json")


def init_pw_file() -> str:
    return os.path.join(core.ROOT, "panel-initial-password.txt")


# ------------------------------------------------------------------------------------------- password, sessions

def _hash(pw: str, salt: bytes | None = None, it: int = 200_000) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    return "pbkdf2_sha256$%d$%s$%s" % (it, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def _verify(pw: str, stored: str) -> bool:
    try:
        _algo, it, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def _write_private(path: str, text: str) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def save_state() -> None:
    _write_private(panel_file(), json.dumps(STATE, indent=1))


def load_state() -> None:
    try:
        with open(panel_file()) as f:
            STATE.update(json.load(f))
    except (OSError, ValueError):
        pw = os.environ.get("AGY_PANEL_PASSWORD") or secrets.token_urlsafe(12)
        STATE.update({"password": _hash(pw), "secret": secrets.token_hex(32), "settings": dict(DEFAULT_SETTINGS)})
        save_state()
        if not os.environ.get("AGY_PANEL_PASSWORD"):
            _write_private(init_pw_file(), pw + "\n")
            core.log.warning("panel: initial admin password written to %s (change it in the panel)", init_pw_file())
    STATE.setdefault("settings", {})
    for k, v in DEFAULT_SETTINGS.items():
        STATE["settings"].setdefault(k, v)
    n = STATE["settings"].get("per_account")      # set in the panel; otherwise AGY_PER_ACCOUNT from gateway.env
    if isinstance(n, int) and 1 <= n <= MAX_PER_ACCOUNT:
        core.POOL.set_limit(n)


def _token(exp: int) -> str:
    msg = "%d.%s" % (exp, secrets.token_hex(8))
    mac = hmac.new(STATE["secret"].encode(), msg.encode(), hashlib.sha256).hexdigest()
    return msg + "." + mac


def _valid(token: str) -> bool:
    try:
        exp, nonce, mac = token.split(".")
        good = hmac.new(STATE["secret"].encode(), ("%s.%s" % (exp, nonce)).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, good) and int(exp) > time.time()
    except (ValueError, AttributeError):
        return False


def _set_cookie(resp: Response, request: Request) -> None:
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(COOKIE, _token(int(time.time()) + SESSION_SECONDS), max_age=SESSION_SECONDS, httponly=True,
                    samesite="strict", secure=secure, path="/")


def require(request: Request, write: bool = False) -> None:
    if not _valid(request.cookies.get(COOKIE, "")):
        raise HTTPException(401, "请先登录")
    if write and request.headers.get("x-acp-panel") != "1":
        raise HTTPException(403, "missing X-ACP-Panel header")


def client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


# ------------------------------------------------------------------------------------------- helpers

def mask_proxy(url: str) -> str:
    """scheme://host:port (user and password removed; split at the last '@' in case the password contains one)."""
    m = re.match(r"^([a-z0-9]+://)(?:.*@)?([^@/]+)", url or "")
    return (m.group(1) + m.group(2)) if m else ""


def mask_proxy_user(url: str) -> str:
    """scheme://user@host:port without the password: tells apart proxies that share one gateway host."""
    m = re.match(r"^([a-z0-9]+)://(?:(.*)@)?([^@/]+)", url or "")
    if not m:
        return ""
    user = (m.group(2) or "").split(":", 1)[0]
    return "%s://%s%s" % (m.group(1), (user + "@") if user else "", m.group(3))


def history_file() -> str:
    return os.path.join(core.ROOT, "proxy-history.json")


def load_history() -> list[dict]:
    try:
        with open(history_file()) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_history(rows: list[dict]) -> None:
    _write_private(history_file(), json.dumps(rows[-2000:], ensure_ascii=False, indent=1))


def remember_proxy(account: str, proxy: str, ip: str, cc: str, at: float | None = None) -> None:
    """Add (account, proxy) to the proxy history if it is not there yet. Only scheme://user@host:port is kept."""
    masked = mask_proxy_user(proxy)
    if not masked:
        return
    rows = load_history()
    for r in rows:
        if r.get("account") == account and r.get("proxy") == masked and not r.get("note"):
            r.update({"ip": ip or r.get("ip", ""), "country": cc or r.get("country", ""), "last": time.time()})
            save_history(rows)
            return
    rows.append({"account": account, "proxy": masked, "ip": ip, "country": cc, "at": at or time.time(), "last": time.time(), "note": ""})
    save_history(rows)


def backfill_history() -> None:
    """Make sure every account's current proxy is in the history (accounts made before the history existed)."""
    if not os.path.isdir(core.ACCOUNTS_DIR):
        return
    for name in sorted(os.listdir(core.ACCOUNTS_DIR)):
        home = os.path.join(core.ACCOUNTS_DIR, name)
        px = core.read_proxy(home) if os.path.isdir(home) else {}
        if px:
            try:
                at = os.path.getmtime(os.path.join(home, "proxy.env"))
            except OSError:
                at = None
            remember_proxy(name, px["HTTPS_PROXY"], read_text(os.path.join(home, "egress_ip.txt")),
                           read_text(os.path.join(home, "egress_country.txt")), at)


def proxy_rows() -> list[dict]:
    """Every proxy ever used, with where it is used now and duplicate flags."""
    backfill_history()
    current = {}
    if os.path.isdir(core.ACCOUNTS_DIR):
        for name in os.listdir(core.ACCOUNTS_DIR):
            px = core.read_proxy(os.path.join(core.ACCOUNTS_DIR, name))
            if px:
                current[name] = mask_proxy_user(px["HTTPS_PROXY"])
    rows = []
    for r in load_history():
        if r.get("note"):
            status, where = "other", r["note"]
        elif r.get("account") in current:
            status = "in_use" if current[r["account"]] == r.get("proxy") else "replaced"
            where = r["account"]
        else:
            status, where = "deleted", r.get("account", "")
        rows.append({**r, "status": status, "where": where})
    # duplicates: one egress IP (or one proxy) used by more than one account / place
    holders: dict[str, set] = {}
    for r in rows:
        for key in filter(None, ("ip:" + r.get("ip", "") if r.get("ip") else "", "px:" + r.get("proxy", ""))):
            holders.setdefault(key, set()).add(r["where"])
    for r in rows:
        others = set()
        for key in filter(None, ("ip:" + r.get("ip", "") if r.get("ip") else "", "px:" + r.get("proxy", ""))):
            others |= holders.get(key, set()) - {r["where"]}
        r["shared_with"] = sorted(others)
    order = {"in_use": 0, "replaced": 1, "deleted": 2, "other": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r.get("where", ""), -r.get("last", 0)))
    return rows


def read_text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def account_env(home: str) -> dict[str, str]:
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "xterm",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    px = core.read_proxy(home)
    if px:
        env.update(px)
        env.update({"https_proxy": px["HTTPS_PROXY"], "http_proxy": px["HTTP_PROXY"]})
    return env


async def run(argv: list[str], env: dict | None = None, cwd: str | None = None, timeout: float = 60) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*argv, env=env, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return -1, "timed out after %ds" % timeout
    return p.returncode, out.decode(errors="replace")


async def body_of(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except ValueError:
        return {}


def get_account(name: str):
    core.POOL.scan()
    acc = core.POOL.accounts.get(name)
    if not acc:
        raise HTTPException(404, "没有这个账号：%s" % name)
    return acc


def home_of(name: str) -> str:
    if not NAME_RE.match(name or "") or ".." in name:
        raise HTTPException(400, "名字只能用字母、数字和 - _ . @ +（最长 64 个字符），可以直接用 Google 邮箱")
    return os.path.join(core.ACCOUNTS_DIR, name)


async def proxy_test(proxy: str) -> tuple[str, str]:
    code, ip = await run(["curl", "-s", "--max-time", "20", "-x", proxy, "https://api.ipify.org"], timeout=30)
    ip = ip.strip()
    if code != 0 or not re.match(r"^[0-9a-fA-F.:]{3,45}$", ip):
        raise HTTPException(400, "代理连不上（%s），没有保存" % mask_proxy(proxy))
    _c, cc = await run(["curl", "-s", "--max-time", "15", "-x", proxy, "https://ipinfo.io/%s/country" % ip], timeout=25)
    cc = cc.strip() if re.match(r"^[A-Z]{2}\s*$", cc.strip() or "") else ""
    return ip, cc


def save_proxy(home: str, proxy: str, ip: str, cc: str) -> list[str]:
    """Save the account's proxy; returns where else this proxy / egress IP has been used (current, past, other servers)."""
    name = os.path.basename(home.rstrip("/"))
    os.makedirs(home, exist_ok=True)
    os.chmod(home, 0o700)
    old = core.read_proxy(home)
    if old:   # keep the proxy being replaced in the history
        remember_proxy(name, old["HTTPS_PROXY"], read_text(os.path.join(home, "egress_ip.txt")), read_text(os.path.join(home, "egress_country.txt")))
    _write_private(os.path.join(home, "proxy.env"), "HTTPS_PROXY=%s\nHTTP_PROXY=%s\n" % (shlex.quote(proxy), shlex.quote(proxy)))
    with open(os.path.join(home, "egress_ip.txt"), "w") as f:
        f.write(ip + "\n")
    with open(os.path.join(home, "egress_country.txt"), "w") as f:
        f.write(cc + "\n")
    masked = mask_proxy_user(proxy)
    same = sorted({r["where"] for r in proxy_rows() if r["where"] != name and (r.get("ip") == ip or r.get("proxy") == masked)})
    remember_proxy(name, proxy, ip, cc)
    return same


async def logged_in(home: str) -> tuple[bool, str]:
    os.makedirs(os.path.join(home, "workspace"), exist_ok=True)
    _c, out = await run([core.AGY, "models"], env=account_env(home), cwd=os.path.join(home, "workspace"), timeout=60)
    ok = "gemini" in out.lower() and "sign in" not in out.lower()
    return ok, out


def _set_enabled(home: str, enabled: bool) -> None:
    flag = os.path.join(home, "disabled")
    if enabled:
        if os.path.exists(flag):
            os.remove(flag)
    else:
        with open(flag, "w") as f:
            f.write("switched off in the panel %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))


async def restart_acp(acc) -> None:
    acc.cooldown_until, acc.cooldown_kind, acc.disabled_reason = 0.0, "", ""
    await acc.stop_processes()     # the video-mode process (if any) starts again with its next request
    if acc.acp and acc.enabled:
        asyncio.create_task(acc.acp.prewarm())


AUTH_WORDS = re.compile(r"sign in|not logged in|unauthenticated|authentication required|invalid_grant|needs a new login", re.I)


async def check_account(acc) -> dict:
    """Send one tiny prompt through the account's own proxy (like `account.sh check`)."""
    t0 = time.time()
    home = acc.home
    os.makedirs(os.path.join(home, "workspace"), exist_ok=True)
    _c, out = await run([core.AGY, "-p", "Reply with exactly one word: OK", "--output-format", "json"],
                        env=account_env(home), cwd=os.path.join(home, "workspace"), timeout=120)
    acc.last_check_at, acc.verify_url = time.time(), ""
    setattr(acc, "last_prompt_check", time.time())
    low = out.lower()
    res: dict[str, Any] = {"ok": False}
    try:
        answer = str(json.loads(out.strip()).get("response") or "")
    except ValueError:
        answer = ""
    soft = core.soft_error(answer) if answer else None   # agy says SUCCESS even when the "answer" is a ⚠ status line
    if soft is not None and soft.kind == "LICENSE":
        acc.disabled_reason = "no valid Antigravity licence (#3501)"
        res = {"ok": False, "message": "这个号没有 Antigravity 使用许可（#3501）：重新登录试试，不行就换号"}
    elif soft is not None and soft.kind != "VERIFY":
        res = {"ok": False, "message": "出错了：" + ANSI.sub("", answer)[:240]}
    elif re.search(r'"status"\s*:\s*"SUCCESS"', out) and soft is None:
        res = {"ok": True, "message": "正常（%.0f 秒）" % (time.time() - t0)}
        if acc.cooldown_kind == "VERIFY" or acc.disabled_reason:
            await restart_acp(acc)
    elif "verify your account" in low or "verification required" in low:
        m = re.search(r'https://accounts\.google\.com/v3/signin/continue[^"\s\\]*', out)
        acc.verify_url = (m.group(0).replace("\\u0026", "&") if m else "")
        acc.cooldown_until, acc.cooldown_kind = time.time() + core.COOLDOWN_VERIFY, "VERIFY"
        res = {"ok": False, "verify_url": acc.verify_url, "message": "Google 要求验证这个账号"}
    elif AUTH_WORDS.search(out):
        acc.disabled_reason = "需要重新登录"
        res = {"ok": False, "message": "登录已失效，需要重新登录"}
    elif re.search(r"resource.?exhausted|quota|429", low):
        res = {"ok": False, "message": "额度用完或被限流（稍后会自动恢复）"}
    else:
        text = out.strip()
        try:
            text = str(json.loads(text).get("response") or text)
        except ValueError:
            pass
        res = {"ok": False, "message": "出错了：" + ANSI.sub("", text)[:240]}
    acc.last_check_ok, acc.last_check_msg = res["ok"], res["message"]
    core.event("info" if res["ok"] else "warn", acc.name, "检测：" + res["message"])
    return res


async def quota_check(acc, record: bool = True) -> Any:
    q = await core.read_quota(acc, force=True)
    if record:
        acc.last_check_at = time.time()
        if isinstance(q, list):
            acc.last_check_ok, acc.last_check_msg = True, "额度读取正常"
        else:
            err = str((q or {}).get("error", ""))
            acc.last_check_ok, acc.last_check_msg = False, "读取额度失败：" + err[:160]
            if AUTH_WORDS.search(err):
                acc.disabled_reason = "需要重新登录"
                core.event("warn", acc.name, "巡检发现登录已失效")
    return q


# ------------------------------------------------------------------------------------------- Google login in the browser

class LoginSession:
    """Runs `agy acp --login` under a pseudo-terminal: we show the Google link it prints, the owner pastes back the
    code (or the URL of the page Google lands on) and we type it in. Credentials never pass through the panel."""

    def __init__(self, name: str, home: str):
        self.name, self.home = name, home
        self.buf, self.url, self.done, self.started = "", "", False, time.time()
        for d in (".gemini", "workspace"):
            os.makedirs(os.path.join(home, d), exist_ok=True)
        master, slave = pty.openpty()
        self.proc = subprocess.Popen([core.AGY, "acp", "--login", "--gemini-dir", os.path.join(home, ".gemini"), "--disable-telemetry"],
                                     cwd=os.path.join(home, "workspace"), stdin=slave, stdout=slave, stderr=slave,
                                     env=account_env(home), close_fds=True, start_new_session=True)
        os.close(slave)
        self.fd = master
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                ready, _, _ = select.select([self.fd], [], [], 1.0)
            except (OSError, ValueError):
                break
            if ready:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self.buf = (self.buf + ANSI.sub("", chunk.decode(errors="replace")))[-20000:]
                if not self.url:
                    m = re.search(r"https://accounts\.google\.com/\S+", self.buf)
                    if m:
                        self.url = m.group(0)
            elif self.proc.poll() is not None:
                break
        self.done = True

    def send(self, code: str) -> None:
        os.write(self.fd, (code.strip() + "\r").encode())

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def drop_login(name: str) -> None:
    s = LOGINS.pop(name, None)
    if s:
        s.close()


# ------------------------------------------------------------------------------------------- login with a refresh_token

TOKEN_FILE = os.path.join(".gemini", "antigravity-cli", "antigravity-oauth-token")   # where `agy` keeps its login
RT_RE = re.compile(r"1//[0-9A-Za-z_\-]{20,500}")
TOKEN_LOCK = asyncio.Lock()


def token_path(home: str) -> str:
    return os.path.join(home, TOKEN_FILE)


def _find_key(obj: Any, keys: tuple[str, ...], depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(obj, dict):
        out = [v for k, v in obj.items() if k in keys and isinstance(v, str)]
        for v in obj.values():
            out += _find_key(v, keys, depth + 1)
        return out
    if isinstance(obj, list):
        return [x for v in obj[:200] for x in _find_key(v, keys, depth + 1)]
    return []


def parse_refresh_token(text: str) -> tuple[str, str]:
    """The owner may paste the bare token, a JSON blob that contains it (an Antigravity token file, an account-manager
    export) or a line like refresh_token=... Returns (token, "") or ("", reason)."""
    text = (text or "").strip()
    if not text:
        return "", "请粘贴 refresh_token"
    if len(text) > 50000:
        return "", "内容太长了，只粘贴一个账号的 refresh_token"
    try:
        found = [t.strip() for t in _find_key(json.loads(text), ("refresh_token", "refreshToken"))]
    except ValueError:
        found = []
    found = [t for t in found if RT_RE.fullmatch(t)] or RT_RE.findall(text)
    uniq = list(dict.fromkeys(found))
    if not uniq:
        return "", "这看起来不是 Google 的 refresh_token（一般以 1// 开头，只含字母、数字、- 和 _）"
    if len(uniq) > 1:
        return "", "里面有 %d 个不同的 refresh_token，一次只能给一个账号登录" % len(uniq)
    return uniq[0], ""


def token_body(rt: str) -> str:
    """Same shape `agy` writes after a browser login. The empty, long-expired access token makes `agy` exchange the
    refresh token right away; it then fills in the access token and the account's e-mail itself."""
    return json.dumps({"token": {"access_token": "", "token_type": "Bearer", "refresh_token": rt,
                                 "expiry": "2000-01-01T00:00:00Z"}, "auth_method": "consumer"})


def token_email(home: str) -> str:
    try:
        with open(token_path(home)) as f:
            return str(json.load(f).get("id_token_email") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def accounts_with_email(email: str, skip: str) -> list[str]:
    if not email:
        return []
    try:
        names = sorted(os.listdir(core.ACCOUNTS_DIR))
    except OSError:
        return []
    return [n for n in names if n != skip and token_email(os.path.join(core.ACCOUNTS_DIR, n)).lower() == email.lower()]


# ------------------------------------------------------------------------------------------- overview

def status_of(acc) -> tuple[str, str]:
    now = time.time()
    if not acc.enabled:
        return "off", "已关闭"
    if acc.disabled_reason:
        r = acc.disabled_reason
        if "#3501" in r:
            return "error", "无使用许可（#3501）"
        return "error", "需要重新登录" if (AUTH_WORDS.search(r) or "登录" in r or "login" in r.lower()) else r[:40]
    if acc.cooldown_until > now:
        return {"VERIFY": ("verify", "需要 Google 验证"), "QUOTA": ("quota", "额度用完，冷却中"),
                "RATE_LIMIT": ("rate", "被限流，冷却中")}.get(acc.cooldown_kind, ("rate", "冷却中"))
    if acc.verify_url:
        return "verify", "需要 Google 验证"
    if acc.last_check_ok is False:
        return "warn", "上次检测有问题"
    return "ok", "正常"


def account_view(acc) -> dict:
    code, text = status_of(acc)
    px = core.read_proxy(acc.home)
    q = acc.quota
    return {
        "name": acc.name, "enabled": acc.enabled, "usable": acc.usable(), "status": code, "status_text": text,
        "cooldown_s": max(0, int(acc.cooldown_until - time.time())), "inflight": acc.inflight,
        "since_start": {"ok": acc.ok, "fail": acc.fail},
        "today": core.STATS.summary(acc.name, 1), "week": core.STATS.summary(acc.name, 7),
        "last_error": acc.last_error, "last_error_at": acc.last_error_at or None,
        "proxy": mask_proxy_user(px.get("HTTPS_PROXY", "")) if px else "",
        "egress_ip": read_text(os.path.join(acc.home, "egress_ip.txt")),
        "country": read_text(os.path.join(acc.home, "egress_country.txt")),
        "quota": q if isinstance(q, list) else None, "quota_error": q.get("error") if isinstance(q, dict) else None,
        "quota_at": acc.quota_at or None,
        "last_check_at": acc.last_check_at or None, "last_check_ok": acc.last_check_ok, "last_check_msg": acc.last_check_msg,
        "verify_url": acc.verify_url, "login_active": acc.name in LOGINS,
    }


# ------------------------------------------------------------------------------------------- routes

@router.post("/panel/api/login")
async def api_login(request: Request):
    ip = client_ip(request)
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    if len(FAILS[ip]) >= 8:
        raise HTTPException(429, "密码错误次数太多，请 10 分钟后再试")
    body = await body_of(request)
    if not _verify(str(body.get("password") or ""), STATE["password"]):
        FAILS[ip].append(now)
        core.event("warn", "", "面板登录失败（%s）" % ip)
        await asyncio.sleep(1)
        raise HTTPException(401, "密码不对")
    FAILS.pop(ip, None)
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, request)
    return resp


@router.post("/panel/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/panel/api/overview")
async def api_overview(request: Request):
    require(request)
    core.POOL.scan()
    accs = [account_view(a) for a in core.POOL.accounts.values()]
    today = {"ok": 0, "fail": 0, "in": 0, "out": 0}
    for a in accs:
        for k in today:
            today[k] += a["today"].get(k, 0)
    return {"server": {"started": STARTED, "now": time.time(), "accounts": len(accs), "usable": sum(1 for a in accs if a["usable"]),
                       "inflight": sum(a["inflight"] for a in accs), "waiting": core.POOL.waiting,
                       "per_account": core.POOL.limit, "per_account_default": core.PER_ACCOUNT, "require_proxy": core.REQUIRE_PROXY,
                       "agy": core.AGY, "initial_password": os.path.exists(init_pw_file()), "version": core.__version__},
            "update": update_view(),
            "today": today, "accounts": accs, "settings": STATE["settings"]}


@router.post("/panel/api/accounts")
async def api_create(request: Request):
    require(request, True)
    body = await body_of(request)
    name = str(body.get("name") or "").strip()
    home = home_of(name)
    if os.path.exists(home):
        raise HTTPException(400, "账号 %s 已经存在" % name)
    proxy = str(body.get("proxy") or "").strip()
    same: list[str] = []
    if proxy:
        if not PROXY_RE.match(proxy):
            raise HTTPException(400, "代理格式不对，需要 协议://[用户:密码@]主机:端口，例如 socks5://user:pass@1.2.3.4:1080")
        ip, cc = await proxy_test(proxy)
        os.makedirs(home, exist_ok=True)
        _set_enabled(home, False)            # switched off until the Google login and a check succeed
        same = save_proxy(home, proxy, ip, cc)
    elif core.REQUIRE_PROXY:
        raise HTTPException(400, "这个网关要求每个账号都有代理（AGY_REQUIRE_PROXY=1），请填代理地址")
    else:
        os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
        os.chmod(home, 0o700)
        _set_enabled(home, False)            # switched off until the login and a check succeed
        ip, cc = "", ""
    for d in ("workspace", ".gemini"):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    core.event("info", name, "新建账号（%s）" % (("代理 %s，出口 %s %s" % (mask_proxy(proxy), ip, cc)) if proxy else "不用代理"))
    return {"ok": True, "egress_ip": ip, "country": cc, "same_ip_as": same}


@router.post("/panel/api/accounts/{name}/enabled")
async def api_enabled(name: str, request: Request):
    require(request, True)
    acc = get_account(name)
    on = bool((await body_of(request)).get("enabled"))
    _set_enabled(acc.home, on)
    acc.enabled = on
    if on:
        core.POOL.wake()     # requests queued for a free slot can use it right away
    core.event("info", name, "已开启" if on else "已关闭（不再被调用）")
    return {"ok": True, "enabled": on}


@router.post("/panel/api/accounts/{name}/quota")
async def api_quota(name: str, request: Request):
    require(request, True)
    acc = get_account(name)
    await quota_check(acc)
    return account_view(acc)


@router.post("/panel/api/quota-all")
async def api_quota_all(request: Request):
    require(request, True)
    core.POOL.scan()
    await asyncio.gather(*(quota_check(a) for a in core.POOL.accounts.values()), return_exceptions=True)
    return {"ok": True}


@router.post("/panel/api/accounts/{name}/check")
async def api_check(name: str, request: Request):
    require(request, True)
    acc = get_account(name)
    body = await body_of(request)
    # ponytail: the login flow already validates credentials; quota is an explicit action, not an add-account gate.
    # Keep old panel pages (enable_if_ok + light) on the same no-quota path.
    if body.get("enable_if_ok"):
        ok, _ = await logged_in(acc.home)
        res = {"ok": ok, "message": "登录正常，额度可在账号卡片上手动刷新" if ok else "登录尚未完成，请重新登录"}
        if ok:
            acc.last_check_at = time.time()     # defer the regular patrol until its normal interval
            acc.last_check_ok, acc.last_check_msg = True, res["message"]
        core.event("info" if ok else "warn", name, "添加账号：" + res["message"])
    elif body.get("light"):
        # Explicit lightweight check; never used to gate adding an account.
        t0 = time.time()
        q = await quota_check(acc)
        if isinstance(q, list):
            res = {"ok": True, "message": "登录正常，读到额度（%.0f 秒，没有发测试消息）" % (time.time() - t0)}
        else:
            res = {"ok": False, "message": acc.last_check_msg or "读取额度失败"}
        core.event("info" if res["ok"] else "warn", name, "检测（只读额度）：" + res["message"])
    else:
        res = await check_account(acc)
    if res["ok"] and not acc.enabled and body.get("enable_if_ok"):
        _set_enabled(acc.home, True)
        acc.enabled = True
        core.POOL.wake()
        core.event("info", name, "登录确认，已开启")
    return {**res, "account": account_view(acc)}


@router.post("/panel/api/accounts/{name}/reset")
async def api_reset(name: str, request: Request):
    require(request, True)
    acc = get_account(name)
    acc.verify_url, acc.last_check_ok, acc.last_check_msg = "", None, ""
    await restart_acp(acc)
    core.event("info", name, "清除冷却 / 错误标记，重启了这个账号的进程")
    return account_view(acc)


@router.post("/panel/api/accounts/{name}/proxy")
async def api_proxy(name: str, request: Request):
    require(request, True)
    home = home_of(name)
    if not os.path.isdir(home):
        raise HTTPException(404, "没有这个账号：%s" % name)
    proxy = str((await body_of(request)).get("proxy") or "").strip()
    if not PROXY_RE.match(proxy):
        raise HTTPException(400, "代理格式不对，需要 协议://[用户:密码@]主机:端口")
    ip, cc = await proxy_test(proxy)
    same = save_proxy(home, proxy, ip, cc)
    core.POOL.scan()
    acc = core.POOL.accounts.get(name)
    if acc:
        await restart_acp(acc)
        acc.quota = None
    core.event("info", name, "换了代理：%s（出口 %s %s）" % (mask_proxy(proxy), ip, cc))
    return {"ok": True, "egress_ip": ip, "country": cc, "same_ip_as": same}


@router.post("/panel/api/accounts/{name}/login/start")
async def api_login_start(name: str, request: Request):
    require(request, True)
    home = home_of(name)
    if not os.path.isdir(home):
        raise HTTPException(404, "没有这个账号：%s" % name)
    if core.REQUIRE_PROXY and not os.path.exists(os.path.join(home, "proxy.env")):
        raise HTTPException(400, "先给这个账号设置代理再登录")
    drop_login(name)
    s = LoginSession(name, home)
    LOGINS[name] = s
    for _ in range(60):
        if s.url or s.done:
            break
        await asyncio.sleep(0.5)
    if not s.url:
        tail = s.buf.strip()[-300:]
        drop_login(name)
        raise HTTPException(502, "没拿到 Google 登录链接：" + (tail or "agy 没有输出"))
    core.event("info", name, "开始 Google 登录")
    return {"ok": True, "url": s.url}


@router.post("/panel/api/accounts/{name}/login/finish")
async def api_login_finish(name: str, request: Request):
    require(request, True)
    s = LOGINS.get(name)
    if not s:
        raise HTTPException(400, "登录已经超时或没有开始，请重新点「开始登录」")
    code = str((await body_of(request)).get("code") or "").strip()
    if len(code) < 4:
        raise HTTPException(400, "请粘贴授权码，或者浏览器地址栏里的整串网址")
    before = len(s.buf)
    s.send(code)
    for _ in range(180):
        if s.done or s.proc.poll() is not None:
            break
        await asyncio.sleep(0.5)
    out = s.buf[before:].replace(code, "")
    drop_login(name)
    ok, _models = await logged_in(s.home)
    core.POOL.scan()
    acc = core.POOL.accounts.get(name)
    if ok:
        if acc:
            await restart_acp(acc)
            acc.quota, acc.verify_url = None, ""
        core.event("info", name, "Google 登录成功")
        return {"ok": True}
    msg = re.sub(r"\s+", " ", out).strip()[-300:]
    core.event("warn", name, "Google 登录没有成功")
    raise HTTPException(400, "登录没有成功：" + (msg or "请确认粘贴的是完整的网址或授权码，然后重新开始登录"))


@router.post("/panel/api/accounts/{name}/login/token")
async def api_login_token(name: str, request: Request):
    """Log an account in with a Google refresh_token instead of the browser flow. The token goes straight into the
    account's own login file (mode 600) and is never logged or sent back; `agy models` then proves it works. If it
    does not, the previous login file is put back."""
    require(request, True)
    home = home_of(name)
    if not os.path.isdir(home):
        raise HTTPException(404, "没有这个账号：%s" % name)
    if core.REQUIRE_PROXY and not os.path.exists(os.path.join(home, "proxy.env")):
        raise HTTPException(400, "先给这个账号设置代理再登录")
    rt, why = parse_refresh_token(str((await body_of(request)).get("refresh_token") or ""))
    if not rt:
        raise HTTPException(400, why)
    async with TOKEN_LOCK:
        drop_login(name)
        core.POOL.scan()
        acc = core.POOL.accounts.get(name)
        if acc:
            await acc.stop_processes()      # a running agy could write its old token back over the new one
        path = token_path(home)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            with open(path, "rb") as f:
                old: bytes | None = f.read()
        except OSError:
            old = None
        _write_private(path, token_body(rt))
        ok, out = await logged_in(home)
        if not ok:
            if old is None:
                os.remove(path)
            else:
                fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(old)
                os.replace(path + ".tmp", path)
            if acc and acc.acp and acc.enabled:
                asyncio.create_task(acc.acp.prewarm())
            core.event("warn", name, "用 refresh_token 登录没有成功")
            if "sign in" in out.lower():
                raise HTTPException(400, "这个 refresh_token 用不了：可能已经失效或被撤销，或者不是 Antigravity 登录得到的"
                                         "（Gemini CLI 等其它程序的 refresh_token 不能用）。原来的登录没有改动。")
            raise HTTPException(400, "检测没有通过（可能是代理连不上），原来的登录没有改动：" + re.sub(r"\s+", " ", out).strip()[-200:])
    email = token_email(home)
    if acc:
        await restart_acp(acc)
        acc.quota, acc.verify_url = None, ""
    core.event("info", name, "用 refresh_token 登录成功" + ("（%s）" % email if email else ""))
    return {"ok": True, "email": email, "same_email_as": accounts_with_email(email, name)}


@router.post("/panel/api/accounts/{name}/login/cancel")
async def api_login_cancel(name: str, request: Request):
    require(request, True)
    drop_login(name)
    return {"ok": True}


@router.delete("/panel/api/accounts/{name}")
async def api_delete(name: str, request: Request):
    require(request, True)
    home = home_of(name)
    if not os.path.isdir(home):
        raise HTTPException(404, "没有这个账号：%s" % name)
    drop_login(name)
    acc = core.POOL.accounts.pop(name, None)
    if acc:
        await acc.stop_processes()
    px = core.read_proxy(home)
    if px:   # the history keeps proxies of deleted accounts too
        remember_proxy(name, px["HTTPS_PROXY"], read_text(os.path.join(home, "egress_ip.txt")), read_text(os.path.join(home, "egress_country.txt")))
    shutil.rmtree(home, ignore_errors=True)
    core.event("info", name, "已删除账号（含登录凭据）")
    return {"ok": True}


@router.get("/panel/api/connect")
async def api_connect(request: Request):
    require(request)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "127.0.0.1:8095"
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    base = "%s://%s" % (proto, host)
    names = list(core.MODEL_ALIASES)
    for m in core.POOL.models_seen:
        if m not in names:
            names.append(m)
    return {"base": base, "openai": base + "/v1", "anthropic": base, "key": core.API_KEY, "models": names,
            "default_model": core.DEFAULT_MODEL}


@router.post("/panel/api/test")
async def api_test(request: Request):
    require(request, True)
    body = await body_of(request)
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "请输入要测试的内容")
    model = str(body.get("model") or core.DEFAULT_MODEL)
    system, blocks = core.to_acp("", [{"role": "user", "parts": [{"type": "text", "text": prompt}]}], [], "auto")
    t0 = time.time()
    parts, account = [], ""
    try:
        async for kind, text, acc in core.generate(model, system, blocks):
            account = acc
            if kind == "text":
                parts.append(text)
    except core.AcpError as e:
        return {"ok": False, "error": "%s：%s" % (e.kind, e), "seconds": round(time.time() - t0, 1)}
    return {"ok": True, "text": "".join(parts), "account": account, "seconds": round(time.time() - t0, 1), "model": model}


@router.get("/panel/api/proxies")
async def api_proxies(request: Request):
    require(request)
    return {"proxies": proxy_rows()}


@router.get("/panel/api/events")
async def api_events(request: Request):
    require(request)
    return {"events": list(core.EVENTS)[-200:][::-1]}


@router.post("/panel/api/settings")
async def api_settings(request: Request):
    require(request, True)
    body = await body_of(request)
    s = STATE["settings"]
    if "auto_check" in body:
        s["auto_check"] = bool(body["auto_check"])
    if "check_hours" in body:
        h = int(body["check_hours"])
        if h not in (1, 2, 3, 6, 12, 24):
            raise HTTPException(400, "巡检间隔只能是 1 / 2 / 3 / 6 / 12 / 24 小时")
        s["check_hours"] = h
    if "prompt_check" in body:
        s["prompt_check"] = bool(body["prompt_check"])
    if "update_check" in body:
        s["update_check"] = bool(body["update_check"])
    if "per_account" in body:
        try:
            n = int(body["per_account"])
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= MAX_PER_ACCOUNT:
            raise HTTPException(400, "每个账号同时请求数只能是 1–%d" % MAX_PER_ACCOUNT)
        s["per_account"] = n
        core.POOL.set_limit(n)
    save_state()
    core.event("info", "", "修改了设置：%s" % json.dumps(s, ensure_ascii=False))
    return {"ok": True, "settings": s}


@router.post("/panel/api/password")
async def api_password(request: Request):
    require(request, True)
    body = await body_of(request)
    if not _verify(str(body.get("old") or ""), STATE["password"]):
        raise HTTPException(400, "原密码不对")
    new = str(body.get("new") or "")
    if len(new) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    STATE["password"], STATE["secret"] = _hash(new), secrets.token_hex(32)   # new secret = sign out everywhere else
    save_state()
    try:
        os.remove(init_pw_file())
    except OSError:
        pass
    core.event("info", "", "修改了面板密码")
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, request)
    return resp


async def page():
    return FileResponse(HTML_FILE, media_type="text/html", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------------------------------- updates
# Every few hours read CHANGELOG.md from the GitHub repo (one small public file, nothing is sent). A newer version
# shows a banner in the panel. Installs made by install.sh from a git clone, running as a systemd service, can update
# in one click: update.sh (git pull --ff-only + install.sh --update), then the gateway exits and systemd restarts it.

UPDATE_REPO = os.environ.get("ACPANT_UPDATE_REPO", "czg86389-hub/ACP-Ant")
UPDATE_BRANCH = os.environ.get("ACPANT_UPDATE_BRANCH", "main")
UPDATE_URL = (os.environ.get("ACPANT_UPDATE_URL") or "https://raw.githubusercontent.com/%s/%s" % (UPDATE_REPO, UPDATE_BRANCH)).rstrip("/")
try:
    UPDATE_HOURS = float(os.environ.get("ACPANT_UPDATE_CHECK_HOURS", "6") or 0)   # 0 = never check
except ValueError:
    UPDATE_HOURS = 6.0
_BG: set = set()
UPDATE: dict[str, Any] = {"latest": None, "checked_at": None, "error": "", "notes": [], "state": "idle", "log": "", "started_at": None}


def _vtuple(v: Any) -> tuple[int, int, int]:
    m = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def parse_changelog(text: str) -> list[dict]:
    """'## 1.2.0 — 2026-11-09' headings with '- ...' lines below -> [{version, title, lines}]."""
    out: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = re.match(r"^##\s+v?(\d+\.\d+\.\d+)\s*(.*)$", line)
        if m:
            cur = {"version": m.group(1), "title": m.group(2).strip(" -—–·"), "lines": []}
            out.append(cur)
        elif cur is not None and line.strip() and not line.startswith("#"):
            cur["lines"].append(line.rstrip()[:400])
    return out


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ACP-Ant/%s (update check)" % core.__version__, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(300_000).decode("utf-8", "replace")


def install_info() -> dict:
    if os.environ.get("ACPANT_INSTALL") == "docker":
        return {"mode": "docker"}
    try:
        with open(os.path.join(core.ROOT, "install-info.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {"mode": "manual"}
    except (OSError, ValueError):
        return {"mode": "manual"}


def can_apply(info: dict) -> tuple[bool, str]:
    """One-click update needs: install.sh install from a git clone, update.sh in place, and systemd to restart us."""
    mode = info.get("mode")
    if mode == "docker":
        return False, "docker"
    if mode not in ("system", "user"):
        return False, "manual"
    src = str(info.get("src") or "")
    if not src or not os.path.isdir(os.path.join(src, ".git")):
        return False, "nogit"
    if not os.path.exists(os.path.join(core.ROOT, "update.sh")) or not shutil.which("git"):
        return False, "manual"
    if not os.environ.get("INVOCATION_ID"):     # set by systemd for its services; without it nobody would restart us
        return False, "nosystemd"
    return True, ""


def update_view(with_log: bool = False) -> dict:
    info = install_info()
    ok, why = can_apply(info)
    latest = UPDATE["latest"] or core.__version__
    d = {"current": core.__version__, "latest": latest, "available": False,
         "checked_at": UPDATE["checked_at"], "error": UPDATE["error"], "notes": UPDATE["notes"], "repo": UPDATE_REPO,
         "auto": bool(STATE.get("settings", {}).get("update_check", True)) and UPDATE_HOURS > 0, "every_hours": UPDATE_HOURS,
         "mode": info.get("mode"), "src": info.get("src") or "", "can_apply": ok, "why": why, "state": "idle"}
    if with_log:
        d["log"] = UPDATE["log"][-6000:]
    return d


def _silent_sync_files_sync() -> bool:
    """后台静默同步远端最新 panel.html 与 README.md（不通知、不中断服务），并检测 git 仓库是否有新提交。"""
    info = install_info()
    src = str(info.get("src") or "")
    # ponytail: unmanaged installs keep local fixes; use a managed git install for automatic file sync.
    if not src:
        return False
    for fname in ("panel.html", "README.md"):
        try:
            remote_txt = _fetch_text(UPDATE_URL + "/" + fname)
            if not remote_txt or (fname == "panel.html" and "<!DOCTYPE html>" not in remote_txt):
                continue
            targets = [os.path.join(HERE, fname)]
            if src and os.path.isdir(src):
                src_f = os.path.join(src, fname)
                if src_f not in targets:
                    targets.append(src_f)
            for tpath in targets:
                try:
                    cur_txt = ""
                    if os.path.isfile(tpath):
                        with open(tpath, encoding="utf-8") as f:
                            cur_txt = f.read()
                    if cur_txt != remote_txt:
                        tmp = tpath + ".tmp"
                        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                            f.write(remote_txt)
                        os.replace(tmp, tpath)
                except OSError:
                    pass
        except Exception:
            pass
    if src and os.path.isdir(os.path.join(src, ".git")) and shutil.which("git"):
        try:
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            loc = subprocess.run(["git", "-C", src, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, env=env)
            rem = subprocess.run(["git", "-C", src, "ls-remote", "origin", f"refs/heads/{UPDATE_BRANCH}"], capture_output=True, text=True, timeout=15, env=env)
            if loc.returncode == 0 and rem.returncode == 0:
                l_sha = loc.stdout.strip()
                r_sha = (rem.stdout.strip().split() or [""])[0]
                if l_sha and r_sha and l_sha != r_sha:
                    return True
        except Exception:
            pass
    return False


async def check_update() -> dict:
    try:
        git_has_new = await asyncio.to_thread(_silent_sync_files_sync)
        secs = parse_changelog(await asyncio.to_thread(_fetch_text, UPDATE_URL + "/CHANGELOG.md"))
        if not secs:
            raise ValueError("CHANGELOG.md 里没有版本号")
        latest = max(secs, key=lambda s: _vtuple(s["version"]))["version"]
        cur = _vtuple(core.__version__)
        newer = sorted([s for s in secs if _vtuple(s["version"]) > cur], key=lambda s: _vtuple(s["version"]), reverse=True)
        UPDATE.update(latest=latest, error="", notes=newer[:10])
        if (_vtuple(latest) > cur or git_has_new) and UPDATE["state"] not in ("running", "restarting"):
            info = install_info()
            ok, _ = can_apply(info)
            if ok:
                UPDATE.update(state="running", log="", started_at=time.time())
                t = asyncio.create_task(_run_update(str(info["src"]), silent=True))
                _BG.add(t)
                t.add_done_callback(_BG.discard)
    except Exception as e:  # noqa: BLE001
        UPDATE["error"] = ("检查失败：%s" % e)[:200]
    UPDATE["checked_at"] = time.time()
    return update_view()


async def _run_update(src: str, silent: bool = True) -> None:
    env = dict(os.environ, AGY_ROOT=core.ROOT, GIT_TERMINAL_PROMPT="0", LANG="C.UTF-8")
    rc = -1
    try:
        p = await asyncio.create_subprocess_exec("bash", os.path.join(core.ROOT, "update.sh"), src, cwd=src, env=env,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        async def pump() -> int:
            assert p.stdout
            async for line in p.stdout:
                UPDATE["log"] = (UPDATE["log"] + line.decode("utf-8", "replace"))[-20000:]
            return await p.wait()
        try:
            rc = await asyncio.wait_for(pump(), 900)
        except asyncio.TimeoutError:
            p.kill()
            UPDATE["log"] += "\n更新超过 15 分钟，已停止。\n"
    except Exception as e:  # noqa: BLE001
        UPDATE["log"] += "\n%s\n" % e
    if rc != 0:
        UPDATE["state"] = "idle" if silent else "failed"
        if not silent:
            core.event("warn", "", "更新失败，网关继续用当前版本 %s（面板里能看到出错信息）" % core.__version__)
        return
    UPDATE["state"] = "restarting"
    UPDATE["log"] += "\n==> 文件已更新。等正在处理的请求做完，网关自动重启…\n"
    if not silent:
        core.event("info", "", "新版本文件已就位，网关马上重启")
    for _ in range(180):     # give running requests up to 3 minutes
        if not core.POOL.waiting and not any(a.inflight for a in core.POOL.accounts.values()):
            break
        await asyncio.sleep(1)
    await asyncio.sleep(1)
    os.kill(os.getpid(), signal.SIGTERM)       # systemd (Restart=always) starts the new version


@router.get("/panel/api/update")
async def api_update(request: Request):
    require(request)
    return update_view(with_log=True)


@router.post("/panel/api/update/check")
async def api_update_check(request: Request):
    require(request, True)
    return await check_update()


@router.post("/panel/api/update/apply")
async def api_update_apply(request: Request):
    require(request, True)
    info = install_info()
    ok, why = can_apply(info)
    if not ok:
        raise HTTPException(400, "这台不能在面板里一键更新（%s），请按面板上的命令手动更新" % why)
    if UPDATE["state"] in ("running", "restarting"):
        raise HTTPException(409, "正在更新，请稍等")
    UPDATE.update(state="running", log="", started_at=time.time())
    t = asyncio.create_task(_run_update(str(info["src"]), silent=True))
    _BG.add(t)
    t.add_done_callback(_BG.discard)
    return update_view(with_log=True)


async def update_loop() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await check_update()
        except Exception:
            pass
        await asyncio.sleep(max(0.25, UPDATE_HOURS or 6.0) * 3600)


# ------------------------------------------------------------------------------------------- patrol (keep-alive)

async def patrol() -> None:
    """Every 5 minutes: enabled accounts whose last check is older than the interval read their quota (free; refreshes
    the Google token, proves proxy + login). Optional daily prompt check. Expired login sessions are cleaned up."""
    await asyncio.sleep(90)
    while True:
        try:
            for name in [n for n, s in LOGINS.items() if time.time() - s.started > 900]:
                drop_login(name)
            st = STATE["settings"]
            if st.get("auto_check"):
                core.POOL.scan()
                for acc in list(core.POOL.accounts.values()):
                    if not acc.enabled or acc.name in LOGINS:
                        continue
                    if time.time() - acc.last_check_at > st["check_hours"] * 3600:
                        await quota_check(acc)
                    if st.get("prompt_check") and time.time() - getattr(acc, "last_prompt_check", 0) > 24 * 3600:
                        await check_account(acc)
        except Exception as e:  # noqa: BLE001
            core.log.warning("patrol failed: %s", e)
        await asyncio.sleep(300)


def setup(app, module) -> None:
    global core
    core = module
    load_state()
    try:
        backfill_history()
    except Exception as e:  # noqa: BLE001
        core.log.warning("proxy history backfill failed: %s", e)
    app.include_router(router)
    app.add_api_route("/", page, methods=["GET"], include_in_schema=False)
    app.add_api_route("/panel", page, methods=["GET"], include_in_schema=False)

    @app.on_event("startup")
    async def _panel_startup() -> None:
        asyncio.create_task(patrol())
        asyncio.create_task(update_loop())

    @app.on_event("shutdown")
    async def _panel_shutdown() -> None:
        for name in list(LOGINS):
            drop_login(name)
