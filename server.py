"""
Mobilerun Portal Bridge Server
- WebSocket endpoint for Android phone (reverse connection)
- MCP tools via FastMCP mounted at /mcp
- HTTP control endpoints
- Vision analysis via Anthropic API (for non-vision MCP clients)
- Deploy on Zeabur, port 8080
"""

import asyncio
import base64
import json
import uuid
import logging
from contextlib import asynccontextmanager
from typing import Any

import anthropic
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from mcp.server.fastmcp import FastMCP

# ─────────────────────────── logging ───────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────── global state ──────────────────────

phone_ws: WebSocket | None = None
pending: dict[str, asyncio.Future] = {}
_lock = asyncio.Lock()          # protect phone_ws writes

# Anthropic client for vision analysis (reads ANTHROPIC_API_KEY from env)
_anthropic_client = anthropic.AsyncAnthropic()


# ─────────────────────────── core helpers ──────────────────────

async def _cleanup_phone():
    """Called when the phone disconnects. Cancel all waiting futures."""
    global phone_ws
    phone_ws = None
    for fut in pending.values():
        if not fut.done():
            fut.set_exception(ConnectionError("Phone disconnected"))
    pending.clear()
    log.warning("Phone disconnected – pending requests cancelled.")


async def send_command(
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """
    Send a JSON-RPC command to the phone and await the response.
    Thread-safe via asyncio; reader() dispatches replies into pending.
    """
    global phone_ws
    if phone_ws is None:
        raise RuntimeError("No phone connected")

    cid = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    pending[cid] = fut

    payload = json.dumps({"id": cid, "method": method, "params": params or {}})
    try:
        async with _lock:
            await phone_ws.send_text(payload)
        result = await asyncio.wait_for(fut, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        raise TimeoutError(f"Command '{method}' timed out after {timeout}s")
    finally:
        pending.pop(cid, None)


# ─────────────────────────── WebSocket reader ──────────────────

async def reader(ws: WebSocket):
    """
    Continuously read frames from the phone WebSocket.
    Dispatches text JSON replies and binary screenshot frames
    into the corresponding pending Future.
    """
    try:
        while True:
            data = await ws.receive()

            # ── text frame: normal JSON-RPC response ──
            if "text" in data:
                try:
                    msg: dict = json.loads(data["text"])
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

            # ── binary frame: screenshot PNG (first 36 bytes = UUID string) ──
            elif "bytes" in data:
                raw: bytes = data["bytes"]
                if len(raw) > 36:
                    try:
                        rid = raw[:36].decode("ascii")
                    except UnicodeDecodeError:
                        log.warning("Binary frame: cannot decode id prefix, skipping.")
                        continue

                    if rid in pending:
                        fut = pending[rid]
                        if not fut.done():
                            fut.set_result(
                                {
                                    "id": rid,
                                    "status": "success",
                                    "result": base64.b64encode(raw[36:]).decode(),
                                }
                            )
                    else:
                        log.debug("Binary frame: unmatched id=%s", rid)
                else:
                    log.warning("Binary frame too short (%d bytes), ignoring.", len(raw))

    except WebSocketDisconnect:
        log.info("Phone WebSocket disconnected (WebSocketDisconnect).")
    except Exception as exc:
        log.error("reader() error: %s", exc)
    finally:
        await _cleanup_phone()


# ─────────────────────────── FastAPI app ───────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    log.info("Server starting up…")
    yield
    log.info("Server shutting down…")
    await _cleanup_phone()


app = FastAPI(title="Mobilerun Portal Bridge", lifespan=lifespan)


@app.get("/ping")
async def ping():
    return {"status": "ok"}


@app.get("/status")
async def status():
    return {"phone_connected": phone_ws is not None}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """Reverse WebSocket – the Android phone connects here."""
    global phone_ws
    await ws.accept()
    phone_ws = ws
    log.info("Phone connected: %s", ws.client)
    await reader(ws)          # blocks until disconnect


@app.post("/cmd")
async def http_cmd(method: str, params: str = "{}"):
    """Quick HTTP shim for manual testing."""
    try:
        result = await send_command(method, json.loads(params))
        return result
    except RuntimeError as e:
        return {"error": str(e)}
    except TimeoutError as e:
        return {"error": str(e)}


# ─────────────────────────── FastMCP tools ─────────────────────

mcp = FastMCP(
    name="mobilerun-portal",
    instructions="Control an Android phone via the Mobilerun Portal App reverse WebSocket connection.",
)


@mcp.tool()
async def phone_screenshot() -> str:
    """
    Take a screenshot of the phone screen.
    Returns a base64-encoded PNG string.
    Portal App may respond with a binary frame (first 36 bytes = request UUID,
    remainder = PNG bytes) or a JSON reply with a base64 result field.
    Both cases are handled transparently.
    """
    resp = await send_command("screenshot", {}, timeout=15.0)
    result = resp.get("result", "")
    # result is already base64 whether it came from binary or JSON path
    return result


@mcp.tool()
async def phone_analyze_screen(
    question: str = "描述当前屏幕上显示的内容，包括所有可见的文字、按钮和界面元素",
) -> str:
    """
    截图并用视觉模型（claude-opus-4-5）分析屏幕内容，返回文字描述。
    当控制端模型不支持图片时，用此工具代替 phone_screenshot()。
    question: 你想问关于当前屏幕的具体问题，默认是描述全部内容。
    示例: "登录按钮在哪里？", "输入框里现在有什么文字？", "当前页面是什么App？"
    """
    # 1. 截图
    screenshot_b64 = await phone_screenshot()
    if not screenshot_b64:
        return "截图失败，无法分析屏幕"

    # 2. 调用视觉模型分析
    resp = await _anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_b64,
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
    return resp.content[0].text


@mcp.tool()
async def phone_tap_by_description(target: str) -> str:
    """
    截图后由视觉模型识别目标元素坐标并自动点击。
    适合不支持视觉的控制端模型，只需描述要点击的内容即可。
    target: 要点击的元素描述，例如 "登录按钮"、"搜索框"、"返回箭头"
    """
    # 1. 截图
    screenshot_b64 = await phone_screenshot()
    if not screenshot_b64:
        return "截图失败，无法定位元素"

    # 2. 让视觉模型返回坐标 JSON
    prompt = (
        f"请在图片中找到"{target}"，返回其中心点坐标。"
        f"只返回 JSON，格式: {{\"x\": 数字, \"y\": 数字, \"found\": true/false, \"reason\": \"说明\"}}"
        f"坐标单位是像素，原点在左上角。如果找不到，found 返回 false。"
    )
    vision_resp = await _anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=128,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    raw = vision_resp.content[0].text.strip()

    # 3. 解析坐标
    try:
        # 去掉可能的 markdown 代码块
        clean = raw.replace("```json", "").replace("```", "").strip()
        coords = json.loads(clean)
    except json.JSONDecodeError:
        return f"视觉模型返回格式无法解析: {raw}"

    if not coords.get("found", False):
        return f"未找到目标元素 "{target}"：{coords.get('reason', '未知原因')}"

    x, y = int(coords["x"]), int(coords["y"])

    # 4. 点击
    tap_resp = await send_command("tap", {"x": x, "y": y})
    status = tap_resp.get("status", "unknown")
    return f"已点击 "{target}" 坐标 ({x}, {y})，状态: {status}"


@mcp.tool()
async def phone_tap(x: int, y: int) -> str:
    """Tap the screen at coordinates (x, y)."""
    resp = await send_command("tap", {"x": x, "y": y})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_swipe(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration: int = 300,
) -> str:
    """
    Swipe from (start_x, start_y) to (end_x, end_y).
    duration is in milliseconds (default 300 ms).
    """
    resp = await send_command(
        "swipe",
        {
            "startX": start_x,
            "startY": start_y,
            "endX": end_x,
            "endY": end_y,
            "duration": duration,
        },
    )
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_input_text(text: str) -> str:
    """
    Type text into the currently focused input field.
    The text is base64-encoded before sending to avoid encoding issues.
    """
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    resp = await send_command("inputText", {"text": encoded, "base64": True})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_press_key(key_code: int) -> str:
    """
    Press an Android key by its key code.
    Common codes: 3=HOME, 4=BACK, 24=VOL_UP, 25=VOL_DOWN, 26=POWER, 66=ENTER.
    """
    resp = await send_command("pressKey", {"keyCode": key_code})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_press_back() -> str:
    """Press the Back button (global action 1)."""
    resp = await send_command("globalAction", {"action": 1})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_press_home() -> str:
    """Press the Home button (global action 2)."""
    resp = await send_command("globalAction", {"action": 2})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_launch_app(package: str) -> str:
    """
    Launch an Android app by its package name.
    Example: package='com.android.settings'
    """
    resp = await send_command("launchApp", {"package": package})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_stop_app(package: str) -> str:
    """
    Force-stop an Android app by its package name.
    Example: package='com.example.myapp'
    """
    resp = await send_command("stopApp", {"package": package})
    return resp.get("status", "unknown")


@mcp.tool()
async def phone_get_state() -> str:
    """
    Retrieve the current accessibility tree (UI hierarchy) of the screen.
    Returns a JSON string describing all visible nodes.
    """
    resp = await send_command("getState", {})
    result = resp.get("result", "")
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


@mcp.tool()
async def phone_get_packages() -> str:
    """
    Get the list of all installed app packages on the device.
    Returns a JSON array of package name strings.
    """
    resp = await send_command("getPackages", {})
    result = resp.get("result", [])
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


@mcp.tool()
async def phone_keep_awake(enabled: bool) -> str:
    """
    Enable or disable keep-screen-awake mode on the device.
    enabled=True prevents the screen from turning off.
    """
    resp = await send_command("keepAwake", {"enabled": enabled})
    return resp.get("status", "unknown")


# ─────────────────────────── mount MCP ─────────────────────────
# streamable_http_app mounts the MCP server as an ASGI sub-app at /mcp

app.mount("/mcp", mcp.streamable_http_app())
