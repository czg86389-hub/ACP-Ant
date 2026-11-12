# -*- coding: utf-8 -*-
"""Offline test: Antigravity status lines sent back as "answers" are recognised as errors, never passed on.

    python3 scripts/soft_error_test.py

No accounts, no network. (Before 1.4.5 "⚠ You do not have a valid license of this product ... (#3501)" from an account
without a licence went to the caller as if it were the model's answer.)"""
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


def kind(text):
    e = gw.soft_error(text)
    return e.kind if e else None


def check(name, got, want):
    results.append(got == want)
    print("%s  %s%s" % ("PASS" if got == want else "FAIL", name, "" if got == want else "  -> got %r, want %r" % (got, want)))


LICENSE = ("\n⚠ You do not have a valid license of this product. Please contact your administrator to request a license. "
           "If you are not an enterprise user and believe you are receiving this message as an error, please try using the "
           "latest version and logging in again. (#3501)\n")
check("no licence (#3501) -> LICENSE", kind(LICENSE), "LICENSE")
check("other ⚠ status line -> SERVER", kind("⚠ Model produced invalid output.\n## Video overview\n" + "x" * 500), "SERVER")
check("verification -> VERIFY", kind("⚠ Verification Required"), "VERIFY")
check("quota text -> UPSTREAM_LIMIT", kind("⚠ API error (attempt 1): RESOURCE_EXHAUSTED (code 429): exhausted\n"), "UPSTREAM_LIMIT")
check("prompt filter -> BLOCKED", kind("The prompt could not be submitted because it violates the Generative AI Prohibited Use policy"), "BLOCKED")
check("normal answer -> not an error", kind("## 视频总览\n- 视频总时长：25 秒\n" + "正文" * 300), None)
check("answer mentioning a licence later -> not an error", kind("## Overview\n" + "text " * 200 + "you do not have a valid license"), None)
check("LICENSE maps to HTTP 503", gw.ERR_STATUS.get("LICENSE"), 503)
check("⚠ start is held back for checking", gw.could_be_canned("⚠ You do not"), True)

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
