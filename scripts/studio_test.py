"""Offline checks for studio-4 (ACP-Ant -> its own AIStudio2API). A fake AIStudio2API (HTTP mock + real loopback
WebSocket) stands in; no Google, no agy, no production call, no real credential."""
import asyncio, json, os, re, subprocess, sys, tempfile, threading, time
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent).resolve()
sys.path.insert(0, str(root))
import httpx
from websockets.asyncio.server import serve

checks = 0


def ok(x, label):
    global checks
    assert x, label
    checks += 1
    print('PASS', label)


# ---------------------------------------------------------------- fake AIStudio2API
port, seen_ws = {}, []


async def live(conn):
    seen_ws.append({'path': conn.request.path, 'cookie': conn.request.headers.get('Cookie')})
    await conn.send(json.dumps({'type': 'frame', 'context': 'c', 'url': 'https://accounts.google.com/', 'title': 't', 'width': 10, 'height': 10}))
    await conn.send(b'\xff\xd8FRAME')
    async for m in conn:
        await conn.send(json.dumps({'echo': json.loads(m)}))


def run_ws():
    async def main():
        async with serve(live, '127.0.0.1', 0) as s:
            port['p'] = s.sockets[0].getsockname()[1]
            await asyncio.Future()
    asyncio.run(main())


threading.Thread(target=run_ws, daemon=True).start()
while 'p' not in port:
    time.sleep(0.05)

S = {'accounts': [
        {'id': 'main@example.invalid', 'label': 'main', 'enabled': True, 'state': 'ready', 'proxy': 'socks5://u:SECRET@1.2.3.4:1080',
         'egress': 'socks5://u:SECRET@1.2.3.4:1080', 'locale': 'en-US', 'timezone': 'America/Los_Angeles', 'models': ['gemini-4-hydrogen'],
         'benefit_tier': 'Free', 'message': ''},
        {'id': 'dead@example.invalid', 'label': 'dead', 'enabled': True, 'state': 'auth_required', 'proxy': '', 'egress': '',
         'models': [], 'benefit_tier': 'Pro', 'message': '隔离登录态失效'}],
     'status': {'state': 'STOPPED', 'accounts': {'ready': 1}}, 'tasks': {}, 'calls': [], 'puts': [], 'creates': [], 'deleted': []}


def handler(request):
    path, method = request.url.path, request.method
    S['calls'].append((method, path, dict(request.headers), request.content))
    if path == '/api/auth/login':
        return httpx.Response(200, json={'ok': True}, headers={'set-cookie': 'aistudio2api_session=ADMIN-SESSION; Path=/'})
    if path == '/api/accounts' and method == 'GET':
        return httpx.Response(200, json={'accounts': S['accounts']})
    if path == '/api/status':
        return httpx.Response(200, json=S['status'])
    if path == '/api/control/start':
        S['status'] = {'state': 'RUNNING', 'accounts': {'ready': 1}}
        return httpx.Response(202, json={})
    if path == '/api/cooldowns':
        return httpx.Response(200, json={'cooldowns': [{'account_id': 'main@example.invalid', 'model_id': 'gemini-4-hydrogen',
                                                        'until': '2026-11-17T08:00:00Z', 'reason': '每日限额: 429 limit: 50'}]})
    if path == '/api/usage':
        return httpx.Response(200, json={'groups': {'account': [{'key': 'main@example.invalid', 'requests': 4, 'succeeded': 3,
                                                                'failed': 1, 'rate_limited': 1, 'total_tokens': 99}]}})
    if path == '/api/models':
        return httpx.Response(200, json={'models': [{'id': 'gemini-4-hydrogen', 'name': 'Hydrogen', 'methods': ['generateContent'],
                                                     'capabilities': {'chat_model': True}},
                                                    {'id': 'imagen-x', 'methods': ['predict'], 'capabilities': {}}]})
    if method == 'PUT' and path.startswith('/api/accounts/'):
        S['puts'].append(json.loads(request.content))
        return httpx.Response(200, json={'account': {}})
    if method == 'POST' and re.fullmatch(r'/api/accounts/[^/]+/login', path):
        body = json.loads(request.content)
        tid = 'T%d' % (len(S['tasks']) + 1)
        S['tasks'][tid] = {'id': tid, 'view': body['view'], 'state': 'running', 'items': [{'label': path.split('/')[3], 'state': 'pending'}]}
        return httpx.Response(202, json={'task': S['tasks'][tid]})
    if method == 'POST' and path == '/api/accounts':
        body = json.loads(request.content)
        S['creates'].append(body)
        tid = 'T%d' % (len(S['tasks']) + 1)
        S['tasks'][tid] = {'id': tid, 'view': body['view'], 'state': 'running', 'items': [{'label': '', 'state': 'pending'}]}
        return httpx.Response(202, json={'task': S['tasks'][tid]})
    if path == '/api/accounts/tasks':
        return httpx.Response(200, json={'tasks': list(S['tasks'].values())})
    if method == 'DELETE' and path.startswith('/api/accounts/tasks/'):
        S['tasks'].pop(path.rsplit('/', 1)[1], None)
        return httpx.Response(204)
    if method == 'DELETE' and path.startswith('/api/accounts/'):
        S['deleted'].append(path.rsplit('/', 1)[1])
        return httpx.Response(204)
    if path == '/api/accounts/import/cookies':
        tid = 'C%d' % (len(S['tasks']) + 1)
        S['tasks'][tid] = {'id': tid, 'state': 'done', 'items': [{'label': 'main@example.invalid', 'state': 'done'}]}
        return httpx.Response(202, json={'task': S['tasks'][tid]})
    if path in ('/v1/chat/completions', '/v1/messages', '/v1/responses'):
        body = json.loads(request.content)
        if body.get('stream'):
            return httpx.Response(200, content=b'data: {"x":1}\n\ndata: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
        if request.headers.get('authorization') != 'Bearer sidecar-key':
            return httpx.Response(401, json={'error': 'bad key'})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'hi from ' + body['model']}}]})
    return httpx.Response(404)


async def unit():
    from studio_provider import StudioProvider, is_studio_model
    p = StudioProvider({'base_url': 'http://127.0.0.1:%d' % port['p'], 'api_key': 'sidecar-key', 'admin_password': 'admin-pw'})
    p._client = lambda timeout=290: httpx.AsyncClient(base_url=p.base, transport=httpx.MockTransport(handler), follow_redirects=False)
    p._exit_of = staticmethod(lambda proxy: {'country': 'US', 'timezone': 'America/Los_Angeles', 'ip': '9.9.9.9'} if proxy == 'socks5://good' else None)
    ok(p.configured and not StudioProvider({'base_url': 'http://x', 'api_key': 'k'}).configured, 'needs base URL, API key and admin password')
    body = {'model': 'aistudio/gemini-4-hydrogen', 'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [{'type': 'function', 'function': {'name': 't'}}], 'metadata': {'opaque': 1}}
    r = await p.forward('/v1/chat/completions', body, {'authorization': 'Bearer client-key', 'x-aistudio-account': 'evil@x'})
    sent = S['calls'][-1]
    ok(r.status_code == 200 and json.loads(sent[3]) == dict(body, model='gemini-4-hydrogen') and json.loads(r.body)['choices'][0]['message']['content'] == 'hi from gemini-4-hydrogen',
       'aistudio/ prefix is removed for AIStudio2API; tools and unknown fields pass unchanged')
    ok(sent[2]['authorization'] == 'Bearer sidecar-key' and 'x-aistudio-account' not in sent[2], 'client key replaced; a caller cannot pick the account by header')
    ok(await p.forward('/v1/chat/completions', {'model': 'gemini-3.8-flash'}) is None, 'non-Studio models stay on Antigravity')
    r = await p.forward('/v1/chat/completions', dict(body, stream=True))
    chunks = b''.join([c async for c in r.body_iterator])
    ok(chunks.endswith(b'data: [DONE]\n\n') and r.headers['x-acp-provider'] == 'aistudio', 'streams pass through intact')
    r = await p.forward('/v1/messages', body, None, 'MAIN@example.invalid')
    ok(r.status_code == 200 and S['calls'][-1][2]['x-aistudio-account'] == 'main@example.invalid' and r.headers['x-acp-upstream-account'] == 'main@example.invalid',
       'a chosen e-mail is served by that AIStudio2API account')
    n = len([c for c in S['calls'] if c[1].startswith('/v1/')])
    r = await p.forward('/v1/messages', body, None, 'nobody@example.invalid')
    ok(r.status_code == 400 and len([c for c in S['calls'] if c[1].startswith('/v1/')]) == n, 'an e-mail without an AI Studio account is refused, no other account used')
    p.key = 'wrong'
    r = await p.forward('/v1/chat/completions', body)
    ok(r.status_code == 502 and b'wrong' not in r.body, 'upstream 401 becomes 502 without leaking the key')
    p.key = 'sidecar-key'
    models = await p.models()
    ok([m['id'] for m in models] == ['aistudio/gemini-4-hydrogen'] and models[0]['source'] == 'aistudio', 'only chat models are listed, with the aistudio/ prefix')
    rows, err = await p.accounts(force=True)
    text = json.dumps(rows)
    main = next(r for r in rows if r['email'] == 'main@example.invalid')
    dead = next(r for r in rows if r['email'] == 'dead@example.invalid')
    ok(not err and 'SECRET' not in text and main['exit'] == '1.2.3.4' and 'proxy' not in main, 'account rows never contain proxy credentials; exit shows the host only')
    ok(main['ready'] and not dead['ready'] and dead['error'] == '隔离登录态失效' and dead['state'] == 'auth_required', 'ready / login-required state and reason come from AIStudio2API')
    q = main['quota']
    ok(q['remaining'] is None and q['used_today']['requests'] == 4 and q['limits'] == [{'model': 'gemini-4-hydrogen', 'until': '2026-11-17T08:00:00Z', 'kind': '每日限额', 'limit': 50}]
       and q['plan'] == 'Free', 'quota: today\'s usage and per-model limits; remaining stays unknown')
    ok(any(c[1] == '/api/control/start' for c in S['calls']), 'a stopped generation service is started once an account is ready')
    n = len([c for c in S['calls'] if c[1] == '/api/control/start'])
    S['status'] = {'state': 'STOPPED', 'accounts': {'ready': 1}}
    await p.accounts(force=True)
    ok(len([c for c in S['calls'] if c[1] == '/api/control/start']) == n, 'auto start is attempted at most every 5 minutes')
    # login: existing account, same exit as Antigravity
    r = await p.start_login({'email': 'main@example.invalid', 'use_proxy': True, 'proxy': 'socks5h://good'.replace('socks5h://good', 'socks5://good')})
    ok(S['puts'][-1] == {'label': 'main', 'enabled': True, 'proxy': 'socks5://good', 'locale': 'en-US', 'timezone': 'America/Los_Angeles'}
       and r['exit_ip'] == '9.9.9.9' and 'proxy' not in r and any(re.fullmatch(r'/api/accounts/main@example.invalid/login', c[1].replace('%40', '@')) for c in S['calls']),
       'relogin of an existing account first moves it to the Antigravity exit')
    try:
        await p.start_login({'email': 'main@example.invalid', 'use_proxy': True, 'proxy': 'socks5://dead'})
        ok(False, 'dead proxy')
    except ValueError:
        ok(True, 'an unreachable proxy opens no login')
    view = r['view']
    ok((await p.login_state(view))['state'] == 'running', 'progress while signing in')
    S['tasks'][p.logins[view]['task_id']].update(state='done', items=[{'label': 'main@example.invalid', 'state': 'done'}])
    st = await p.login_state(view)
    ok(st['state'] == 'done' and p.logins[view]['task_id'] not in S['tasks'], 'a finished login is reported and its task cleared')
    # login: new account, wrong e-mail -> refused and the stray account removed
    r = await p.start_login({'email': 'new@example.invalid'})
    ok(S['creates'][-1]['view'] == r['view'] and S['creates'][-1]['proxy'] == '', 'a new e-mail opens AIStudio2API\'s add-account browser login')
    S['tasks'][p.logins[r['view']]['task_id']].update(state='done', items=[{'label': 'stranger@example.invalid', 'state': 'done'}])
    st = await p.login_state(r['view'])
    ok(st['state'] == 'failed' and 'stranger' in st['error'] and S['deleted'][-1] == 'stranger%40example.invalid' or S['deleted'][-1] == 'stranger@example.invalid',
       'a login as a different e-mail is refused and the account it created is removed')
    r = await p.start_login({'email': 'main@example.invalid'})
    await p.cancel_login(r['view'])
    ok((await p.login_state(r['view']))['state'] == 'cancelled', 'cancel ends the login')
    st, data = await p.bind([{'name': 'SID', 'value': 'x'}], 'main@example.invalid', 'socks5://good')
    imp = next(json.loads(c[3]) for c in reversed(S['calls']) if c[1] == '/api/accounts/import/cookies')
    ok(st == 200 and data['email'] == 'main@example.invalid' and imp['proxy'] == 'socks5://good' and imp['timezone'] == 'America/Los_Angeles',
       'pasted Cookie goes to AIStudio2API with the same Antigravity exit')
    return p


p = asyncio.run(unit())

# ---------------------------------------------------------------- through the real gateway + panel
with tempfile.TemporaryDirectory() as d:
    os.environ.update(AGY_ROOT=d, AGY_GATEWAY_KEY='client-key', AGY_ACCOUNTS=os.path.join(d, 'accounts'))
    for name, email, proxy in (('g9', 'main@example.invalid', 'socks5://good'), ('g8', 'native@example.invalid', '')):
        home = os.path.join(d, 'accounts', name)
        os.makedirs(os.path.join(home, '.gemini', 'antigravity-cli'))
        open(os.path.join(home, '.gemini', 'antigravity-cli', 'antigravity-oauth-token'), 'w').write(json.dumps({'id_token_email': email}))
        open(os.path.join(home, 'proxy.env'), 'w').write(('HTTPS_PROXY=%s\nHTTP_PROXY=%s\n' % (proxy, proxy)) if proxy else '')
    import acp_ant as core
    import acp_panel as panel
    core.STUDIO = p
    p._accounts_at = 0
    from starlette.testclient import TestClient
    client = TestClient(core.app, base_url='http://testserver')
    panel.load_state()
    client.cookies.update({panel.COOKIE: panel._token(int(time.time()) + 600)})
    hdr = {'X-ACP-Panel': '1'}
    key = {'Authorization': 'Bearer client-key'}
    r = client.post('/v1/chat/completions', json={'model': 'aistudio/gemini-4-hydrogen', 'messages': [{'role': 'user', 'content': 'x'}]},
                    headers={**key, 'X-ACP-Account': 'g9'})
    ok(r.status_code == 200 and r.headers['x-acp-upstream-account'] == 'main@example.invalid', 'gateway: an Antigravity account name maps to its e-mail\'s AI Studio account')
    r = client.post('/v1/chat/completions', json={'model': 'aistudio/gemini-4-carbon', 'messages': [{'role': 'user', 'content': 'x'}]},
                    headers={**key, 'X-ACP-Account': 'g8'})
    ok(r.status_code == 400, 'gateway: an account without an AI Studio login is refused for aistudio/ models')
    models = client.get('/v1/models', headers=key).json()['data']
    ok('aistudio/gemini-4-hydrogen' in [m['id'] for m in models], 'gateway /v1/models lists AI Studio models')
    view = client.get('/panel/api/overview').json()
    by = {a['name']: a for a in view['accounts']}
    ok(by['g9']['studio_link'] == 'linked' and by['g9']['studio']['exit'] == '1.2.3.4' and 'SECRET' not in json.dumps(view)
       and [s['email'] for s in view['studio']['unlinked']] == ['dead@example.invalid'], 'panel links by e-mail, shows AI-Studio-only accounts, no secret')
    r = client.post('/panel/api/studio/login', json={'email': 'main@example.invalid'}, headers=hdr)
    ok(r.status_code == 200 and r.json()['same_exit_as'] == 'g9' and S['puts'][-1]['proxy'] == 'socks5://good', 'panel login uses the Antigravity account\'s exit')
    v = r.json()['view']
    with client.websocket_connect('/panel/api/studio/login-view/' + v, headers={'origin': 'http://testserver'}) as ws:
        meta = json.loads(ws.receive_text()); frame = ws.receive_bytes()
        ws.send_text(json.dumps({'type': 'key', 'action': 'down', 'key': 'a'}))
        echo = json.loads(ws.receive_text())
    ok(meta['type'] == 'frame' and frame.startswith(b'\xff\xd8') and echo['echo']['key'] == 'a' and 'ADMIN-SESSION' in (seen_ws[-1]['cookie'] or ''),
       'live view relays frames and keys; the AIStudio2API admin session stays on the server')
    c2 = TestClient(core.app, base_url='http://testserver')
    try:
        with c2.websocket_connect('/panel/api/studio/login-view/' + v, headers={'origin': 'http://testserver'}) as ws:
            ws.receive_text()
        ok(False, 'no session')
    except Exception:
        ok(True, 'live view refused without the panel session')
    r = client.post('/panel/api/studio/login', json={'email': 'brand.new@example.invalid'}, headers=hdr)
    ok(r.status_code == 200 and S['creates'][-1]['view'] == r.json()['view'], 'panel can add a brand-new AI Studio account by browser login')
    connect = client.get('/panel/api/connect').json()
    ok('aistudio/gemini-4-hydrogen' in connect['studio_models'], 'connect info lists AI Studio models')
    test = client.post('/panel/api/test', json={'model': 'aistudio/gemini-4-hydrogen', 'prompt': 'hi'}, headers=hdr).json()
    ok(test['ok'] and test['source'] == 'aistudio', 'panel test reaches AI Studio')
    src = (root / 'panel.html').read_text(encoding='utf-8') + (root / 'acp_panel.py').read_text(encoding='utf-8') + (root / 'studio_provider.py').read_text(encoding='utf-8')
    ok('web_session_valid' not in src and 'addStudioModal' in src, 'panel has the add-account entry')

html = (root / 'panel.html').read_text(encoding='utf-8')
script = re.search(r'<script>(.*)</script>', html, re.S).group(1)
code = r"""
const vm=require('vm'),assert=require('assert'),fs=require('fs');const s=fs.readFileSync(process.argv[1],'utf8');
const names=['when','studioState','studioQuota','studioBody','studioBtn','switchHtml','studioSwitch','studioDelBtn','studioHtml','studioOnlyHtml'];
const src=names.map(n=>{const i=s.indexOf('function '+n+'(');assert(i>=0,n);let d=0,j=s.indexOf('{',i);for(let k=j;k<s.length;k++){if(s[k]==='{')d++;if(s[k]==='}'&&--d===0)return s.slice(i,k+1);}}).join('\n');
const c={esc:String,num:String,ago:t=>'T'+t,copy(){},DATA:{studio:{configured:true,error:''}}};vm.createContext(c);vm.runInContext(src,c);
const row={account_id:'a@b',email:'a@b',ready:true,state:'ready',exit:'1.2.3.4',models:['m'],quota:{plan:'Free',used_today:{requests:2},limits:[],service_state:'RUNNING'}};
let h=c.studioHtml({email:'a@b',studio:row,studio_link:'linked'});
assert(h.includes('出口 1.2.3.4')&&h.includes('剩余 <b>未知</b>')&&h.includes('请求 <b>2</b>')&&!h.includes('网页会话'),'render');
h=c.studioHtml({email:'a@b',studio:{...row,ready:false,state:'auth_required',error:'隔离登录态失效'},studio_link:'linked'});assert(h.includes('隔离登录态失效'),'reason');
console.log('PANEL_RENDER_PASS');
"""
with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
    f.write(script)
r = subprocess.run(['node', '-e', code, f.name], capture_output=True, text=True, encoding='utf-8')
os.unlink(f.name)
ok(r.returncode == 0, 'panel renders exit, quota facts and the real AI Studio state' + ((' :: ' + r.stderr[-300:]) if r.returncode else ''))
print('DIRECT_CHECKS %d passed; no Google/agy/production calls' % checks)
