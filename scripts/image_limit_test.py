# -*- coding: utf-8 -*-
"""Offline test: images of the latest message are never dropped; too many is a clear 400; only earlier turns give way.

    python3 scripts/image_limit_test.py

No accounts, no network, no running gateway. (Before 1.4.5 the gateway kept only the newest 10 images of a request and
dropped the rest without a word: a caller sending 12 video frames silently lost the first 2.)"""
import atexit
import base64
import os
import shutil
import sys
import tempfile

os.environ["AGY_ROOT"] = tempfile.mkdtemp(prefix="acp-ant-test-")
atexit.register(shutil.rmtree, os.environ["AGY_ROOT"], True)
os.environ["AGY_GATEWAY_KEY"] = "test-key"
os.environ["AGY_MAX_IMAGES"] = "5"          # small limit so the test stays tiny
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import acp_ant as gw  # noqa: E402
from fastapi import HTTPException  # noqa: E402

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 16).decode()
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name, "" if cond else "  -> " + str(detail)[:300]))


def user(n_img, text="look"):
    return {"role": "user", "content": [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": PNG}}] * n_img}


def blocks_for(messages):
    system, msgs, tools, choice = gw.from_openai({"model": "gemini-3.8-flash-medium", "messages": messages})
    return gw.to_acp(system, msgs, tools, choice)[1]


def images(blocks):
    return sum(1 for b in blocks if b.get("type") == "image")


def omitted(blocks):
    return sum(b.get("text", "").count("an earlier image was omitted") for b in blocks if b.get("type") == "text")


check("limit read from AGY_MAX_IMAGES", gw.MAX_IMAGES == 5, gw.MAX_IMAGES)

b = blocks_for([user(5)])
check("latest message at the limit: all 5 images kept", images(b) == 5 and omitted(b) == 0, (images(b), omitted(b)))

try:
    blocks_for([user(6)])
    check("latest message over the limit: 400", False, "no error")
except HTTPException as e:
    check("latest message over the limit: 400 that says how many", e.status_code == 400 and "6" in str(e.detail) and "5" in str(e.detail), e.detail)

b = blocks_for([user(3, "first"), {"role": "assistant", "content": "ok"}, user(4, "second")])
check("history + latest over the limit: latest 4 kept, 2 of 3 earlier dropped with a note", images(b) == 5 and omitted(b) == 2, (images(b), omitted(b)))

b = blocks_for([user(2, "first"), {"role": "assistant", "content": "ok"}, user(3, "second")])
check("history + latest within the limit: nothing dropped", images(b) == 5 and omitted(b) == 0, (images(b), omitted(b)))

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
