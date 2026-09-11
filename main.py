import asyncio
import datetime
import json
import logging
import math
import os
import sqlite3
import urllib.parse
from aiohttp import web
import httpx
import trafilatura
import websockets

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BASE_ENDPOINT = "wss://api.xiaozhi.me/mcp/"
XIAOZHI_TOKEN = os.environ.get("eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOjEwNjc3MDAsImFnZW50SWQiOjIzMjg2OTQsImVuZHBvaW50SWQiOiJhZ2VudF8yMzI4Njk0IiwicHVycG9zZSI6Im1jcC1lbmRwb2ludCIsImlhdCI6MTc4OTAxNzMwOCwiZXhwIjoxODIwNTc0OTA4fQ.mPvYHCmo0nsbaftvmZ_0WbF6CO9AraC5lGo5ZBIBjJQMwIc2GN_QpmSfuyS1kUl2TBxD0ZRUQB9Z2OmJtX104Q", "")
PORT = int(os.environ.get("PORT", 8000))

def get_connection_url() -> str:
    token_str = XIAOZHI_TOKEN.strip()
    if token_str.startswith("wss://") or token_str.startswith("ws://"):
        return token_str
    separator = "&" if "?" in BASE_ENDPOINT else "?"
    return f"{BASE_ENDPOINT}{separator}token={token_str}"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

DB_PATH = "xiaozhi_cloud_memory.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            fact TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

def remember_fact(fact: str, category: str = "general") -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO memory (category, fact) VALUES (?, ?)", (category, fact))
        conn.commit()
        conn.close()
        return f"Saved to cloud memory: '{fact}' [{category}]."
    except Exception as e:
        return f"Database error: {e}"

def recall_facts(keyword: str = "") -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if keyword:
            q = f"%{keyword}%"
            c.execute("SELECT category, fact, created_at FROM memory WHERE fact LIKE ? OR category LIKE ? ORDER BY id DESC LIMIT 10", (q, q))
        else:
            c.execute("SELECT category, fact, created_at FROM memory ORDER BY id DESC LIMIT 10")
        rows = c.fetchall()
        conn.close()
        if not rows:
            return "No matching memories found."
        return "Cloud Memories:\n" + "\n".join([f"- [{r[0]}] {r[1]}" for r in rows])
    except Exception as e:
        return f"Recall error: {e}"

def search_web(query: str, max_results: int = 5) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No web results found."
        return "\n".join([f"{i}. {item.get('title')}\n   {item.get('href')}\n   {item.get('body')}\n" for i, item in enumerate(results, 1)])
    except Exception as e:
        return f"Search error: {e}"

def fetch_page_content(url: str, max_chars: int = 6000) -> str:
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True, headers=HEADERS) as client:
            resp = client.get(url)
            text = trafilatura.extract(resp.text, include_links=False, output_format="txt")
            return text[:max_chars] if text else "Failed to extract text."
    except Exception as e:
        return f"Fetch error: {e}"

def get_live_weather(latitude: float, longitude: float) -> str:
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,wind_speed_10m&timezone=auto"
        with httpx.Client(timeout=10.0) as client:
            data = client.get(url).json().get("current", {})
        return f"Weather: {data.get('temperature_2m')}°C, Humidity: {data.get('relative_humidity_2m')}%, Wind: {data.get('wind_speed_10m')} km/h"
    except Exception as e:
        return f"Weather error: {e}"

def get_rainfall_forecast(latitude: float, longitude: float) -> str:
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&hourly=precipitation_probability,rain&daily=precipitation_sum,precipitation_probability_max&timezone=auto"
        with httpx.Client(timeout=10.0) as client:
            data = client.get(url).json()
        daily = data.get("daily", {})
        today_sum = daily.get("precipitation_sum", [0])[0]
        today_prob = daily.get("precipitation_probability_max", [0])[0]
        return f"Rainfall Forecast: {today_sum} mm expected today, peak chance: {today_prob}%."
    except Exception as e:
        return f"Rainfall error: {e}"

def execute_python_calc(code: str) -> str:
    try:
        allowed = {"math": math, "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow}
        return str(eval(code, {"__builtins__": {}}, allowed))
    except Exception as e:
        return f"Calculation error: {e}"

TOOLS = [
    {"name": "web_search", "description": "Searches the live web.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "fetch_page_content", "description": "Extracts clean text from a webpage URL.", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "get_live_weather", "description": "Gets current weather conditions.", "inputSchema": {"type": "object", "properties": {"latitude": {"type": "number"}, "longitude": {"type": "number"}}, "required": ["latitude", "longitude"]}},
    {"name": "get_rainfall_forecast", "description": "Fetches precipitation sum and rain probabilities.", "inputSchema": {"type": "object", "properties": {"latitude": {"type": "number"}, "longitude": {"type": "number"}}, "required": ["latitude", "longitude"]}},
    {"name": "remember_fact", "description": "Stores user facts in persistent cloud memory.", "inputSchema": {"type": "object", "properties": {"fact": {"type": "string"}, "category": {"type": "string"}}, "required": ["fact"]}},
    {"name": "recall_facts", "description": "Retrieves facts from persistent cloud memory.", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}},
    {"name": "execute_python_calc", "description": "Evaluates Python math expressions.", "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}
]

async def handle_mcp_message(ws, raw_msg: str):
    try:
        data = json.loads(raw_msg)
    except json.JSONDecodeError:
        return

    msg_id, method, params = data.get("id"), data.get("method"), data.get("params", {})

    if method == "initialize":
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "xiaozhi-koyeb-cloud", "version": "1.0.0"}}}))
    elif method == "tools/list":
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}))
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        result_text, is_err = "", False

        try:
            if name == "web_search":
                result_text = await asyncio.to_thread(search_web, args.get("query", ""))
            elif name == "fetch_page_content":
                result_text = await asyncio.to_thread(fetch_page_content, args.get("url", ""))
            elif name == "get_live_weather":
                result_text = await asyncio.to_thread(get_live_weather, float(args["latitude"]), float(args["longitude"]))
            elif name == "get_rainfall_forecast":
                result_text = await asyncio.to_thread(get_rainfall_forecast, float(args["latitude"]), float(args["longitude"]))
            elif name == "remember_fact":
                result_text = remember_fact(args.get("fact"), args.get("category", "general"))
            elif name == "recall_facts":
                result_text = recall_facts(args.get("keyword", ""))
            elif name == "execute_python_calc":
                result_text = execute_python_calc(args.get("code", ""))
            else:
                result_text, is_err = f"Tool '{name}' not found on cloud.", True
        except Exception as e:
            result_text, is_err = str(e), True

        await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": result_text}], "isError": is_err}}))
    elif method == "ping":
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}))

async def run_mcp_bridge():
    ws_url = get_connection_url()

    while True:
        try:
            logging.info("Connecting to XiaoZhi Gateway from Koyeb...")
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10
            ) as ws:
                logging.info("XiaoZhi Cloud Bridge connected on Koyeb.")
                async for msg in ws:
                    await handle_mcp_message(ws, msg)
        except Exception as e:
            logging.warning(f"Bridge disconnected ({e}). Reconnecting in 5s...")
            await asyncio.sleep(5)

async def health_check(request):
    return web.Response(text="XiaoZhi Cloud MCP Bridge is Running 24/7!")

async def start_background_tasks(app):
    app['mcp_task'] = asyncio.create_task(run_mcp_bridge())

async def cleanup_background_tasks(app):
    app['mcp_task'].cancel()
    await app['mcp_task']

def main():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()