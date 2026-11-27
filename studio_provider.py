"""AI Studio as ACP-Ant's second source, through ACP-Ant's OWN AIStudio2API instance (github.com/Mag1cFall/AIStudio2API).
Accounts are AIStudio2API accounts (id = verified Google e-mail); they are signed in with
AIStudio2API's isolated server-side browser, whose sessions are not bound to any PC.

Server-owned configuration only (ACPANT_STUDIO_CONFIG, JSON, mode 600):
  {"base_url": "http://127.0.0.1:2051", "api_key": "<PROXY_API_KEY>", "admin_password": "<ADMIN_PASSWORD>"}
Nothing secret (keys, cookies, proxy credentials) is ever returned to callers or logged.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import anyio
from fastapi.responses import JSONResponse, Response, StreamingResponse

log = logging.getLogger('acp-ant.studio')
PREFIX = 'aistudio/'
PATHS = ('/v1/chat/completions', '/v1/responses', '/v1/messages', '/v1/messages/count_tokens')
LIVE_STATES = ('ready', 'busy', 'cooldown')
NOT_CONFIGURED = 'AI Studio 未启用：在服务器上运行 aistudio.sh setup（见 README「AI Studio 第二路」）'
DENIED = re.compile(r'PERMISSION_DENIED|Permission denied|权限拒绝|(?:错误码|error code) 7\b')
HEAL_GAP = 600       # seconds: one automatic page restart per account at most this often
HEAL_TRIES = 3       # restarts in a row that did not help -> leave the account alone for HEAL_PAUSE
HEAL_PAUSE = 3600
LOCALES = {'US': 'en-US', 'GB': 'en-GB', 'CA': 'en-CA', 'AU': 'en-AU', 'SG': 'en-SG', 'IN': 'en-IN', 'JP': 'ja-JP',
           'DE': 'de-DE', 'FR': 'fr-FR', 'NL': 'nl-NL', 'TW': 'zh-TW', 'HK': 'zh-HK', 'KR': 'ko-KR'}


DEFAULT_TIMEOUT = int(os.environ.get('AGY_REQUEST_TIMEOUT') or 290)
_LEVEL_RE = re.compile(r'^(.+?)-(xhigh|high|medium|low)$', re.I)
_GEMINI4_RE = re.compile(r'^gemini[\s_-]*4(?:\.0)?[\s_-]*(hydrogen|neon|carbon|argon)(?:[\s_-]+(xhigh|high|medium|low))?$', re.I)


def is_studio_model(model):
    return isinstance(model, str) and model.lower().startswith(PREFIX)


def normalize_studio_model(model):
    """Map 'aistudio/gemini-4-hydrogen-medium', 'aistudio/gemini-4-neon-xhigh', 'aistudio/gemini-4-carbon-high',
    'aistudio/Gemini 4 Neon (High)', etc. -> ('gemini-4-<tier>', 'medium'|'high'|...)."""
    s = (model or '').strip()
    if s.lower().startswith(PREFIX):
        s = s[len(PREFIX):].strip()
    if s.lower().startswith('models/'):
        s = s[7:].strip()
    s = re.sub(r'\s*[（(]\s*(xhigh|high|medium|low)\s*[)）]\s*$', r'-\1', s, flags=re.I)
    mg = _GEMINI4_RE.match(s)
    if mg:
        return 'gemini-4-' + mg.group(1).lower(), (mg.group(2).lower() if mg.group(2) else None)
    ml = _LEVEL_RE.match(s)
    if ml and ml.group(1).lower().startswith('gemini-'):
        return ml.group(1).lower(), ml.group(2).lower()
    return s, None


def mask_exit(egress):
    """'socks5://user:pass@1.2.3.4:1080' -> '1.2.3.4' (never the credentials)."""
    try:
        return urlsplit(egress).hostname or ''
    except ValueError:
        return ''


def quota_day_start(now=None):
    """00:00 Pacific time (the day boundary AIStudio2API and Google use for daily limits)."""
    try:
        from zoneinfo import ZoneInfo
        local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/Los_Angeles'))
    except Exception:  # no tz database: US rule, DST from 2nd Sunday of March to 1st Sunday of November
        utc = now or datetime.now(timezone.utc)

        def sunday(month, nth):
            d = datetime(utc.year, month, 1, 10, tzinfo=timezone.utc)
            return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (nth - 1))
        dst = sunday(3, 2) <= utc < sunday(11, 1)
        local = utc.astimezone(timezone(timedelta(hours=-7 if dst else -8)))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


class _StudioBody:
    """Explicit close works even before the first byte was requested."""
    def __init__(self, owner):
        self.owner = owner
        self.iterator = owner.upstream.aiter_bytes().__aiter__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.owner.closed:
            raise StopAsyncIteration
        try:
            return await self.iterator.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        await self.owner.aclose()


class StudioStreamingResponse(StreamingResponse):
    def __init__(self, upstream, client, **kwargs):
        self.upstream, self.client = upstream, client
        self.closed = False
        super().__init__(_StudioBody(self), **kwargs)

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        with anyio.CancelScope(shield=True):
            try:
                await self.upstream.aclose()
            finally:
                await self.client.aclose()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.aclose()


class StudioProvider:
    def __init__(self, config=None):
        self.error = ''
        if config is None:
            config = {}
            path = os.environ.get('ACPANT_STUDIO_CONFIG')
            if path:
                try:
                    config = json.loads(Path(path).read_text(encoding='utf-8'))
                    if not isinstance(config, dict):
                        raise ValueError('expected object')
                except (OSError, ValueError):
                    config, self.error = {}, 'AI Studio server configuration could not be read'
        self.base = str(config.get('base_url') or '').rstrip('/')
        if self.base.endswith('/v1'):
            self.base = self.base[:-3]
        self.key = str(config.get('api_key') or '')
        self.admin_password = str(config.get('admin_password') or '')
        if self.base:
            try:
                url = urlsplit(self.base)
                valid = (url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password
                         and not url.query and not url.fragment and not url.path)
                _ = url.port
            except ValueError:
                valid = False
            if not valid:
                self.error = 'AI Studio base URL must be a server-owned HTTP(S) origin'
        if any(c in self.key + self.admin_password for c in '\r\n'):
            self.error = 'AI Studio credentials contain invalid characters'
        self.bindings = {}            # kept for interface compatibility: AIStudio2API ids are e-mails, never duplicated
        self._models, self._models_at = [], 0.0
        self._accounts, self._accounts_at, self._accounts_error = [], 0.0, ''
        self._lock = asyncio.Lock()
        self.logins = {}
        self._service_start_at = 0.0
        self._bg = None
        self.heals = {}               # e-mail -> {'at': time of the last automatic page restart, 'count': restarts in a row}
        self._heal_lock = asyncio.Lock()
        self.on_event = None          # set by the panel: on_event(email, text) adds a line to the activity log

    @property
    def configured(self):
        return bool(self.base and self.key and self.admin_password and not self.error)

    # ------------------------------------------------------------------ transport
    def _client(self, timeout=None):
        import httpx
        if timeout is None:
            timeout = DEFAULT_TIMEOUT
        return httpx.AsyncClient(base_url=self.base, timeout=httpx.Timeout(timeout, connect=10),
                                 follow_redirects=False, trust_env=False)

    async def _admin(self, timeout=60):
        """Logged-in admin client (caller closes it). The admin password never leaves the server."""
        client = self._client(timeout)
        try:
            r = await client.post('/api/auth/login', json={'username': 'admin', 'password': self.admin_password,
                                                           'remember': True})
            r.raise_for_status()
        except BaseException:
            await client.aclose()
            raise
        return client

    def _headers(self, incoming=None, account=''):
        headers = {'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'}
        for name in ('anthropic-version', 'anthropic-beta', 'openai-beta'):
            if incoming and incoming.get(name):
                headers[name] = incoming[name]
        if account:
            headers['X-AIStudio-Account'] = account
        return headers

    @staticmethod
    def _error(status, message, code='aistudio_unavailable', upstream_status=None):
        error = {'message': message, 'type': code, 'provider': 'aistudio'}
        if upstream_status is not None:
            error['upstream_status'] = upstream_status
        return JSONResponse({'error': error}, status_code=status, headers={'X-ACP-Provider': 'aistudio'})

    # ------------------------------------------------------------------ generation
    async def forward(self, path, body, incoming=None, email='', _retry=True):
        if not is_studio_model(body.get('model')):
            return None
        native, level = normalize_studio_model(body['model'])
        if not native:
            return self._error(400, 'AI Studio model name is empty', 'invalid_request_error')
        if path not in PATHS:
            return self._error(400, 'Unsupported AI Studio API path', 'invalid_request_error')
        if not self.configured:
            return self._error(503, self.error or NOT_CONFIGURED)
        account = ''
        if email:
            # The caller chose one Google account: serve that e-mail or fail; never another one.
            try:
                rows, problem = await self.accounts()
            except ImportError:
                return self._error(503, 'AI Studio integration requires the optional httpx dependency')
            if problem:
                return self._error(503, 'AI Studio account list is unavailable; the requested account cannot be verified')
            account, why = self.match(rows, email)
            if why == 'disabled':
                return self._error(400, "The requested account's AI Studio source is switched off in the panel", 'invalid_account')
            if not account:
                return self._error(400, 'The requested account has no AI Studio session', 'invalid_account')
        try:
            client = self._client()
        except ImportError:
            return self._error(503, 'AI Studio integration requires the optional httpx dependency')
        payload = dict(body, model=native)     # AIStudio2API uses the real model id; prefix and -medium/-high suffix normalized here
        if level:
            if path == '/v1/chat/completions':
                payload.setdefault('reasoning_effort', level)
            elif path == '/v1/responses':
                payload['reasoning'] = {**(payload.get('reasoning') or {}), 'effort': (payload.get('reasoning') or {}).get('effort') or level}
        upstream, transferred = None, False
        try:
            upstream = await client.send(client.build_request('POST', path, json=payload,
                                                               headers=self._headers(incoming, account)), stream=True)
            status = upstream.status_code
            if status == 401:
                return self._error(502, 'AI Studio upstream authentication failed', 'upstream_auth_error', 401)
            if 300 <= status < 400:
                return self._error(502, 'AI Studio upstream redirect was refused', 'upstream_redirect', status)
            headers = {'X-ACP-Provider': 'aistudio'}
            if upstream.headers.get('retry-after'):
                headers['Retry-After'] = upstream.headers['retry-after']
            if account:
                headers['X-ACP-Upstream-Account'] = account
            content_type = upstream.headers.get('content-type') or 'application/json'
            if status >= 400 or not body.get('stream'):
                raw = await upstream.aread()
                if status >= 400:
                    self._accounts_at = 0.0
                    raw = raw.replace(self.key.encode(), b'[REDACTED]')
                    log.warning('fail provider=aistudio model=%s status=%s', native, status)
                else:
                    log.info('ok provider=aistudio model=%s status=%s', native, status)
                reply = Response(raw, status_code=status, media_type=content_type, headers=headers)
                if not (_retry and status in (403, 429, 503) and DENIED.search(raw.decode('utf-8', 'replace'))):
                    return reply
            else:
                log.info('ok provider=aistudio model=%s stream=1 status=%s', native, status)
                headers.update({'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
                response = StudioStreamingResponse(upstream, client, status_code=status, media_type=content_type,
                                                   headers=headers)
                transferred = True
                return response
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('AI Studio transport failed: %s', type(exc).__name__)
            return self._error(502, 'AI Studio upstream connection or response failed', 'upstream_error')
        finally:
            if not transferred:
                with anyio.CancelScope(shield=True):
                    try:
                        if upstream is not None:
                            await upstream.aclose()
                    finally:
                        await client.aclose()
        # Google refused an account with code 7: restart its AI Studio page and try once more (see _heal_denied)
        healed = await self.heal_now()
        if healed and (not account or account in healed):
            return await self.forward(path, body, incoming, email, _retry=False)
        return reply

    async def models(self):
        if not self.configured:
            return []
        if time.monotonic() - self._models_at < 60:
            return [dict(m) for m in self._models]
        try:
            client = await self._admin(10)
            try:
                r = await client.get('/api/models')
                r.raise_for_status()
                items = r.json().get('models') or []
            finally:
                await client.aclose()
            self._models = [{'id': PREFIX + m['id'], 'object': 'model', 'type': 'model', 'created': 0,
                             'display_name': (m.get('name') or m['id']) + ' · AI Studio', 'owned_by': 'google',
                             'source': 'aistudio', 'provider': 'aistudio'}
                            for m in items if isinstance(m, dict) and m.get('id')
                            and 'generateContent' in (m.get('methods') or [])
                            and (m.get('capabilities') or {}).get('chat_model')]
            self._models_at = time.monotonic()
        except Exception as exc:
            log.warning('AI Studio catalog unavailable: %s', type(exc).__name__)
            return [dict(m) for m in self._models]
        return [dict(m) for m in self._models]

    # ------------------------------------------------------------------ accounts, quota facts, service
    async def accounts(self, force=False):
        """[(row)], error. A row never contains a proxy, cookie or key."""
        if not self.configured:
            return [], self.error or NOT_CONFIGURED
        async with self._lock:
            if not force and self._accounts_at and time.monotonic() - self._accounts_at < 20:
                return [dict(r) for r in self._accounts], self._accounts_error
            try:
                client = await self._admin(20)
                try:
                    accounts = (await client.get('/api/accounts')).json().get('accounts') or []
                    service = (await client.get('/api/status')).json()
                    cooldowns = (await client.get('/api/cooldowns')).json().get('cooldowns') or []
                    start = quota_day_start()
                    r = await client.get('/api/usage', params={'from': start.isoformat(),
                                                                'to': datetime.now(timezone.utc).isoformat(),
                                                                'tz': 'America/Los_Angeles'})
                    usage = {(g.get('key') or '').lower(): g for g in
                             ((r.json().get('groups') or {}).get('account') or [])} if r.status_code == 200 else {}
                    if await self._heal_denied(client, accounts, cooldowns):
                        cooldowns = (await client.get('/api/cooldowns')).json().get('cooldowns') or []
                    ready = (service.get('accounts') or {}).get('ready', 0)
                    if service.get('state') == 'STOPPED' and ready and time.time() - self._service_start_at > 300:
                        self._service_start_at = time.time()
                        started = await client.post('/api/control/start')
                        log.warning('AI Studio generation service was stopped; started it (HTTP %s)', started.status_code)
                finally:
                    await client.aclose()
                self._accounts = [self._row(a, service, cooldowns, usage, start, self.heals.get(str(a['id']).lower()))
                                  for a in accounts if isinstance(a, dict) and a.get('id')]
                self._accounts_error = ''
            except Exception as exc:
                log.warning('AI Studio account list unavailable: %s', type(exc).__name__)
                self._accounts, self._accounts_error = [], 'AI Studio 服务暂时连不上'
            self._accounts_at = time.monotonic()
            return [dict(r) for r in self._accounts], self._accounts_error

    @staticmethod
    def _row(a, service, cooldowns, usage, start, heal=None):
        email = str(a['id']).lower()
        state = a.get('state') or 'unknown'
        ready = bool(a.get('enabled')) and state in LIVE_STATES
        used = usage.get(email) or {}
        limits, denied = [], False
        for c in cooldowns:
            if (c.get('account_id') or '').lower() != email:
                continue
            reason = str(c.get('reason') or '')
            denied = denied or bool(DENIED.search(reason))
            m = re.search(r'limit: (\d+)', reason)
            limits.append({'model': c.get('model_id') or '*', 'until': c.get('until'),
                           'kind': reason.split(':', 1)[0].strip() if ':' in reason else (reason[:20] or '冷却'),
                           'limit': int(m.group(1)) if m else None})
        return {'account_id': email, 'label': a.get('label') or email, 'email': email, 'state': state, 'ready': ready,
                'disabled': not a.get('enabled'), 'models': sorted(a.get('models') or []),
                'error': (str(a.get('message') or '')[:200] if not ready else
                          ('Google 一直拒绝这个账号的 AI Studio 请求（权限码 7），自动重启 %d 次没好；请用这个账号在浏览器打开'
                           ' aistudio.google.com 发一句话，看官网是否也报错' % heal['count'])
                          if denied and heal and heal['count'] >= HEAL_TRIES else ''),
                'exit': mask_exit(a.get('egress') or ''),
                'locale': a.get('locale') or '', 'timezone': a.get('timezone') or '',
                'quota': {'source': 'aistudio', 'plan': a.get('benefit_tier') or '未知', 'available': ready,
                          'used': None, 'remaining': None, 'total': None,
                          'used_today': {k: used.get(k, 0) for k in
                                         ('requests', 'succeeded', 'failed', 'rate_limited', 'total_tokens')},
                          'day_start': start.isoformat(), 'resets_at': (start + timedelta(days=1)).isoformat(),
                          'limits': limits, 'service_state': service.get('state')}}

    # ------------------------------------------------------------------ automatic repair of "permission denied" pages
    def _note(self, email, text):
        log.warning('%s: %s', email, text)
        if self.on_event:
            try:
                self.on_event(email, text)
            except Exception:  # noqa: BLE001
                pass

    async def _heal_denied(self, client, accounts, cooldowns):
        """AI Studio can refuse every request from an account's background page with 403 / code 7 ("The caller does not
        have permission") - seen for hours right after a server-browser login - while a freshly started page of the same
        account works at once. Restart that page the way the AIStudio2API admin page does (disable, then enable with the
        same proxy / locale / time zone) and clear its permission cooldowns. Once per HEAL_GAP per account at most; after
        HEAL_TRIES restarts in a row the account is left alone for HEAL_PAUSE and the panel says so.
        Returns the e-mails restarted. Proxies are passed back unchanged and never logged."""
        denied = {}
        for c in cooldowns:
            if DENIED.search(str(c.get('reason') or '')):
                denied.setdefault((c.get('account_id') or '').lower(), []).append(c.get('scope') or c.get('model_id') or '')
        async with self._heal_lock:
            now = time.time()
            for email, h in list(self.heals.items()):
                if email not in denied and now - h['at'] > (HEAL_PAUSE if h['count'] >= HEAL_TRIES else HEAL_GAP):
                    self.heals.pop(email, None)       # stayed healthy since the last restart
            done = []
            for a in accounts:
                email = str(a.get('id') or '').lower() if isinstance(a, dict) else ''
                if (email not in denied or not a.get('enabled') or a.get('active_requests')
                        or a.get('state') not in LIVE_STATES):
                    continue
                h = self.heals.setdefault(email, {'at': 0.0, 'count': 0})
                if now - h['at'] < (HEAL_PAUSE if h['count'] >= HEAL_TRIES else HEAL_GAP):
                    continue
                if h['count'] >= HEAL_TRIES:
                    h['count'] = 0
                h['at'] = now
                path = '/api/accounts/' + quote(a['id'], safe='')
                settings = {'label': a.get('label') or a['id'], 'proxy': a.get('proxy') or '',
                            'locale': a.get('locale') or '', 'timezone': a.get('timezone') or ''}
                try:
                    (await client.put(path, json=dict(settings, enabled=False))).raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    log.warning('AI Studio page restart of %s failed: %s', email, type(exc).__name__)
                    continue
                enabled = False
                for _ in range(3):          # never leave the account switched off
                    await asyncio.sleep(1)
                    try:
                        (await client.put(path, json=dict(settings, enabled=True))).raise_for_status()
                        enabled = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        log.warning('AI Studio re-enable of %s failed: %s', email, type(exc).__name__)
                if not enabled:
                    self._note(email, 'AI Studio 页面重启时没能重新启用这个账号，请在面板里手动启用')
                    continue
                for scope in denied[email]:
                    if scope:
                        try:
                            await client.delete(path + '/cooldowns/' + quote(scope, safe=''))
                        except Exception:  # noqa: BLE001
                            pass
                h['count'] += 1
                done.append(email)
                self._note(email, 'AI Studio 以权限码 7 拒绝了这个账号的请求，已自动重启它的 AI Studio 页面（连续第 %d 次）'
                           % h['count'])
            return done

    async def heal_now(self):
        """Run the repair at once (after a request failed with code 7). Returns the e-mails restarted."""
        try:
            client = await self._admin(30)
            try:
                accounts = (await client.get('/api/accounts')).json().get('accounts') or []
                cooldowns = (await client.get('/api/cooldowns')).json().get('cooldowns') or []
                done = await self._heal_denied(client, accounts, cooldowns)
            finally:
                await client.aclose()
        except Exception as exc:  # noqa: BLE001
            log.warning('AI Studio repair check failed: %s', type(exc).__name__)
            return []
        if done:
            self._accounts_at = 0.0
        return done

    def match(self, rows, email):
        email = (email or '').strip().lower()
        if not email:
            return '', 'none'
        same = [r for r in rows if str(r.get('email') or '').lower() == email]
        if same and same[0].get('disabled'):
            return '', 'disabled'
        return (same[0]['account_id'], '') if same else ('', 'none')

    async def set_enabled(self, email, enabled):
        """Switch one AIStudio2API account on or off; its proxy, locale and time zone are written back unchanged.
        Shares the repair lock so an automatic page restart can never undo the owner's choice."""
        email = (email or '').strip().lower()
        async with self._heal_lock:
            client = await self._admin(30)
            try:
                accounts = (await client.get('/api/accounts')).json().get('accounts') or []
                a = next((x for x in accounts if str(x.get('id') or '').lower() == email), None)
                if a is None:
                    raise KeyError(email)
                r = await client.put('/api/accounts/' + quote(a['id'], safe=''), json={
                    'label': a.get('label') or a['id'], 'enabled': bool(enabled), 'proxy': a.get('proxy') or '',
                    'locale': a.get('locale') or '', 'timezone': a.get('timezone') or ''})
                r.raise_for_status()
            finally:
                await client.aclose()
        self.heals.pop(email, None)
        self._accounts_at = 0.0
        return bool(enabled)

    async def delete(self, email):
        """Remove one AIStudio2API account together with its saved sign-in (the Antigravity login with the same
        e-mail is not touched). Shares the repair lock so a page restart cannot run on an account being removed."""
        email = (email or '').strip().lower()
        async with self._heal_lock:
            client = await self._admin(30)
            try:
                accounts = (await client.get('/api/accounts')).json().get('accounts') or []
                a = next((x for x in accounts if str(x.get('id') or '').lower() == email), None)
                if a is None:
                    raise KeyError(email)
                r = await client.delete('/api/accounts/' + quote(a['id'], safe=''))
                r.raise_for_status()
            finally:
                await client.aclose()
        self.heals.pop(email, None)
        self._accounts_at = 0.0
        return True

    def start_background(self):
        """Keep the account list / service auto-start running even when nobody has the panel open."""
        async def loop():
            while True:
                try:
                    await self.accounts(force=True)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(60)
        if self.configured and (self._bg is None or self._bg.done()):
            self._bg = asyncio.ensure_future(loop())

    # ------------------------------------------------------------------ exit (proxy) of an account
    @staticmethod
    def _exit_of(proxy):
        cmd = ['curl', '-s', '-m', '20', 'https://ipinfo.io/json'] + (['-x', proxy] if proxy else [])
        try:
            data = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=25).stdout or '{}')
            if data.get('country') and data.get('timezone'):
                return {'country': data['country'], 'timezone': data['timezone'], 'ip': data.get('ip', '')}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return None

    async def _exit_settings(self, proxy):
        proxy = (proxy or '').strip()
        if proxy.startswith('socks5h://'):
            proxy = 'socks5://' + proxy[len('socks5h://'):]
        found = await asyncio.to_thread(self._exit_of, proxy)
        if found is None:
            raise ValueError('这个账号的代理现在连不上，没有打开登录；请先在面板检测或更换代理')
        return {'proxy': proxy, 'locale': LOCALES.get(found['country'], 'en-US'), 'timezone': found['timezone'],
                'exit_ip': found['ip'], 'country': found['country']}

    # ------------------------------------------------------------------ server-browser login
    async def login_call(self, method, path, body=None):
        """Compatibility entry used by the panel: (status, data)."""
        if not self.configured:
            return 503, {'error': {'message': self.error or NOT_CONFIGURED}}
        try:
            if method == 'POST' and path == '/v1/aistudio/accounts/login':
                return 200, await self.start_login(body or {})
            view = path.rsplit('/', 1)[-1]
            if method == 'GET':
                return 200, await self.login_state(view)
            if method == 'DELETE':
                await self.cancel_login(view)
                return 200, {'ok': True}
        except KeyError:
            return 404, {'error': {'message': '没有这个登录窗口'}}
        except ValueError as exc:
            return 400, {'error': {'message': str(exc)}}
        except ImportError:
            return 503, {'error': {'message': 'AI Studio integration requires the optional httpx dependency'}}
        except Exception as exc:
            log.warning('AI Studio login call failed: %s', type(exc).__name__)
            return 502, {'error': {'message': 'AI Studio 服务没有响应，稍后再试'}}
        return 400, {'error': {'message': 'unsupported'}}

    async def start_login(self, body):
        email = str(body.get('email') or '').strip().lower()
        if email and not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", email):
            raise ValueError('邮箱格式不对')
        use_proxy = bool(body.get('use_proxy'))
        for v, item in list(self.logins.items()):
            if time.time() - item['started'] > 1800:
                self.logins.pop(v, None)
        view = 'g2a-' + secrets.token_urlsafe(18)
        client = await self._admin(60)
        try:
            existing = {str(a['id']).lower(): a for a in (await client.get('/api/accounts')).json().get('accounts') or []}
            exit_ = await self._exit_settings(body.get('proxy')) if use_proxy else None
            if email and email in existing:
                if exit_:
                    a = existing[email]
                    if ((a.get('proxy') or '') != exit_['proxy'] or a.get('locale') != exit_['locale']
                            or a.get('timezone') != exit_['timezone']):
                        r = await client.put('/api/accounts/' + quote(email, safe=''), json={
                            'label': a.get('label') or email, 'enabled': a.get('enabled', True), 'proxy': exit_['proxy'],
                            'locale': exit_['locale'], 'timezone': exit_['timezone']})
                        r.raise_for_status()
                r = await client.post('/api/accounts/' + quote(email, safe='') + '/login', json={'view': view})
            else:   # a new AI Studio account: AIStudio2API reads the e-mail from the signed-in page
                r = await client.post('/api/accounts', json={
                    'proxy': exit_['proxy'] if exit_ else '', 'locale': exit_['locale'] if exit_ else '',
                    'timezone': exit_['timezone'] if exit_ else '', 'view': view})
            r.raise_for_status()
            task = r.json()['task']
        finally:
            await client.aclose()
        if task.get('view') != view:
            await self._drop_task(task.get('id'))
            raise ValueError('AI Studio 服务没有提供网页内登录窗口')
        self.logins[view] = {'task_id': task['id'], 'email': email, 'existing': email in existing,
                             'before': sorted(existing), 'started': time.time(), 'state': 'running'}
        result = {'view': view, 'email': email}
        if exit_:
            result.update({k: exit_[k] for k in ('exit_ip', 'country', 'timezone', 'locale')})
        return result

    async def _drop_task(self, task_id):
        if not task_id:
            return
        try:
            client = await self._admin(20)
            try:
                await client.delete('/api/accounts/tasks/' + quote(task_id, safe=''))
            finally:
                await client.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def login_state(self, view):
        item = self.logins.get(view)
        if item is None:
            raise KeyError(view)
        if item['state'] != 'running':
            return {k: item.get(k) for k in ('state', 'error', 'email')}
        client = await self._admin(20)
        try:
            tasks = (await client.get('/api/accounts/tasks')).json().get('tasks') or []
            task = next((t for t in tasks if t.get('id') == item['task_id']), None)
            if task is None:
                item.update(state='cancelled', error='登录已取消')
                return {k: item.get(k) for k in ('state', 'error', 'email')}
            entry = (task.get('items') or [{}])[0]
            if task.get('state') not in ('done', 'failed', 'cancelled'):
                return {'state': 'running', 'email': item['email'], 'error': None, 'sidecar_state': task.get('state')}
            got = (entry.get('label') or '').lower()
            if task['state'] != 'done' or entry.get('state') == 'failed':
                item.update(state='failed', error=str(entry.get('error') or '登录没有完成')[:300])
            elif item['email'] and got != item['email']:
                item.update(state='failed', error='登录的是 %s，不是 %s；没有保留这个登录' % (got, item['email']))
                if got and got not in item['before']:      # an account that only exists because of this wrong login
                    await client.delete('/api/accounts/' + quote(got, safe=''))
            else:
                item.update(state='done', email=got or item['email'])
            await client.delete('/api/accounts/tasks/' + quote(item['task_id'], safe=''))
        finally:
            await client.aclose()
        self._accounts_at = 0.0
        self._models_at = 0.0
        return {k: item.get(k) for k in ('state', 'error', 'email')}

    async def cancel_login(self, view):
        item = self.logins.get(view)
        if item and item['state'] == 'running':
            item.update(state='cancelled', error='登录已取消')
            await self._drop_task(item['task_id'])

    async def relay_login_view(self, websocket, view):
        """Relay the server browser's live view between the panel and AIStudio2API (frames down, input up)."""
        import websockets
        if self.logins.get(view, {}).get('state') != 'running':
            return
        client = await self._admin(20)
        try:
            cookie = '; '.join('%s=%s' % (k, v) for k, v in client.cookies.items())
        finally:
            await client.aclose()
        url = self.base.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1)
        async with websockets.connect(url + '/api/login-view/' + quote(view, safe=''),
                                      additional_headers={'Cookie': cookie}, max_size=8 * 1024 * 1024,
                                      open_timeout=20) as upstream:
            async def down():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            async def up():
                while True:
                    message = await websocket.receive()
                    if message['type'] == 'websocket.disconnect':
                        return
                    if message.get('text') is not None:
                        await upstream.send(message['text'])
            tasks = [asyncio.ensure_future(down()), asyncio.ensure_future(up())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()

    # ------------------------------------------------------------------ paste a Cookie export (fallback)
    async def bind(self, cookies, email='', proxy=None):
        if not self.configured:
            return 503, {'error': {'message': self.error or NOT_CONFIGURED}}
        text = cookies if isinstance(cookies, str) else json.dumps(cookies)
        try:
            exit_ = await self._exit_settings(proxy) if proxy is not None else None
            client = await self._admin(200)
            try:
                r = await client.post('/api/accounts/import/cookies', json={
                    'items': [{'email': (email or '').lower(), 'cookies': text, 'auth_user': 0}],
                    'proxy': exit_['proxy'] if exit_ else '', 'locale': exit_['locale'] if exit_ else '',
                    'timezone': exit_['timezone'] if exit_ else ''})
                if r.status_code >= 400:
                    return 400, {'error': {'message': 'AI Studio 没有接受这份 Cookie'}}
                task_id = r.json()['task']['id']
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    tasks = (await client.get('/api/accounts/tasks')).json().get('tasks') or []
                    task = next((t for t in tasks if t.get('id') == task_id), {})
                    if task.get('state') in ('done', 'failed', 'cancelled'):
                        await client.delete('/api/accounts/tasks/' + quote(task_id, safe=''))
                        entry = (task.get('items') or [{}])[0]
                        if task['state'] != 'done' or entry.get('state') == 'failed':
                            return 400, {'error': {'message': str(entry.get('error') or 'AI Studio 没有接受这份 Cookie')[:300]}}
                        got = (entry.get('label') or '').lower()
                        if email and got != email.lower():
                            return 400, {'error': {'message': '这份 Cookie 登录的是 %s，不是 %s' % (got, email)}}
                        self._accounts_at = self._models_at = 0.0
                        return 200, {'email': got, 'account_id': got, 'existing': False, 'studio': {'ready': True}}
                    await asyncio.sleep(0.5)
                return 504, {'error': {'message': '导入验证超时，请稍后刷新状态'}}
            finally:
                await client.aclose()
        except ValueError as exc:
            return 400, {'error': {'message': str(exc)}}
        except Exception as exc:
            log.warning('AI Studio cookie import failed: %s', type(exc).__name__)
            return 502, {'error': {'message': 'AI Studio 服务没有响应，稍后再试'}}


STUDIO = StudioProvider()
