"""Offline: gemini-3.8-flash / gemini-4-hydrogen / gemini-4-neon share quota between Antigravity ("gemini-4-neon")
and AI Studio ("aistudio/gemini-4-neon"). Fake AIStudio2API + fake Antigravity generation; no Google, no agy.
Usage: python scripts/shared_quota_test.py"""
import json, os, sys, tempfile, time
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


S = {'status': {}, 'calls': []}       # status per native model (default 200), every request AI Studio got


def sse_for(path, model):
    if path == '/v1/chat/completions':
        return ('data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"from-studio"}}]}\n\ndata: [DONE]\n\n')
    if path == '/v1/messages':
        return ('event: message_start\ndata: {"type":"message_start","message":{"id":"msg_studio"}}\n\n'
                'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"from-studio"}}\n\n'
                'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    return ('event: response.created\ndata: {"type":"response.created"}\n\n'
            'event: response.in_progress\ndata: {"type":"response.in_progress"}\n\n'
            'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"from-studio"}\n\n'
            'event: response.completed\ndata: {"type":"response.completed"}\n\n')


def handler(request):
    path, method = request.url.path, request.method
    if path == '/api/auth/login':
        return httpx.Response(200, json={'ok': True})
    if path == '/api/accounts':
        return httpx.Response(200, json={'accounts': [{'id': 'main@example.invalid', 'label': 'main', 'enabled': True, 'state': 'ready',
                                                       'models': ['gemini-4-neon', 'gemini-3.8-flash', 'other-model']}]})
    if path in ('/api/status', '/api/cooldowns', '/api/usage', '/api/models'):
        return httpx.Response(200, json={'state': 'RUNNING', 'cooldowns': [], 'groups': {}, 'models': [], 'accounts': {'ready': 1}})
    if path in ('/v1/chat/completions', '/v1/messages', '/v1/responses'):
        body = json.loads(request.content)
        S['calls'].append({'path': path, 'model': body['model'], 'account': request.headers.get('x-aistudio-account', ''),
                           'effort': body.get('reasoning_effort') or (body.get('reasoning') or {}).get('effort')})
        st = S['status'].get(body['model'], 200)
        if st != 200:
            return httpx.Response(st, json={'error': {'message': 'limit reached for ' + body['model'], 'type': 'rate_limit'}})
        if body.get('stream'):
            return httpx.Response(200, content=sse_for(path, body['model']).encode(), headers={'content-type': 'text/event-stream'})
        if path == '/v1/messages':
            return httpx.Response(200, json={'type': 'message', 'content': [{'type': 'text', 'text': 'from-studio'}]})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'from-studio'}}], 'output_text': 'from-studio'})
    return httpx.Response(404)


with tempfile.TemporaryDirectory() as d:
    os.environ.update(AGY_ROOT=d, AGY_GATEWAY_KEY='client-key', AGY_ACCOUNTS=os.path.join(d, 'accounts'))
    os.environ.pop('AGY_SHARED_MODELS', None)
    for name, email in (('g1', 'main@example.invalid'), ('g2', 'two@example.invalid')):
        home = os.path.join(d, 'accounts', name)
        os.makedirs(os.path.join(home, '.gemini', 'antigravity-cli'))
        open(os.path.join(home, '.gemini', 'antigravity-cli', 'antigravity-oauth-token'), 'w').write(json.dumps({'id_token_email': email}))
        open(os.path.join(home, 'proxy.env'), 'w').write('')
    import acp_ant as core
    from studio_provider import StudioProvider
    p = StudioProvider({'base_url': 'http://127.0.0.1:9', 'api_key': 'sidecar-key', 'admin_password': 'pw'})
    p._client = lambda timeout=290: httpx.AsyncClient(base_url=p.base, transport=httpx.MockTransport(handler))
    core.STUDIO = p
    core.POOL.scan()
    g1, g2 = core.POOL.accounts['g1'], core.POOL.accounts['g2']
    for a in (g1, g2):
        a.models = ['gemini-4-neon', 'gemini-3.8-flash', 'gemini-4-hydrogen']

    G = {'calls': [], 'mode': 'ok'}
    real_generate = core.generate

    # ---- default thought levels (Antigravity side): neon / hydrogen xhigh, 3.8 flash high, explicit levels win
    import asyncio
    seen = []

    async def fake_run_on(acc, model, effort, system, blocks, deadline):
        seen.append((model, effort))
        yield 'text', 'ok'
    real_run_on, core.run_on = core.run_on, fake_run_on

    async def levels_for(name, override=None):
        seen.clear()
        async for _ in real_generate(name, '', [{'type': 'text', 'text': 'hi'}], override):
            pass
        return seen[-1][1]
    got = {n: asyncio.run(levels_for(n)) for n in ('gemini-4-neon', 'gemini-4-hydrogen', 'gemini-3.8-flash', 'gemini-3.8-flash-medium')}
    ok(got == {'gemini-4-neon': 'xhigh', 'gemini-4-hydrogen': 'xhigh', 'gemini-3.8-flash': 'high', 'gemini-3.8-flash-medium': 'medium'},
       'no level named: neon / hydrogen use xhigh, 3.8 flash uses high; a "-medium" name keeps medium (%s)' % got)
    ok(asyncio.run(levels_for('gemini-4-neon', 'low')) == 'low', 'a reasoning_effort the client sends wins over the default')
    core.run_on = real_run_on
    ok(core.nearest_level('xhigh', ['low', 'medium', 'high']) == 'high' and core.nearest_level('high', ['low', 'high', 'xhigh']) == 'high'
       and core.nearest_level('xhigh', ['low', 'medium', 'high', 'xhigh']) == 'xhigh' and core.nearest_level('medium', []) == 'medium',
       'a level the model does not offer becomes the nearest lower one it has')

    async def fake_generate(model_name, system, blocks, effort_override=None):
        G['calls'].append(model_name)
        if G['mode'] == 'ok':
            yield 'text', 'from-antigravity', 'g1'
        elif G['mode'] == 'late':
            yield 'text', 'partial', 'g1'
            raise core.AcpError('quota exhausted mid-answer', 'QUOTA')
        else:
            raise core.AcpError('all accounts out of quota', G['mode'])
    core.generate = fake_generate
    from starlette.testclient import TestClient
    client = TestClient(core.app, base_url='http://testserver')
    key = {'Authorization': 'Bearer client-key'}

    def reset(mode='ok', status=None, cool=False):
        S['calls'].clear(); G['calls'].clear(); G['mode'] = mode; S['status'] = status or {}
        until = time.time() + 600 if cool else 0
        for a in (g1, g2):
            a.cooldown_until, a.cooldown_kind = until, ('QUOTA' if cool else '')

    def chat(model, stream=False, headers=None, **extra):
        return client.post('/v1/chat/completions', json=dict({'model': model, 'stream': stream, 'messages': [{'role': 'user', 'content': 'hi'}]}, **extra),
                           headers={**key, **(headers or {})})

    ok(core.shared_base('aistudio/gemini-4-neon') == 'gemini-4-neon' and core.shared_base('gemini-3.8-flash-medium') == 'gemini-3.8-flash'
       and core.shared_base('gemini-4-hydrogen') == 'gemini-4-hydrogen' and core.shared_base('antigravity/gemini-4-neon') == 'gemini-4-neon'
       and core.shared_base('aistudio/gemini-4-hydrogen-medium') == 'gemini-4-hydrogen'
       and core.shared_base('Gemini 4 Hydrogen (Medium)') == 'gemini-4-hydrogen'
       and core.shared_base('aistudio/gemini-4-neon-xhigh') == 'gemini-4-neon'
       and core.shared_base('Gemini 4 Neon (High)') == 'gemini-4-neon'
       and core.shared_base('gemini_4_neon') == 'gemini-4-neon'
       and core.shared_base('gemini-4-carbon') == 'gemini-4-carbon'
       and core.shared_base('aistudio/gemini-4-carbon-high') == 'gemini-4-carbon'
       and core.shared_base('Gemini 4 Carbon (High)') == 'gemini-4-carbon'
       and core.shared_base('gemini_4_carbon') == 'gemini-4-carbon'
       and core.shared_base('models/gemini-4-hydrogen') == 'gemini-4-hydrogen'
       and core.shared_base('gemini_4_hydrogen') == 'gemini-4-hydrogen'
       and core.shared_base('other-model') == '' and core.shared_base('aistudio/other-model') == '',
       'shared models (with aistudio/, antigravity/, display names, or thought level) resolve cleanly; others do not')

    # ---- unified Gemini 4 Hydrogen / Neon / Carbon names across AI Studio and Antigravity
    reset()
    r = chat('aistudio/gemini-4-hydrogen-medium')
    ok(r.status_code == 200 and 'from-studio' in r.text and G['calls'] == [] and S['calls'][0]['model'] == 'gemini-4-hydrogen'
       and S['calls'][0]['effort'] == 'medium', 'aistudio/gemini-4-hydrogen-medium strips -medium for AI Studio and keeps effort=medium')
    reset(status={'gemini-4-hydrogen': 429})
    r = chat('aistudio/gemini-4-hydrogen-medium')
    ok(r.status_code == 200 and 'from-antigravity' in r.text and G['calls'] == ['gemini-4-hydrogen-medium'],
       'aistudio/gemini-4-hydrogen-medium falls back to Antigravity gemini-4-hydrogen-medium on 429')
    reset()
    r = chat('Gemini 4 Hydrogen (Medium)')
    ok(r.status_code == 200 and 'from-studio' in r.text and G['calls'] == [] and S['calls'][0]['model'] == 'gemini-4-hydrogen'
       and S['calls'][0]['effort'] == 'medium', 'display name Gemini 4 Hydrogen (Medium) routes to AI Studio first as same model')
    reset()
    r = chat('aistudio/gemini-4-neon-high')
    ok(r.status_code == 200 and 'from-studio' in r.text and G['calls'] == [] and S['calls'][0]['model'] == 'gemini-4-neon'
       and S['calls'][0]['effort'] == 'high', 'aistudio/gemini-4-neon-high strips -high for AI Studio and keeps effort=high')
    reset()
    r = chat('Gemini 4 Neon (Medium)')
    ok(r.status_code == 200 and 'from-studio' in r.text and G['calls'] == [] and S['calls'][0]['model'] == 'gemini-4-neon'
       and S['calls'][0]['effort'] == 'medium', 'display name Gemini 4 Neon (Medium) routes to AI Studio first as same model')
    reset(status={'gemini-4-carbon': 403})
    r = chat('aistudio/gemini-4-carbon-high')
    ok(r.status_code == 200 and 'from-antigravity' in r.text and S['calls'][0]['model'] == 'gemini-4-carbon'
       and S['calls'][0]['effort'] == 'high' and G['calls'] == ['gemini-4-carbon-high'],
       'aistudio/gemini-4-carbon-high normalizes for AI Studio and falls back to Antigravity gemini-4-carbon-high on 403')

    # ---- default STUDIO_FIRST=True: plain X uses AI Studio first, falls back to Antigravity when AI Studio cannot serve
    reset()
    r = chat('gemini-4-neon')
    ok(r.status_code == 200 and 'from-studio' in r.text and G['calls'] == [] and S['calls'][0]['model'] == 'gemini-4-neon'
       and S['calls'][0]['effort'] == 'xhigh', 'STUDIO_FIRST default: plain shared model uses AI Studio first with default effort')
    reset()
    r = chat('gemini-4-hydrogen-medium')
    ok(r.status_code == 200 and 'from-studio' in r.text and G['calls'] == [] and S['calls'][0]['model'] == 'gemini-4-hydrogen'
       and S['calls'][0]['effort'] == 'medium', 'STUDIO_FIRST default: -medium suffix preserved on AI Studio')
    reset(status={'gemini-4-neon': 429})
    r = chat('gemini-4-neon')
    ok(r.status_code == 200 and 'from-antigravity' in r.text and len(S['calls']) == 1 and G['calls'] == ['gemini-4-neon'],
       'STUDIO_FIRST default: AI Studio 429 falls back to Antigravity without ping-pong')
    reset()
    r = chat('gemini-4-carbon-high')
    ok(r.status_code == 200 and 'from-antigravity' in r.text and S['calls'] == [] and G['calls'] == ['gemini-4-carbon-high'],
       'STUDIO_FIRST default: gemini-4-carbon skips non-Ultra AI Studio accounts without a wasted 403')
    reset()
    r = chat('Gemini 4 Carbon (High)')
    ok(r.status_code == 200 and 'from-antigravity' in r.text and S['calls'] == [] and G['calls'] == ['Gemini 4 Carbon (High)'],
       'STUDIO_FIRST default: display name Gemini 4 Carbon (High) resolves cleanly to gemini-4-carbon')
    reset()
    r = chat('antigravity/gemini-4-neon')
    ok(r.status_code == 200 and 'from-antigravity' in r.text and S['calls'] == [],
       'explicit antigravity/ prefix uses Antigravity first even when STUDIO_FIRST is on')

    # ---- with STUDIO_FIRST=False: Antigravity first, AI Studio takeover on cooldown / error
    core.STUDIO_FIRST = False

    # ---- aistudio/X: AI Studio first, Antigravity when AI Studio cannot serve
    reset()
    r = chat('aistudio/gemini-4-neon')
    ok(r.status_code == 200 and 'from-studio' in r.text and G['calls'] == [], 'aistudio/ model served by AI Studio while it has quota')
    reset(status={'gemini-4-neon': 429})
    r = chat('aistudio/gemini-4-neon')
    ok(r.status_code == 200 and 'from-antigravity' in r.text and G['calls'] == ['gemini-4-neon'],
       'AI Studio at its limit (429): the same request is answered by Antigravity\'s gemini-4-neon')
    ok(any('改用 Antigravity' in e['text'] for e in list(core.EVENTS)[-3:]), 'the switch is written to the activity log')
    for st in (403, 503):
        reset(status={'gemini-4-neon': st})
        ok(chat('aistudio/gemini-4-neon').status_code == 200 and G['calls'] == ['gemini-4-neon'], 'AI Studio HTTP %d also switches to Antigravity' % st)
    reset(status={'other-model': 429})
    r = chat('aistudio/other-model')
    ok(r.status_code == 429 and G['calls'] == [], 'a model that is not shared keeps AI Studio\'s own answer (no switch)')
    reset(mode='QUOTA', status={'gemini-4-neon': 429})
    r = chat('aistudio/gemini-4-neon')
    ok(r.status_code == 429 and len(S['calls']) == 1 and G['calls'] == ['gemini-4-neon'],
       'both sides out: the error comes back, AI Studio is not asked twice')

    # ---- plain X: Antigravity first, AI Studio when no Antigravity account can serve
    reset()
    r = chat('gemini-4-neon')
    ok(r.status_code == 200 and 'from-antigravity' in r.text and S['calls'] == [], 'plain name served by Antigravity while it has quota')
    reset(cool=True)
    r = chat('gemini-4-neon')
    ok(r.status_code == 200 and 'from-studio' in r.text and G['calls'] == [] and S['calls'][0]['model'] == 'gemini-4-neon',
       'every Antigravity account cooling down: AI Studio answers the same model straight away')
    reset(cool=True)
    chat('gemini-4-neon-high')
    ok(S['calls'][0]['model'] == 'gemini-4-neon' and S['calls'][0]['effort'] == 'high', 'a "-high" name keeps its thought level on AI Studio')
    reset(cool=True)
    chat('gemini-4-hydrogen')
    ok(S['calls'][0]['effort'] == 'xhigh', 'switched to AI Studio without a level: hydrogen gets its default xhigh')

    # ---- default thought levels (AI Studio side)
    reset()
    chat('aistudio/gemini-4-neon'); chat('aistudio/gemini-3.8-flash'); chat('aistudio/gemini-4-neon', reasoning_effort='low'); chat('aistudio/other-model')
    ok([c['effort'] for c in S['calls']] == ['xhigh', 'high', 'low', None],
       'aistudio/ requests: neon xhigh, 3.8 flash high, the client\'s own level kept, other models untouched')
    reset()
    client.post('/v1/responses', json={'model': 'aistudio/gemini-4-hydrogen', 'input': 'hi'}, headers=key)
    ok(S['calls'][0]['effort'] == 'xhigh', 'Responses requests get the default as reasoning.effort')
    reset(mode='QUOTA')
    r = chat('gemini-4-neon')
    ok(r.status_code == 200 and 'from-studio' in r.text and G['calls'] == ['gemini-4-neon'],
       'the last Antigravity account runs out during the request: AI Studio answers it (non-stream)')
    reset(mode='BAD_REQUEST')
    ok(chat('gemini-4-neon').status_code == 400 and S['calls'] == [], 'a bad request is not retried on AI Studio')
    reset(mode='QUOTA')
    ok(chat('other-model').status_code == 429 and S['calls'] == [], 'a model that is not shared never goes to AI Studio')

    # ---- streams: one clean answer, no duplicated opening event, no error event
    reset(mode='NO_ACCOUNT')
    r = chat('gemini-4-neon', stream=True)
    ok('from-studio' in r.text and r.text.count('[DONE]') == 1 and '"error"' not in r.text, 'chat stream: AI Studio takes over, one [DONE], no error')
    reset(mode='QUOTA')
    r = client.post('/v1/messages', json={'model': 'gemini-4-neon', 'stream': True, 'max_tokens': 50, 'messages': [{'role': 'user', 'content': 'hi'}]}, headers=key)
    ok('from-studio' in r.text and r.text.count('event: message_start') == 1 and 'event: error' not in r.text and 'event: message_stop' in r.text,
       'messages stream: one message_start, AI Studio\'s text, message_stop')
    reset(mode='QUOTA')
    r = client.post('/v1/responses', json={'model': 'gemini-4-neon', 'stream': True, 'input': 'hi'}, headers=key)
    ok('from-studio' in r.text and r.text.count('event: response.created') == 1 and 'response.failed' not in r.text and 'response.completed' in r.text,
       'responses stream: one response.created, AI Studio\'s text, response.completed')
    reset(mode='late')
    r = chat('gemini-4-neon', stream=True)
    ok('partial' in r.text and '"error"' in r.text and S['calls'] == [], 'after part of an answer was sent there is no switch (no mixed answers)')
    reset(mode='QUOTA')
    r = client.post('/v1/responses', json={'model': 'gemini-4-neon', 'input': 'hi'}, headers=key)
    ok(r.status_code == 200 and S['calls'][0]['path'] == '/v1/responses', 'responses (non-stream) switches too')

    # ---- pinned to one account: the other side of the same e-mail only
    reset(mode='QUOTA')
    r = chat('gemini-4-neon', headers={'X-ACP-Account': 'g1'})
    ok(r.status_code == 200 and S['calls'][0]['account'] == 'main@example.invalid', 'pinned request switches to the same e-mail\'s AI Studio')
    reset(status={'gemini-4-neon': 429})
    r = chat('aistudio/gemini-4-neon', headers={'X-ACP-Account': 'g1'})
    ok(r.status_code == 200 and G['calls'] == ['gemini-4-neon'], 'pinned aistudio/ request switches to the same account\'s Antigravity')

    # ---- settings
    core.SHARED_MODELS = frozenset()          # what AGY_SHARED_MODELS="" gives
    reset(status={'gemini-4-neon': 429})
    r = chat('aistudio/gemini-4-neon')
    ok(r.status_code == 429 and G['calls'] == [], 'with sharing turned off (AGY_SHARED_MODELS="") nothing switches')
print('%d/%d passed' % (checks, checks))
