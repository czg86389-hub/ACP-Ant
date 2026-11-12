"""Offline regression: python3 scripts/quota_flow_test.py (no account, agy or network)."""
import asyncio
import importlib.util
import json
import pathlib
import subprocess
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace as NS


async def main():
    source = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
    mode = sys.argv[2] if len(sys.argv) > 2 else "modified"
    spec = importlib.util.spec_from_file_location("quota_flow_panel", source / "acp_panel.py")
    panel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(panel)
    with tempfile.TemporaryDirectory(prefix="acpant-quota-check-") as root:
        acc = NS(name="fixture", home=root, enabled=False, quota=None, last_check_at=0,
                 last_check_ok=None, last_check_msg="", disabled_reason="")
        pathlib.Path(root, "disabled").touch()
        reads = []
        phase = "automatic"
        active = peak = 0

        async def read_quota(account, force=False):
            nonlocal active, peak
            reads.append(phase)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            account.quota = ({"error": "fixture: initial quota unavailable"} if phase == "automatic"
                             else [{"group": "fixture", "buckets": [{"remaining": 1.0}]}])
            return account.quota

        async def logged_in(home):
            return True, "gemini-fixture"

        panel.core = NS(ROOT=root, POOL=NS(accounts={acc.name: acc}, scan=lambda: None,
                        wake=lambda: None, waiting=0, limit=3), PER_ACCOUNT=3,
                        REQUIRE_PROXY=False, AGY="fixture-agy", __version__="fixture",
                        read_quota=read_quota, event=lambda *args: None)
        panel.STATE.update(secret="fixture-secret", settings={"auto_check": True, "check_hours": 6})
        panel.logged_in = logged_in
        panel.update_view = lambda: {}
        panel.account_view = lambda a: {"name": a.name, "enabled": a.enabled, "quota": a.quota,
                                       "today": {}, "usable": a.enabled, "inflight": 0}

        def request(body=None, authenticated=True):
            headers = [(b"x-acp-panel", b"1")]
            if authenticated:
                cookie = panel.COOKIE + "=" + panel._token(int(time.time()) + 60)
                headers.append((b"cookie", cookie.encode()))

            async def receive():
                return {"type": "http.request", "body": json.dumps(body or {}).encode()}

            return panel.Request({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive)

        # Match the UI: refresh the list while completing the add-account wizard.
        await panel.api_overview(request())
        result = await panel.api_check(acc.name, request({"enable_if_ok": True, "light": True}))
        await asyncio.sleep(0.04)
        automatic = len(reads)
        expected = mode == "modified"
        assert automatic == (0 if expected else 2), reads
        assert result["ok"] is expected and acc.enabled is expected, result
        print(f"ADD automatic_quota_reads={automatic} peak_concurrent={peak} ok={result['ok']} enabled={acc.enabled}")
        if expected:
            assert time.time() - acc.last_check_at < 5
            assert time.time() - acc.last_check_at < panel.STATE["settings"]["check_hours"] * 3600
            assert not pathlib.Path(root, "disabled").exists()
            print("PATROL fresh_login_waits_for_normal_interval=True")

        phase = "manual"
        manual = await panel.api_quota(acc.name, request())
        assert isinstance(manual["quota"], list)
        assert reads.count("manual") == 1
        print("MANUAL quota_reads=1 success=True")
        await panel.api_quota_all(request())
        assert reads.count("manual") == 2
        print("MANUAL_ALL additional_quota_reads=1 success=True")
        phase = "light"
        assert (await panel.api_check(acc.name, request({"light": True})))["ok"]
        assert reads.count("light") == 1
        print("EXPLICIT_LIGHT quota_reads=1 success=True")
        before = len(reads)
        try:
            await panel.api_quota(acc.name, request(authenticated=False))
            raise AssertionError("unauthenticated quota request accepted")
        except panel.HTTPException as error:
            assert error.status_code == 401 and len(reads) == before
        print("AUTH unauthenticated_quota_status=401")
        if expected:
            acc.enabled = False
            panel._set_enabled(root, False)

            async def invalid_login(home):
                return False, "sign in"

            panel.logged_in = invalid_login
            bad = await panel.api_check(acc.name, request({"enable_if_ok": True, "light": True}))
            assert not bad["ok"] and not acc.enabled and len(reads) == before
            print("INVALID_LOGIN enabled=False automatic_quota_reads=0")
            fetches = []
            panel.install_info = lambda: {"mode": "manual"}
            panel._fetch_text = lambda url: fetches.append(url) or ""
            assert not panel._silent_sync_files_sync() and not fetches
            print("LOCAL_FIX unmanaged_install_not_overwritten=True")

    js = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[1], 'utf8');
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(code);
const fn = code.match(/function quotaHtml\(a\) \{[\s\S]*?\n\}/)[0];
const result = vm.runInNewContext(fn + '; quotaHtml({})');
const modified = process.argv[2] === 'modified';
assert.equal(result.includes('spin'), !modified);
assert.equal(html.includes('3 完成'), modified);
assert.equal(html.includes('未自动读取额度；'), modified);
console.log('FRONTEND syntax=PASS empty_quota_spinner=' + !modified + ' completion_copy=' + modified);
'''
    html = (source / "panel.html").read_text(encoding="utf-8")
    assert ('3 完成' in html) is (mode == "modified")
    assert ('未自动读取额度；' in html) is (mode == "modified")
    if shutil.which("node"):
        subprocess.run(["node", "-e", js, str(source / "panel.html"), mode], check=True)
    else:
        print("FRONTEND copy=PASS syntax=SKIPPED (optional node not installed)")


asyncio.run(main())
