import asyncio
import websockets
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import os
from dotenv import load_dotenv
import httpx
import json
import requests

load_dotenv()

templates = Jinja2Templates(directory="templates")
API_URL = os.getenv("LYZR_API_URL", "https://agent-prod.studio.lyzr.ai/v3/inference/chat/")
API_KEY = os.getenv("LYZR_API_KEY")
UID = os.getenv("USER_ID")
AID = os.getenv("AGENT_ID")
SID = os.getenv("SESSION_ID")

app = FastAPI()

ws_client = None
msg_queue = None

async def send_to_api(msg: str) -> str:
    payload = {
        "user_id": UID,
        "agent_id": AID,
        "session_id": SID,
        "message": msg
    }
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY}
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.post(API_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get("response", "No response from API")

def get_prs(owner, repo, state="open", token=None):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = {"Authorization": f"token {token}"} if token else {}
    all_items = []
    page = 1
    while True:
        params = {"state": state, "per_page": 100, "page": page}
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        prs = response.json()
        if not prs:
            break
        all_items.extend(prs)
        page += 1
    return all_items

async def process_prs(ws, prs):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for pr in prs:
            try:
                p_url = pr.get("patch_url")
                if not p_url:
                    raise ValueError("PR has no patch_url")
                p_res = await client.get(p_url)
                p_res.raise_for_status()
                p_text = p_res.text
                r_text = await send_to_api(p_text)
                payload = {
                    "pr_number": pr['number'],
                    "pr_title": pr['title'],
                    "response": r_text
                }
                await ws.send(json.dumps(payload))
            except Exception as e:
                err = {"pr_number": pr['number'], "error": str(e)}
                if ws:
                    try:
                        await ws.send(json.dumps(err))
                    except websockets.ConnectionClosed:
                        global ws_client
                        ws_client = None

async def sender():
    global ws_client
    while True:
        msg = await msg_queue.get()
        if ws_client:
            try:
                await ws_client.send(msg)
            except websockets.ConnectionClosed:
                ws_client = None

async def handle_ws(ws):
    global ws_client
    try:
        async for msg in ws:
            if msg == "__register_streamlit__":
                ws_client = ws
                try:
                    prs = get_prs(owner="DhruvVayugundla", repo="testing")
                    asyncio.create_task(process_prs(ws_client, prs))
                except Exception as e:
                    await ws_client.send(json.dumps({"error": str(e)}))
                continue
            if ws == ws_client:
                print(f"Reply: {msg}")
            else:
                await msg_queue.put(msg)
    except websockets.ConnectionClosed:
        if ws == ws_client:
            ws_client = None

async def start_ws():
    server = await websockets.serve(handle_ws, "0.0.0.0", 8765)
    await server.wait_closed()

@app.post("/webhook")
async def webhook(req: Request):
    body = await req.json()
    pr = body.get("pull_request")
    if not pr or "patch_url" not in pr:
        return {"status": "error", "message": "Missing patch_url"}
    p_url = pr["patch_url"]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            res = await client.get(p_url)
            res.raise_for_status()
            p_text = res.text
        except Exception as e:
            return {"status": "error", "message": str(e)}
    if msg_queue:
        info = {
            "user_login": pr["user"]["login"],
            "user_id": pr["user"]["id"],
            "title": pr["title"],
            "number": body["number"],
            "action": body["action"]
        }
        r_text = await send_to_api(p_text)
        combo = {"pull_request": info, "response": r_text}
        msg_queue.put_nowait(str(combo))
        return {"status": "sent", "message": combo}
    return {"status": "error", "message": "WS not ready"}

@app.get("/", response_class=HTMLResponse)
async def home(req: Request):
    return templates.TemplateResponse("listener.html", {"request": req})

@app.head("/")
async def head():
    return Response(status_code=200)

async def run():
    global msg_queue
    msg_queue = asyncio.Queue()

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, loop="asyncio", lifespan="on")
    server = uvicorn.Server(config)

    await asyncio.gather(
        start_ws(),
        sender(),
        server.serve()
    )

if __name__ == "__main__":
    asyncio.run(run())
