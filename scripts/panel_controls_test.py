"""Offline: account search, per-account Antigravity model list, and separate Antigravity / AI Studio switches (1.5.3).
A fake AIStudio2API stands in; no Google, no agy, no network.   Usage: python scripts/panel_controls_test.py"""
import asyncio, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent).resolve()
sys.path.insert(0, str(root))
import httpx

checks = 0


def ok(cond, label):
    global checks
    assert cond, label
    checks += 1
    print('PASS', label)


S = {'accounts': [
    {'id': 'main@example.invalid', 'label': 'main', 'enabled': True, 'state': 'ready', 'proxy': 'socks5://u:SECRET@1.2.3.4:1080',
     'egress': 'socks5://u:SECRET@1.2.3.4:1080', 'locale': 'en-US', 'timezone': 'America/Los_Angeles', 'models': ['gemini-4-hydrogen'],
     'benefit_tier': 'Free', 'message': '', 'active_requests': 0},
    {'id': 'solo@example.invalid', 'label': 'solo', 'enabled': True, 'state': 'ready', 'proxy': '', 'egress': '',
     'locale': 'en-US', 'timezone': 'UTC', 'models': ['gemini-3.8-flash'], 'benefit_tier': 'Free', 'message': '', 'active_requests': 0}],
     'puts': [], 'gen': []}


def handler(request):
    path, method = request.url.path, request.method
    if path == '/api/auth/login':
        return httpx.Response(200, json={'ok': True})
    if path == '/api/accounts' and method == 'GET':
        return httpx.Response(200, json={'accounts': S['accounts']})
    if path == '/api/status':
        return httpx.Response(200, json={'state': 'RUNNING', 'accounts': {'ready': 2}})
    if path == '/api/cooldowns':
        return httpx.Response(200, json={'cooldowns': []})
    if path == '/api/usage':
        return httpx.Response(200, json={'groups': {'account': []}})
    if path == '/api/models':
        return httpx.Response(200, json={'models': []})
    if method == 'PUT' and path.startswith('/api/accounts/'):
        body = json.loads(request.content)
        acc = path.split('/')[3].replace('%40', '@')
        S['puts'].append((acc, body))
        for a in S['accounts']:
            if a['id'] == acc:
                a['enabled'] = body['enabled']
        return httpx.Response(200, json={'account': {}})
    if path == '/v1/chat/completions':
        S['gen'].append(request.headers.get('x-aistudio-account', ''))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'hi'}}]})
    return httpx.Response(404)


with tempfile.TemporaryDirectory() as d:
    os.environ.update(AGY_ROOT=d, AGY_GATEWAY_KEY='client-key', AGY_ACCOUNTS=os.path.join(d, 'accounts'))
    for name, email in (('g9', 'main@example.invalid'), ('g8', 'native@example.invalid'), ('g7', 'other@example.invalid')):
        home = os.path.join(d, 'accounts', name)
        os.makedirs(os.path.join(home, '.gemini', 'antigravity-cli'))
        open(os.path.join(home, '.gemini', 'antigravity-cli', 'antigravity-oauth-token'), 'w').write(json.dumps({'id_token_email': email}))
        open(os.path.join(home, 'proxy.env'), 'w').write('')
    open(os.path.join(d, 'accounts', 'g7', 'disabled'), 'w').close()
    import acp_ant as core
    import acp_panel as panel
    from studio_provider import StudioProvider
    p = StudioProvider({'base_url': 'http://127.0.0.1:9', 'api_key': 'sidecar-key', 'admin_password': 'pw'})
    p._client = lambda timeout=290: httpx.AsyncClient(base_url=p.base, transport=httpx.MockTransport(handler))
    core.STUDIO = p
    from starlette.testclient import TestClient
    client = TestClient(core.app, base_url='http://testserver')
    panel.load_state()
    client.cookies.update({panel.COOKIE: panel._token(int(time.time()) + 600)})
    hdr = {'X-ACP-Panel': '1'}
    core.POOL.scan()
    g9, g7 = core.POOL.accounts['g9'], core.POOL.accounts['g7']

    # ---- Antigravity model list
    g9.models = ['gemini-3.8-flash', 'gemini-4-hydrogen']
    core.POOL.model_levels.update({'gemini-3.8-flash': ['low', 'medium', 'high'], 'gemini-4-hydrogen': ['low', 'medium', 'high', 'xhigh']})
    view = client.get('/panel/api/overview').json()
    by = {a['name']: a for a in view['accounts']}
    ok(by['g9']['models'] == [{'id': 'gemini-3.8-flash', 'levels': ['low', 'medium', 'high']},
                              {'id': 'gemini-4-hydrogen', 'levels': ['low', 'medium', 'high', 'xhigh']}] and by['g8']['models'] == [],
       'each account shows the Antigravity models it offers, with thought levels')

    calls, stops = [], []

    class FakeAcp:
        alive, turns = True, {}

        def __init__(self, acc):
            self.acc = acc

        async def read_models(self):
            calls.append(self.acc.name)
            self.acc.models = ['gemini-3.8-flash']
            return self.acc.models

        async def stop(self):
            stops.append(self.acc.name)
    g7.acp = FakeAcp(g7)
    r = client.post('/panel/api/accounts/g7/models', json={}, headers=hdr)
    ok(r.status_code == 200 and r.json()['models'][0]['id'] == 'gemini-3.8-flash' and calls == ['g7'] and stops == ['g7'],
       'reading the model list works for a switched-off account and stops its process afterwards')
    ok(client.post('/panel/api/accounts/g7/models', json={}).status_code == 403, 'reading models needs the panel header')

    class BrokenAcp(FakeAcp):
        async def read_models(self):
            raise RuntimeError('login expired')
    g7.acp = BrokenAcp(g7)
    r = client.post('/panel/api/accounts/g7/models', json={}, headers=hdr)
    ok(r.status_code == 502 and 'login expired' in r.json()['detail'], 'a failed read is reported, not swallowed')

    # ---- separate switches
    r = client.post('/panel/api/accounts/g9/enabled', json={'enabled': False}, headers=hdr)
    ok(r.status_code == 200 and not core.POOL.accounts['g9'].enabled and S['puts'] == [],
       'the Antigravity switch changes only Antigravity; AI Studio is untouched')
    client.post('/panel/api/accounts/g9/enabled', json={'enabled': True}, headers=hdr)
    r = client.post('/panel/api/studio/main@example.invalid/enabled', json={'enabled': False}, headers=hdr)
    ok(r.status_code == 200 and S['puts'][-1] == ('main@example.invalid', {'label': 'main', 'enabled': False, 'proxy': 'socks5://u:SECRET@1.2.3.4:1080',
                                                                           'locale': 'en-US', 'timezone': 'America/Los_Angeles'})
       and core.POOL.accounts['g9'].enabled, 'the AI Studio switch changes only AI Studio and keeps its proxy, locale and time zone')
    view = client.get('/panel/api/overview').json()
    by = {a['name']: a for a in view['accounts']}
    ok(by['g9']['studio_link'] == 'linked' and by['g9']['studio']['disabled'] is True and by['g9']['enabled'] is True
       and 'SECRET' not in json.dumps(view), 'a switched-off AI Studio session still shows on its account card, with no secret')
    r = client.post('/v1/chat/completions', json={'model': 'aistudio/gemini-4-hydrogen', 'messages': [{'role': 'user', 'content': 'x'}]},
                    headers={'Authorization': 'Bearer client-key', 'X-ACP-Account': 'g9'})
    ok(r.status_code == 400 and 'switched off' in r.json()['error']['message'] and S['gen'] == [],
       'a request pinned to an account whose AI Studio is off is refused, never sent to another account')
    r = client.post('/panel/api/studio/main@example.invalid/enabled', json={'enabled': True}, headers=hdr)
    r2 = client.post('/v1/chat/completions', json={'model': 'aistudio/gemini-4-hydrogen', 'messages': [{'role': 'user', 'content': 'x'}]},
                     headers={'Authorization': 'Bearer client-key', 'X-ACP-Account': 'g9'})
    ok(r.status_code == 200 and r2.status_code == 200 and S['gen'] == ['main@example.invalid'], 'switched on again, it serves requests')
    ok(client.post('/panel/api/studio/nobody@example.invalid/enabled', json={'enabled': False}, headers=hdr).status_code == 404,
       'an unknown AI Studio session is a 404')
    p.heals['main@example.invalid'] = {'at': 0.0, 'count': 3}
    asyncio.run(p.set_enabled('main@example.invalid', True))
    ok('main@example.invalid' not in p.heals, 'switching on clears the automatic-repair pause')
    ok(client.post('/panel/api/studio/solo@example.invalid/enabled', json={'enabled': False}).status_code == 403,
       'the AI Studio switch needs the panel header')

# ---- panel rendering and search (node)
html = (root / 'panel.html').read_text(encoding='utf-8')
script = re.search(r'<script>(.*)</script>', html, re.S).group(1)
code = r"""
const vm=require('vm'),assert=require('assert'),fs=require('fs');const s=fs.readFileSync(process.argv[1],'utf8');
const names=['ago','until','num','kindText','quotaHtml','when','studioState','studioQuota','studioBody','studioBtn','switchHtml','studioSwitch',
 'agModelsHtml','studioHtml','studioOnlyHtml','cardHtml','hayOf','studioHay','matches'];
const src=names.map(n=>{const i=s.indexOf('function '+n+'(');assert(i>=0,n);let d=0,j=s.indexOf('{',i);for(let k=j;k<s.length;k++){if(s[k]==='{')d++;if(s[k]==='}'&&--d===0)return s.slice(i,k+1);}}).join('\n');
const consts=s.match(/const (GROUP|WIN|KIND) = [^\n]*;/g).join('\n');
const c={esc:x=>String(x??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])),copy(){},Date,Math,String,Number,isNaN,
 DATA:{studio:{configured:true,error:''},server:{per_account:3}},QUERY:''};vm.createContext(c);vm.runInContext(consts+'\n'+src,c);
const base={name:'g9',email:'main@example.invalid',enabled:true,status:'ok',status_text:'正常',cooldown_s:0,inflight:0,today:{ok:1,fail:0,in:0,out:0},week:{ok:1,fail:0},
 proxy:'socks5://u@1.2.3.4:1080',egress_ip:'1.2.3.4',country:'US',quota:null,last_check_at:0,models:[{id:'gemini-3.8-flash',levels:['low','medium','high']}],
 studio:{account_id:'main@example.invalid',email:'main@example.invalid',ready:true,state:'ready',disabled:false,exit:'1.2.3.4',models:['gemini-4-hydrogen'],quota:{plan:'Free',used_today:{},limits:[]}},studio_link:'linked'};
let h=c.cardHtml(base);
assert((h.match(/data-act="toggle"/g)||[]).length===1&&(h.match(/data-act="studio-toggle"/g)||[]).length===1,'two separate switches');
assert(h.indexOf('data-act="toggle"')<h.indexOf('<b>AI Studio</b>')&&h.indexOf('data-act="studio-toggle"')>h.indexOf('<b>AI Studio</b>'),'each switch sits in its own section');
assert(h.includes('Antigravity 模型（1）')&&h.includes('gemini-3.8-flash <i>low/medium/high</i>')&&h.includes('data-key="ag:g9"'),'Antigravity model list');
assert(h.includes('AI Studio 模型（1）')&&h.includes('data-key="st:main@example.invalid"'),'AI Studio model list keeps its open state');
h=c.cardHtml({...base,enabled:false,status:'off',status_text:'已关闭',studio:{...base.studio,disabled:true}});
assert(/class="card acc s-off off"/.test(h)&&!/data-act="toggle" data-name="g9" checked/.test(h)&&!/data-act="studio-toggle"[^>]*checked/.test(h),'both off: card dimmed, both switches off');
h=c.cardHtml({...base,enabled:false,status:'off',status_text:'已关闭'});
assert(!/class="card acc s-off off"/.test(h)&&/data-act="studio-toggle"[^>]*checked/.test(h),'Antigravity off, AI Studio on: card stays bright');
h=c.cardHtml({...base,models:[]});assert(h.includes('读取模型列表')&&h.includes('data-act="models"'),'no models yet: read button');
h=c.studioOnlyHtml({...base.studio,disabled:true});assert(h.includes('data-act="studio-toggle"')&&!/studio-toggle"[^>]*checked/.test(h)&&h.includes('card acc off'),'AI-Studio-only card has its switch');
c.QUERY='main';assert(c.matches(c.hayOf(base))&&!c.matches(c.hayOf({...base,name:'g8',email:'x@y',studio:null,egress_ip:'',proxy:''})),'search by e-mail');
c.QUERY='1.2.3.4';assert(c.matches(c.hayOf(base)),'search by exit IP');
c.QUERY='g9 us';assert(c.matches(c.hayOf(base)),'several words must all match');
c.QUERY='已关闭';assert(c.matches(c.hayOf({...base,enabled:false,status_text:'已关闭'}))&&!c.matches(c.hayOf(base)),'search by state');
c.QUERY='solo';assert(c.matches(c.studioHay({email:'solo@example.invalid'})),'AI-Studio-only cards are searchable');
c.QUERY='';assert(c.matches(c.hayOf(base)),'empty search shows everything');
console.log('PANEL_CONTROLS_PASS');
"""
with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
    f.write(script)
r = subprocess.run(['node', '-e', code, f.name], capture_output=True, text=True, encoding='utf-8')
os.unlink(f.name)
ok(r.returncode == 0 and 'PANEL_CONTROLS_PASS' in r.stdout, 'panel renders two switches, model lists and search' + ((' :: ' + r.stderr[-600:]) if r.returncode else ''))
print('%d/%d passed' % (checks, checks))
