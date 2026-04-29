"""
Mobilerun Portal Bridge Server
- WebSocket endpoint for Android phone (reverse connection)
- MCP tools via FastMCP mounted at /mcp/sse
- HTTP control endpoints
- Vision analysis via Doubao (volcengine ark)
- Deploy on Zeabur, port 8080
"""

import asyncio
import base64
import json
import uuid
import logging

from volcenginesdkarkruntime import AsyncArk
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport

# ─────────────────────────── logging ───────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────── global state ──────────────────────

phone_ws = None
pending = {}
_lock = asyncio.Lock()

_vision_client = AsyncArk()  # reads ARK_API_KEY from env

# ─────────────────────────── app & mcp ─────────────────────────

app = FastAPI(title="Mobilerun Portal Bridge")
mcp = FastMCP(
    name="mobilerun-portal",
    instructions="Control an Android phone via the Mobilerun Portal App reverse WebSocket connection.",
)

# ─────────────────────────── startup / shutdown ────────────────

@app.on_event("startup")
async def on_startup():
    log.info("Server starting up...")

@app.on_event("shutdown")
async def on_shutdown():
    log.info("Server shutting down...")
    await _cleanup_phone()

# ─────────────────────────── core helpers ──────────────────────

async def _cleanup_phone():
    global phone_ws
    phone_ws = None
    for fut in list(pending.values()):
        if not fut.done():
            fut.set_exception(ConnectionError("Phone disconnected"))
    pending.clear()
    log.warning("Phone disconnected - pending requests cancelled.")


async def send_command(method, params=None, timeout=10.0):
    global phone_ws
    if phone_ws is None:
        raise RuntimeError("No phone connected")

    cid = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    pending[cid] = fut

    payload = json.dumps({"id": cid, "method": method, "params": params or {}})
    try:
        async with _lock:
            await phone_ws.send_text(payload)
        result = await asyncio.wait_for(fut, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        raise TimeoutError("Command '{}' timed out after {}s".format(method, timeout))
    finally:
        pending.pop(cid, None)


# ─────────────────────────── WebSocket reader ──────────────────

async def reader(ws):
    try:
        while True:
            data = await ws.receive()

            if "text" in data:
                try:
                    msg = json.loads(data["text"])
                except json.JSONDecodeError:
                    log.warning("Received non-JSON text frame, ignoring.")
                    continue

                mid = msg.get("id")
                if mid and mid in pending:
                    fut = pending[mid]
                    if not fut.done():
                        fut.set_result(msg)
                else:
                    log.debug("Unmatched text message id=%s", mid)

            elif "bytes" in data:
                raw = data["bytes"]
                if len(raw) > 36:
                    try:
                        rid = raw[:36].decode("ascii")
                    except UnicodeDecodeError:
                        log.warning("Binary frame: cannot decode id prefix, skipping.")
                        continue

                    if rid in pending:
                        fut = pending[rid]
                        if not fut.done():
                            fut.set_result({
                                "id": rid,
                                "status": "success",
                                "result": base64.b64encode(raw[36:]).decode(),
                            })
                    else:
                        log.debug("Binary frame: unmatched id=%s", rid)
                else:
                    log.warning("Binary frame too short (%d bytes), ignoring.", len(raw))

    except WebSocketDisconnect:
        log.info("Phone WebSocket disconnected.")
    except Exception as exc:
        log.error("reader() error: %s", exc)
    finally:
        await _cleanup_phone()


# ─────────────────────────── HTTP endpoints ────────────────────

@app.get("/ping")
async def ping():
    return {"status": "ok"}


@app.get("/status")
async def status():
    return {"phone_connected": phone_ws is not None}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global phone_ws
    await ws.accept()
    phone_ws = ws
    log.info("Phone connected: %s", ws.client)
    await reader(ws)


@app.post("/cmd")
async def http_cmd(method: str, params: str = "{}"):
    try:
        result = await send_command(method, json.loads(params))
        return result
    except RuntimeError as e:
        return {"error": str(e)}
    except TimeoutError as e:
        return {"error": str(e)}


# ─────────────────────────── MCP tools ─────────────────────────

@mcp.tool()
async def phone_screenshot():
    """Take a screenshot. Returns base64-encoded PNG string."""
    resp = await send_command("screenshot", {}, timeout=15.0)
    return resp.get("result", "")


@mcp.tool()
async def phone_analyze_screen(question="描述当前屏幕上显示的内容，包括所有可见的文字、按钮和界面元素"):
    """
    截图并用豆包视觉模型分析屏幕内容，返回文字描述。
    question: 你想问关于当前屏幕的具体问题。
    """
    screenshot_b64 = await phone_screenshot()
    if not screenshot_b64:
        return "截图失败，无法分析屏幕"

    resp = await _vision_client.chat.completions.create(
        model="ep-20260421160843-l48q6",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + screenshot_b64
                        },
                    },
                    {
                        "type": "text",
                        "text": question,
                    },
                ],
            }
        ],
    )
    return resp.choices[0].message.content


@mcp.tool()
async def phone_tap_by_description(target):
    """
    截图后由豆包视觉模型识别目标元素坐标并自动点击。
    target: 要点击的元素描述，例如 登录按钮、搜索框、返回箭头
    """
    screenshot_b64 = await phone_screenshot()
    if not screenshot_b64:
        return "截图失败，无法定位元素"

    prompt = (
        "请在图片中找到\"" + target + "\"，返回其中心点坐标。"
        "只返回JSON，格式: {\"x\": 数字, \"y\": 数字, \"found\": true/false, \"reason\": \"说明\"}"
        "坐标单位是像素，原点在左上角。如果找不到，found返回false。"
    )

    resp = await _vision_client.chat.completions.create(
        model="ep-20260421160843-l48q6",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + screenshot_b64
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    )

    raw = resp.choices[0].message.content.strip()
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        coords = json.loads(clean)
    except json.JSONDecodeError:
        return "视觉模型返回格式无法解析: " + raw

    if not coords.get("found", False):
        return "未找到目标元素\"" + target + "\"：" + coords.get("reason", "未知原因")

    x = int(coords["x"])
    y = int(coords["y"])
    tap_resp = await send_command("tap", {"x": x, "y": y})
    return "已点击\"" + target + "\"坐标 ({}, {})，状态: {}".format(x, y, tap_resp.get("status", "unknown"))


@mcp.tool()
async def phone_tap(x, y):
    """Tap the screen at coordinates (x, y)."""
    resp = await send_command("tap", {"x": x, "y": y})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_swipe(start_x, start_y, end_x, end_y, duration=300):
    """Swipe from (start_x, start_y) to (end_x, end_y). duration in milliseconds."""
    resp = await send_command("swipe", {
        "startX": start_x,
        "startY": start_y,
        "endX": end_x,
        "endY": end_y,
        "duration": duration,
    })
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_input_text(text):
    """Type text into the currently focused input field."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    resp = await send_command("inputText", {"text": encoded, "base64": True})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_press_key(key_code):
    """Press an Android key by its key code. Common: 3=HOME 4=BACK 66=ENTER."""
    resp = await send_command("pressKey", {"keyCode": key_code})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_press_back():
    """Press the Back button (global action 1)."""
    resp = await send_command("globalAction", {"action": 1})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_press_home():
    """Press the Home button (global action 2)."""
    resp = await send_command("globalAction", {"action": 2})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_launch_app(package):
    """Launch an Android app by package name. Example: com.android.settings"""
    resp = await send_command("launchApp", {"package": package})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_stop_app(package):
    """Force-stop an Android app by package name."""
    resp = await send_command("stopApp", {"package": package})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_get_state():
    """Retrieve the current accessibility tree (UI hierarchy) of the screen."""
    resp = await send_command("getState", {})
    result = resp.get("result", "")
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


@mcp.tool()
async def phone_get_packages():
    """Get the list of all installed app packages on the device."""
    resp = await send_command("getPackages", {})
    result = resp.get("result", [])
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


@mcp.tool()
async def phone_keep_awake(enabled):
    """Enable or disable keep-screen-awake. enabled=True prevents screen off."""
    resp = await send_command("keepAwake", {"enabled": enabled})
    return resp.get("status", "unknown")


# ─────────────────────────── MCP SSE transport ─────────────────
# Bypass built-in host validation by using the low-level SSE transport directly

sse_transport = SseServerTransport("/mcp/messages/")


@app.get("/mcp/sse")
async def mcp_sse(request: Request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


@app.post("/mcp/messages/")
async def mcp_messages(request: Request):
    await sse_transport.handle_post_message(
        request.scope, request.receive, request._send
    )
