import asyncio
import websockets
from fastapi import FastAPI, Request
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
LYZR_API_URL = os.getenv("LYZR_API_URL", "https://agent-prod.studio.lyzr.ai/v3/inference/chat/")
LYZR_API_KEY = os.getenv("LYZR_API_KEY")
USER_ID = os.getenv("USER_ID")
AGENT_ID = os.getenv("AGENT_ID")
SESSION_ID = os.getenv("SESSION_ID")

app = FastAPI()

listener_client = None
listener_queue = None  # will initialize in main()


# -------------------- Helper functions --------------------

async def forward_to_lyzr(message: str) -> str:
    payload = {
        "user_id": USER_ID,
        "agent_id": AGENT_ID,
        "session_id": SESSION_ID,
        "message": message
    }
    headers = {"Content-Type": "application/json", "x-api-key": LYZR_API_KEY}
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.post(LYZR_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get("response", "No response from LyZR")


def get_all_pull_requests(owner, repo, state="open", token=None):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = {"Authorization": f"token {token}"} if token else {}
    all_prs = []
    page = 1
    while True:
        params = {"state": state, "per_page": 100, "page": page}
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        prs = response.json()
        if not prs:
            break
        all_prs.extend(prs)
        page += 1
    return all_prs


async def process_prs_and_forward(listener_ws, prs):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for pr in prs:
            try:
                patch_url = pr.get("patch_url")
                if not patch_url:
                    raise ValueError("PR has no patch_url")
                patch_response = await client.get(patch_url)
                patch_response.raise_for_status()
                patch_content = patch_response.text
                response_text = await forward_to_lyzr(patch_content)
                payload = {
                    "pr_number": pr['number'],
                    "pr_title": pr['title'],
                    "lyzr_response": response_text
                }
                await listener_ws.send(json.dumps(payload))
            except Exception as e:
                error_payload = {"pr_number": pr['number'], "error": str(e)}
                if listener_ws:
                    try:
                        await listener_ws.send(json.dumps(error_payload))
                    except websockets.ConnectionClosed:
                        global listener_client
                        listener_client = None


# -------------------- WebSocket listener --------------------

async def listener_sender():
    global listener_client
    while True:
        message = await listener_queue.get()
        if listener_client:
            try:
                await listener_client.send(message)
            except websockets.ConnectionClosed:
                listener_client = None


async def ws_handler(websocket):
    global listener_client
    try:
        async for message in websocket:
            if message == "__register_streamlit__":
                listener_client = websocket
                try:
                    pr_data = get_all_pull_requests(owner="DhruvVayugundla", repo="testing")
                    asyncio.create_task(process_prs_and_forward(listener_client, pr_data))
                except Exception as e:
                    await listener_client.send(json.dumps({"error": str(e)}))
                continue
            if websocket == listener_client:
                print(f"Reply from listener: {message}")
            else:
                await listener_queue.put(message)
    except websockets.ConnectionClosed:
        if websocket == listener_client:
            listener_client = None


async def start_ws_server():
    server = await websockets.serve(ws_handler, "localhost", 8765)
    await server.wait_closed()


# -------------------- FastAPI webhook --------------------

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    pull_requests = body.get("pull_request")
    if not pull_requests or "patch_url" not in pull_requests:
        return {"status": "error", "message": "Missing 'pull_requests.patch_url' field"}
    patch_url = pull_requests["patch_url"]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(patch_url)
            response.raise_for_status()
            patch_content = response.text
        except Exception as e:
            return {"status": "error", "message": str(e)}
    if listener_queue:
        pr_info = {
            "user_login": pull_requests["user"]["login"],
            "user_id": pull_requests["user"]["id"],
            "title": pull_requests["title"],
            "number": body["number"],
            "action": body["action"]
        }
        lyzr_response = await forward_to_lyzr(patch_content)
        combined_response = {"pull_request": pr_info, "lyzr_response": lyzr_response}
        # thread-safe put into queue
        listener_queue.put_nowait(str(combined_response))
        return {"status": "sent", "message": combined_response}
    return {"status": "error", "message": "WebSocket listener not ready"}

@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request):
    return templates.TemplateResponse("listener.html", {"request": request})

# -------------------- Main startup --------------------

async def main():
    global listener_queue
    listener_queue = asyncio.Queue()  # create queue in this loop

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, loop="asyncio", lifespan="on")
    server = uvicorn.Server(config)

    # Run FastAPI + WebSocket + listener_sender concurrently
    await asyncio.gather(
        start_ws_server(),
        listener_sender(),
        server.serve()
    )


if __name__ == "__main__":
    asyncio.run(main())
