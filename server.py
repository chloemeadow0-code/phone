from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
import json, uuid, asyncio

app = FastAPI()
phone_ws = None

@app.get("/status")
async def status():
    return {"phone_connected": phone_ws is not None}

@app.get("/ping")
async def ping():
    return {"status": "ok"}

@app.websocket("/ws")
async def phone_endpoint(ws: WebSocket):
    global phone_ws
    await ws.accept()
    phone_ws = ws
    print("Phone connected!")
    try:
        while True:
            data = await ws.receive()
            if "text" in data:
                print(f"Phone: {data['text']}")
            elif "bytes" in data:
                print(f"Phone binary: {len(data['bytes'])} bytes")
    except:
        phone_ws = None
        print("Phone disconnected")

@app.post("/cmd")
async def send_cmd(method: str, params: str = "{}"):
    global phone_ws
    if not phone_ws:
        return {"error": "no phone"}
    cmd = {"id": str(uuid.uuid4()), "method": method, "params": json.loads(params)}
    await phone_ws.send_text(json.dumps(cmd))
    try:
        resp = await asyncio.wait_for(phone_ws.receive_text(), timeout=10)
        return json.loads(resp)
    except:
        return {"error": "timeout"}
