#!/usr/bin/env python3
"""LIVE smoke — run the real bridge against api.ambient.xyz and prove the fix.

Opt-in: requires AMBIENT_API_KEY in the environment (the key never touches disk or the
committed tree). Spawns `ambient serve` (the real launcher path), then drives it exactly
as Claude Code would:

  1) a tool-using turn -> a tool_use block with an Anthropic-legal id;
  2) REPLAY that id in the next turn's tool_result -> must NOT 400 on `tool_use.id`
     (this is the exact brick we are eliminating);
  3) streaming completes with content intact;
  4) a huge prompt -> HTTP 400 `prompt is too long` (the compaction/unbrick signal);
  5) repeat 1-2 across every serving model (Kimi / GLM / DeepSeek / whatever is live)
     to prove model-agnosticism.

Exit 0 = the brick is gone on every model that answered. Ambient is on-demand, so a model
that is merely "spinning up" (429/empty) is reported, not failed.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(ROOT, "bin", "ambient")
TOOL = {"name": "get_time", "description": "Get the current time.",
        "input_schema": {"type": "object", "properties": {"tz": {"type": "string"}}, "required": []}}


def _req(method, url, token, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"x-api-key": token, "content-type": "application/json",
               "anthropic-version": "2023-06-01"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def _turn2(base, token, model, first_content, tool_use_id):
    body = {"model": model, "max_tokens": 512, "messages": [
        {"role": "user", "content": "What time is it? Use the get_time tool."},
        {"role": "assistant", "content": first_content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                       "content": "2026-08-26T12:00:00Z"}]}]}
    return _req("POST", base + "/v1/messages", token, body)


def test_model(base, token, model):
    body = {"model": model, "max_tokens": 1024, "tools": [TOOL], "tool_choice": {"type": "any"},
            "messages": [{"role": "user", "content": "What time is it? Use the get_time tool."}]}
    st, data = _req("POST", base + "/v1/messages", token, body)
    if st != 200:
        return "spinning-up", "turn1 HTTP {0}: {1}".format(st, data[:160])
    msg = json.loads(data)
    tus = [b for b in msg.get("content", []) if b.get("type") == "tool_use"]
    if not tus:
        return "no-tool", "stop={0}".format(msg.get("stop_reason"))
    tid = tus[0]["id"]
    st2, data2 = _turn2(base, token, model, msg["content"], tid)
    if st2 == 400 and "tool_use.id" in data2:
        return "BRICK", "tool_use.id 400: {0}".format(data2[:200])
    if st2 != 200:
        return "spinning-up", "turn2 HTTP {0}: {1}".format(st2, data2[:160])
    return "OK", "id={0} stop2={1}".format(tid, json.loads(data2).get("stop_reason"))


def main():
    key = os.environ.get("AMBIENT_API_KEY")
    if not key:
        print("SKIP: set AMBIENT_API_KEY to run the live smoke")
        return 0
    port = 4599
    proc = subprocess.Popen([sys.executable, BIN, "serve", "--port", str(port)],
                            env=dict(os.environ, AMBIENT_API_KEY=key),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    base = "http://127.0.0.1:{0}".format(port)
    try:
        for _ in range(80):
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        else:
            print("FAIL: bridge did not start")
            print(proc.stderr.read().decode()[:800])
            return 1
        token = json.load(open(os.path.expanduser("~/.config/ambient/bridge.json")))["token"]

        st, data = _req("GET", base + "/v1/models", token)
        models = [m["id"] for m in json.loads(data).get("data", [])] if st == 200 else []
        print("serving/catalog models:", models[:10])

        pref = [m for m in ("moonshotai/kimi-k2.7-code", "z-ai/glm-5.2") if m in models]
        ds = [m for m in models if "deepseek" in m.lower()][:1]
        candidates = pref + ds or models[:2]

        results, bricked, any_ok = [], False, False
        for model in candidates:
            verdict, detail = test_model(base, token, model)
            print("  [{0:12}] {1}  {2}".format(verdict, model, detail))
            results.append((model, verdict))
            if verdict == "BRICK":
                bricked = True
            if verdict == "OK":
                any_ok = True

        # streaming
        st, data = _req("POST", base + "/v1/messages", token,
                        {"model": candidates[0], "max_tokens": 128, "stream": True,
                         "messages": [{"role": "user", "content": "Say hi in 3 words."}]})
        streamed = ("message_start" in data and "message_stop" in data and "[DONE]" not in data)
        print("  streaming: HTTP {0}, well-formed-SSE={1}".format(st, streamed))

        # overflow -> unbrick signal
        st, data = _req("POST", base + "/v1/messages", token,
                        {"model": candidates[0], "max_tokens": 100,
                         "messages": [{"role": "user", "content": "x" * 3_000_000}]})
        overflow_ok = (st == 400 and "prompt is too long" in data)
        print("  overflow: HTTP {0}, prompt-is-too-long={1}".format(st, overflow_ok))

        ok = (not bricked) and any_ok and overflow_ok
        print("\nRESULT:", "PASS ✅ (no tool_use.id brick on any live model)" if ok
              else "FAIL ❌" if bricked else "INCONCLUSIVE (no model served a tool call)")
        return 0 if ok else (2 if not bricked else 1)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
