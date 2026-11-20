# -*- coding: utf-8 -*-
"""Offline test: Antigravity tool-call early stop (</tool_calls> / </function_calls>) and pending-task cleanup.

    python3 scripts/tool_stop_test.py

No accounts, no network. Before 1.5.6, ACP had no stop sequence so models on long multi-turn agent histories kept
hallucinating fake <tool_result> blocks after </tool_calls> for minutes while ToolStream silently buffered everything."""
import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import time

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


# 1. tool_calls_end + parse_tool_calls basics and edge cases
sample = 'Checking server.\n<tool_calls>\n[{"name": "Bash", "arguments": {"command": "ls"}}]\n</tool_calls>\n\n<tool_result name="Bash">\nfake\n</tool_result>'
end = gw.tool_calls_end(sample)
check("basic </tool_calls> end detected", end is not None and sample[:end].endswith("</tool_calls>"), True)
c, calls = gw.parse_tool_calls(sample[:end])
check("basic parse content", c, "Checking server.")
check("basic parse calls", calls, [{"name": "Bash", "arguments": {"command": "ls"}}])

nested = 'Editing.\n<tool_calls>\n[{"name": "Edit", "arguments": {"text": "TAG = \\"</tool_calls>\\""}}]\n</tool_calls>\n<tool_result>fake</tool_result>'
first_close = nested.find("</tool_calls>") + len("</tool_calls>")
check("literal </tool_calls> inside JSON arg does NOT stop early", gw.tool_calls_end(nested[:first_close]), None)
real_end = gw.tool_calls_end(nested)
check("outer </tool_calls> after nested literal IS detected", real_end is not None and real_end > first_close, True)
_, calls2 = gw.parse_tool_calls(nested[:real_end])
check("nested literal </tool_calls> parsed intact", calls2, [{"name": "Edit", "arguments": {"text": 'TAG = "</tool_calls>"'}}])

parallel = '<tool_calls>\n[{"name": "Bash", "arguments": {"cmd": "df -h"}}, {"name": "Bash", "arguments": {"cmd": "free -h"}}]\n</tool_calls>\n<tool_result>fake</tool_result>'
pe = gw.tool_calls_end(parallel)
_, pcalls = gw.parse_tool_calls(parallel[:pe])
check("parallel tool calls preserved", [x["arguments"]["cmd"] for x in (pcalls or [])], ["df -h", "free -h"])

alt = 'Running.\n<function_calls>\n<invoke name="Bash"><parameter name="cmd">pwd</parameter></invoke>\n</function_calls>\n<tool_result>fake</tool_result>'
ae = gw.tool_calls_end(alt)
check("<function_calls> end detected", ae is not None and alt[:ae].endswith("</function_calls>"), True)
ac, acalls = gw.parse_tool_calls(alt[:ae])
check("<function_calls> parsed", (ac, acalls), ("Running.", [{"name": "Bash", "arguments": {"cmd": "pwd"}}]))

check("incomplete JSON does not trigger early stop", gw.tool_calls_end('<tool_calls>[{"name": "Bash"'), None)
check("invalid JSON inside tags does not trigger early stop", gw.tool_calls_end("<tool_calls>not json</tool_calls>"), None)
check("empty tool array does not trigger early stop", gw.tool_calls_end("<tool_calls>[]</tool_calls>"), None)


# 2. Async _run_session tests
class MockAcc:
    models = ["gemini-4-neon"]


class MockAcp:
    def __init__(self, finish_immediately: bool = False, stop_reason: str = "end_turn"):
        self.acc = MockAcc()
        self.cancelled: list[str] = []
        self.prompt_cancelled = False
        self.prompt_calls = 0
        self.finish_immediately = finish_immediately
        self.stop_reason = stop_reason

    async def request(self, method, params, timeout):
        if method == "session/prompt":
            self.prompt_calls += 1
            if self.finish_immediately:
                await asyncio.sleep(0.03)
                return {"stopReason": self.stop_reason, "usage": {"inputTokens": 100, "outputTokens": 20}}
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.prompt_cancelled = True
                raise
            return {"stopReason": self.stop_reason}
        return {}

    async def notify(self, method, params):
        if method == "session/cancel":
            self.cancelled.append(params["sessionId"])


SESS = {"sessionId": "sid-1", "configOptions": [{"id": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]}]}


async def run_async_tests():
    # 2a. stop_on_tools=True cancels immediately on chunk-split </tool_calls>, ignores thought chunks, strips trailing <tool_result>
    acp = MockAcp()
    turn = gw.Turn()

    async def feeder():
        await asyncio.sleep(0.01)
        turn.on_update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": '<tool_calls>[{"name":"thought_only","arguments":{}}]</tool_calls>'}})
        await asyncio.sleep(0.01)
        turn.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": 'Running.\n<tool_calls>\n[{"name": "Bash", "arguments": {"cmd": "ls"}}]\n</tool_'}})
        await asyncio.sleep(0.01)
        turn.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": 'calls>\n\n<tool_result name="Bash">HALLUCINATED</tool_result>'}})
        await asyncio.sleep(0.01)
        turn.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "MORE FAKE TURNS"}})

    ft = asyncio.create_task(feeder())
    t0 = time.time()
    chunks = []
    async for kind, txt in gw._run_session(acp, SESS, turn, "gemini-4-neon", None, [{"type": "text", "text": "hi"}], time.time() + 30, False, stop_on_tools=True):
        if kind == "text":
            chunks.append(txt)
    dt = time.time() - t0
    await ft
    full = "".join(chunks)
    check("early-stop finishes fast (<1s)", dt < 1.0, True)
    check("session/cancel sent", acp.cancelled, ["sid-1"])
    check("session/prompt cancelled", acp.prompt_cancelled, True)
    check("hallucinated <tool_result> stripped", "HALLUCINATED" not in full and full.endswith("</tool_calls>"), True)
    ts = gw.ToolStream(True)
    streamed = "".join(ts.feed(x) for x in chunks)
    tail, parsed = ts.finish()
    check("ToolStream emits prose + parsed call cleanly", (streamed, tail, parsed), ("Running.\n", "", [{"name": "Bash", "arguments": {"cmd": "ls"}}]))

    # 2b. Short tool call arriving inside the 400-char held buffer (whitespace prefix) still flushes and cancels
    acp2 = MockAcp()
    turn2 = gw.Turn()

    async def feeder2():
        await asyncio.sleep(0.01)
        turn2.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "  "}})
        await asyncio.sleep(0.01)
        turn2.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": '<tool_calls>[{"name":"ping","arguments":{}}]</tool_calls>\n<tool_result>fake</tool_result>'}})

    ft2 = asyncio.create_task(feeder2())
    chunks2 = []
    async for kind, txt in gw._run_session(acp2, SESS, turn2, "gemini-4-neon", None, [{"type": "text", "text": "hi"}], time.time() + 30, False, stop_on_tools=True):
        if kind == "text":
            chunks2.append(txt)
    await ft2
    full2 = "".join(chunks2)
    check("held-buffer short tool call flushed and truncated", full2.strip() == '<tool_calls>[{"name":"ping","arguments":{}}]</tool_calls>', True)
    check("held-buffer short tool call sent session/cancel", acp2.cancelled, ["sid-1"])

    # 2c. stop_on_tools=False does NOT cancel on </tool_calls>
    acp3 = MockAcp(finish_immediately=True)
    turn3 = gw.Turn()
    turn3.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": '<tool_calls>[{"name":"Bash","arguments":{}}]</tool_calls>\nexplanation after'}})
    chunks3 = []
    async for kind, txt in gw._run_session(acp3, SESS, turn3, "gemini-4-neon", None, [{"type": "text", "text": "hi"}], time.time() + 30, False, stop_on_tools=False):
        if kind == "text":
            chunks3.append(txt)
    check("stop_on_tools=False keeps full text without cancelling", ("explanation after" in "".join(chunks3), acp3.cancelled), (True, []))

    # 2d. When fut finishes with stopReason=max_tokens right as </tool_calls> is queued, do NOT auto-continue
    acp4 = MockAcp(finish_immediately=True, stop_reason="max_tokens")
    turn4 = gw.Turn()
    turn4.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": '<tool_calls>[{"name":"Bash","arguments":{"cmd":"id"}}]</tool_calls>\n<tool_result>fake</tool_result>'}})
    chunks4 = []
    async for kind, txt in gw._run_session(acp4, SESS, turn4, "gemini-4-neon", None, [{"type": "text", "text": "hi"}], time.time() + 30, False, stop_on_tools=True):
        if kind == "text":
            chunks4.append(txt)
    check("no MAX_CONTINUE after complete tool call", acp4.prompt_calls, 1)
    check("drain path strips trailing hallucination", "fake" not in "".join(chunks4) and "".join(chunks4).endswith("</tool_calls>"), True)

    # 2e. AcpProcess.request() removes rid from self.pending on cancellation
    class DummyAcc:
        name = "test"
        home = os.environ["AGY_ROOT"]

    proc = gw.AcpProcess(DummyAcc())
    async def noop_send(msg):
        return None
    proc._send = noop_send
    task = asyncio.create_task(proc.request("session/prompt", {}, 30))
    await asyncio.sleep(0.01)
    check("pending populated while waiting", len(proc.pending), 1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("pending cleaned on cancellation", len(proc.pending), 0)

    # 2f. STOP_ON_TOOLS ContextVar propagates across asyncio.create_task (with_heartbeat) when stop_on_tools is omitted
    acp5 = MockAcp()
    turn5 = gw.Turn()
    token = gw.STOP_ON_TOOLS.set(True)
    try:
        async def run_in_task():
            async def feeder5():
                await asyncio.sleep(0.01)
                turn5.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": '<tool_calls>[{"name":"Bash","arguments":{"cmd":"uptime"}}]</tool_calls>\n<tool_result>fake</tool_result>'}})
            ft5 = asyncio.create_task(feeder5())
            out5 = []
            async for kind, txt in gw._run_session(acp5, SESS, turn5, "gemini-4-neon", None, [{"type": "text", "text": "hi"}], time.time() + 30, False):
                if kind == "text":
                    out5.append(txt)
            await ft5
            return "".join(out5)
        full5 = await asyncio.create_task(run_in_task())
    finally:
        gw.STOP_ON_TOOLS.reset(token)
    check("ContextVar STOP_ON_TOOLS cancels in child task", (acp5.cancelled, "fake" not in full5), (["sid-1"], True))


asyncio.run(run_async_tests())
print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
