"""Avalia JavaScript pela porta de depuração do Chrome, sem manter o Playwright conectado."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import urllib.parse
import urllib.request


def list_debug_targets(port: int) -> list[dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/json",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    return data if isinstance(data, list) else []


def pick_page_target(pages: list[dict], *, host_hint: str = "") -> dict | None:
    hint = (host_hint or "").lower()
    http_pages = [p for p in pages if str(p.get("type") or "") == "page"]
    if hint:
        for page in http_pages:
            if hint in str(page.get("url") or "").lower():
                return page
    for page in http_pages:
        url = str(page.get("url") or "")
        if url.startswith("http"):
            return page
    return http_pages[0] if http_pages else None


async def _ws_send_text(writer, text: str) -> None:
    data = text.encode("utf-8")
    mask_key = os.urandom(4)
    frame = bytearray()
    frame.append(0x81)
    length = len(data)
    if length <= 125:
        frame.append(0x80 | length)
    elif length <= 65535:
        frame.append(0x80 | 126)
        frame.extend(struct.pack(">H", length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack(">Q", length))
    frame.extend(mask_key)
    masked = bytearray(len(data))
    for i in range(len(data)):
        masked[i] = data[i] ^ mask_key[i % 4]
    frame.extend(masked)
    writer.write(bytes(frame))
    await writer.drain()


async def cdp_eval(port: int, expression: str, *, host_hint: str = ""):
    """Executa uma expressão e devolve o valor. None se a aba não responder."""
    try:
        pages = list_debug_targets(port)
        target = pick_page_target(pages, host_hint=host_hint)
        ws_url = target.get("webSocketDebuggerUrl") if target else None
        if not ws_url:
            return None
        parsed = urllib.parse.urlparse(ws_url)
        key = base64.b64encode(os.urandom(16)).decode()
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        handshake = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        writer.write(handshake.encode())
        await writer.drain()
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
            if not chunk:
                break
            response += chunk
        if b"101" not in response:
            writer.close()
            return None
        message = json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        })
        await _ws_send_text(writer, message)
        value = None
        try:
            async with asyncio.timeout(5):
                while True:
                    header = await reader.readexactly(2)
                    opcode = header[0] & 15
                    length = header[1] & 127
                    if length == 126:
                        length = struct.unpack(">H", await reader.readexactly(2))[0]
                    elif length == 127:
                        length = struct.unpack(">Q", await reader.readexactly(8))[0]
                    mask = await reader.readexactly(4) if header[1] & 128 else None
                    payload = await reader.readexactly(length)
                    if mask:
                        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                    if opcode == 8:
                        break
                    if opcode != 1:
                        continue
                    reply = json.loads(payload)
                    if reply.get("id") != 1:
                        continue
                    result = reply.get("result", {})
                    if "error" not in reply and "exceptionDetails" not in result:
                        value = result.get("result", {}).get("value")
                    break
        finally:
            writer.close()
            await writer.wait_closed()
        return value
    except Exception:
        return None
