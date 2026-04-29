from fastapi import FastAPI, WebSocket
import json, uuid, asyncio

app = FastAPI()
phone_ws = None
pending = {}

async def reader(ws):
    global phone_ws
    try:
        while True:
            data = await ws.receive()
            if "text" in data:
                msg = json.loads(data["text"])
                mid = msg.get("id")
                if mid and mid in pending:
                    pending[mid].set_result(msg)
            elif "bytes" in data:
                b = data["bytes"]
                if len(b) > 36:
                    rid = b[:36].decode()
                    if rid in pending:
                        import base64
                        pending[rid].set_result({"id": rid, "result": base64.b64encode(b[36:]).decode()})
    except:
        phone_ws = None

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
    print("Phone connected!")
    await reader(ws)

@app.post("/cmd")
async def cmd(method: str, params: str = "{}"):
    global phone_ws
    if not phone_ws:
        return {"error": "no phone"}
    cid = str(uuid.uuid4())
    fut = asyncio.get_event_loop().create_future()
    pending[cid] = fut
    await phone_ws.send_text(json.dumps({"id": cid, "method": method, "params": json.loads(params)}))
    try:
        return await asyncio.wait_for(fut, timeout=10)
    except asyncio.TimeoutError:
        return {"error": "timeout"}
    finally:
        pending.pop(cid, None)
