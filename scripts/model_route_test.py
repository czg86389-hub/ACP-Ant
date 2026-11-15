# -*- coding: utf-8 -*-
"""Offline test: a model only some accounts offer (e.g. Gemini 4 Hydrogen on paid accounts) is routed to those accounts,
and /v1/models lists every model the accounts offer.

    python3 scripts/model_route_test.py

No accounts, no network."""
import atexit
import os
import shutil
import sys
import tempfile

os.environ["AGY_ROOT"] = tempfile.mkdtemp(prefix="acp-ant-test-")
atexit.register(shutil.rmtree, os.environ["AGY_ROOT"], True)
os.environ["AGY_GATEWAY_KEY"] = "test-key"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import acp_ant as gw  # noqa: E402

results = []


def check(name, got, want):
    results.append(got == want)
    print("%s  %s%s" % ("PASS" if got == want else "FAIL", name, "" if got == want else "  -> got %r, want %r" % (got, want)))


FREE = ["gemini-3.8-flash", "claude-sonnet-4-6", "gpt-oss-120b-medium"]
PAID = ["gemini-4-argon", "gemini-4-hydrogen", "gemini-3.8-flash", "gpt-oss-120b-medium"]
gw.POOL.scan = lambda: None
gw.POOL.accounts = {n: gw.Account(name=n, home="/nonexistent", models=list(m)) for n, m in
                    (("free1", FREE), ("paid", PAID), ("free2", FREE), ("new", []))}
gw.POOL.accounts["paid"].last_pick = 1.0     # the round-robin would otherwise pick it first anyway


def pick(name):
    model, effort = gw.resolve_model(name)
    acc = gw.POOL.pick(set(), frozenset((model, "%s-%s" % (model, effort))))
    return acc and acc.name


check("hydrogen -> paid account", pick("gemini-4-hydrogen"), "paid")
check("hydrogen-xhigh -> paid account", pick("gemini-4-hydrogen-xhigh"), "paid")
check("xhigh is a thought level", gw.resolve_model("gemini-4-hydrogen-xhigh"), ("gemini-4-hydrogen", "xhigh"))
check("flash -> any account (round robin)", pick("gemini-3.8-flash-medium"), "free1")
check("level in the id (gpt-oss-120b-medium) -> any account", pick("gpt-oss-120b-medium"), "free1")
check("name nobody offers -> any account (default-model fallback)", pick("gpt-4o"), "free1")
gw.POOL.accounts["paid"].enabled = False
check("hydrogen, paid account off -> none", pick("gemini-4-hydrogen"), None)
check("model list: switched-off account left out", "gemini-4-hydrogen" in gw.POOL.models_seen, False)
gw.POOL.accounts["paid"].enabled = True
check("model list: union of accounts", gw.POOL.models_seen,
      ["gemini-3.8-flash", "claude-sonnet-4-6", "gpt-oss-120b-medium", "gemini-4-argon", "gemini-4-hydrogen"])

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
