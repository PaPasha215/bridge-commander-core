"""Interoperability check with the official MCP Python SDK client (not part of the stdlib test suite).

    pip install mcp            # in any virtualenv
    python tests/sdk_compat.py

Starts Bridge Commander over stdio and over Streamable HTTP and drives it with the SDK client.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "sdk-compat-token-0123456789abcdef"


def attr(obj, snake, camel):  # SDK 2.x renamed fields to snake_case
    return getattr(obj, snake) if hasattr(obj, snake) else getattr(obj, camel)


async def exercise(session, root):
    init = await session.initialize()
    tools = {t.name for t in (await session.list_tools()).tools}
    assert {"read_file", "write_file", "edit_block", "start_process", "search_code"} <= tools, tools
    path = os.path.join(root, "sdk.txt")
    res = await session.call_tool("write_file", {"path": path, "content": "hello sdk\n"})
    assert not attr(res, 'is_error', 'isError'), res
    res = await session.call_tool("edit_block", {"file_path": path, "old_string": "sdk", "new_string": "SDK"})
    assert not attr(res, 'is_error', 'isError'), res
    res = await session.call_tool("read_file", {"path": path})
    assert res.content[0].text == "hello SDK\n", res
    res = await session.call_tool("start_process", {"command": "echo from-shell", "timeout_ms": 5000})
    assert "from-shell" in res.content[0].text, res
    res = await session.call_tool("read_file", {"path": "/etc/hosts"})
    assert attr(res, 'is_error', 'isError'), res
    return attr(init, 'server_info', 'serverInfo').version, len(tools)


async def main():
    import importlib.metadata
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    version = importlib.metadata.version("mcp")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        cfg = os.path.join(tmp, "config.json")
        with open(cfg, "w") as f:
            json.dump({"allowedDirectories": [tmp], "auditLog": None}, f)
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), BRIDGE_COMMANDER_CONFIG=cfg)

        params = StdioServerParameters(command=sys.executable, args=["-m", "bridge_commander", "serve"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                print("stdio  OK  sdk=%s server=%s tools=%d" % ((version,) + await exercise(session, tmp)))

        try:
            from mcp.client.streamable_http import streamablehttp_client as http_client
        except ImportError:
            from mcp.client.streamable_http import streamable_http_client as http_client
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-m", "bridge_commander", "serve", "--transport", "http",
                                 "--port", str(port)], env=dict(env, BRIDGE_COMMANDER_TOKEN=TOKEN),
                                stderr=subprocess.DEVNULL)
        try:
            time.sleep(1)
            url = "http://127.0.0.1:%d/mcp" % port
            try:
                ctx = http_client(url, headers={"Authorization": "Bearer " + TOKEN})
            except TypeError:  # SDK 2.x takes an httpx client instead of headers
                import httpx
                ctx = http_client(url, http_client=httpx.AsyncClient(headers={"Authorization": "Bearer " + TOKEN}))
            async with ctx as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    print("http   OK  sdk=%s server=%s tools=%d" % ((version,) + await exercise(session, tmp)))
        finally:
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    asyncio.run(main())
