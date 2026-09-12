import asyncio
import datetime
import json
import logging
import math
import os
import re
import sqlite3
import urllib.parse
from html import unescape
from aiohttp import web
import httpx
import psutil
import speedtest
import trafilatura
import websockets

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ==============================================================================
# CONFIGURATION & AUTHENTICATION
# ==============================================================================
HARDCODED_XIAOZHI_TOKEN = "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOjEwNjc3MDAsImFnZW50SWQiOjIzMjg2OTQsImVuZHBvaW50SWQiOiJhZ2VudF8yMzI4Njk0IiwicHVycG9zZSI6Im1jcC1lbmRwb2ludCIsImlhdCI6MTc4OTAxNzMwOCwiZXhwIjoxODIwNTc0OTA4fQ.mPvYHCmo0nsbaftvmZ_0WbF6CO9AraC5lGo5ZBIBjJQMwIc2GN_QpmSfuyS1kUl2TBxD0ZRUQB9Z2OmJtX104Q"
HARDCODED_BRAVE_API_KEY = "BSAKb72H0GlIbFx7PtG0W1DAtNkTBib"
HARDCODED_WEATHER_API_KEY = "f1c6328de66147e88aa125017261209"

RAW_TOKEN_INPUT = os.environ.get("XIAOZHI_TOKEN", HARDCODED_XIAOZHI_TOKEN).strip().strip('"').strip("'")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", HARDCODED_BRAVE_API_KEY).strip()
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", HARDCODED_WEATHER_API_KEY).strip()
PORT = int(os.environ.get("PORT", 8000))

def get_sanitized_connection():
    if not RAW_TOKEN_INPUT:
        logging.error("CRITICAL: No XIAOZHI_TOKEN provided in code or environment!")
        return "wss://api.xiaozhi.me/mcp/", []

    raw = RAW_TOKEN_INPUT
    if "token=" in raw:
        token_match = re.search(r'token=([^&\s]+)', raw)
        token = token_match.group(1) if token_match else raw.split("token=")[-1]
    elif raw.startswith("wss://") or raw.startswith("ws://"):
        parsed = urllib.parse.urlparse(raw)
        query_dict = urllib.parse.parse_qs(parsed.query)
        token = query_dict.get("token", [raw])[0]
    else:
        token = raw

    clean_token = urllib.parse.unquote(token).strip()
    ws_url = f"wss://api.xiaozhi.me/mcp/?token={clean_token}"

    headers = [
        ("Host", "api.xiaozhi.me"),
        ("Origin", "https://xiaozhi.me"),
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    ]
    return ws_url, headers

# ==============================================================================
# CLOUD SQLITE PERSISTENCE
# ==============================================================================
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
            return "No matching memories found in cloud database."
        return "Retrieved Cloud Memories:\n" + "\n".join([f"- [{r[0]}] {r[1]}" for r in rows])
    except Exception as e:
        return f"Recall error: {e}"

# ==============================================================================
# API-KEY POWERED WEATHER ENGINE (WeatherAPI.com)
# ==============================================================================
def get_live_weather(location: str = "Manila") -> str:
    """Fetches high-accuracy real-time weather and forecast using WeatherAPI."""
    loc = location.strip() or "Manila"

    if not WEATHER_API_KEY:
        # Graceful fallback if WEATHER_API_KEY environment variable is omitted
        try:
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(loc)}&count=1"
            with httpx.Client(timeout=6.0) as client:
                res = client.get(geo_url).json()
                if "results" in res and res["results"]:
                    lat, lon = res["results"][0]["latitude"], res["results"][0]["longitude"]
                    fc_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m&timezone=auto"
                    curr = client.get(fc_url).json().get("current", {})
                    return f"Weather for {loc}: {curr.get('temperature_2m')}°C (Feels like {curr.get('apparent_temperature')}°C), Humidity: {curr.get('relative_humidity_2m')}%, Wind: {curr.get('wind_speed_10m')} km/h"
        except Exception:
            pass
        return "Error: WEATHER_API_KEY environment variable is not configured on Render."

    url = f"https://api.weatherapi.com/v1/forecast.json?key={WEATHER_API_KEY}&q={urllib.parse.quote(loc)}&days=1&aqi=no"

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return f"Weather provider error: {resp.text}"
            data = resp.json()

        curr = data.get("current", {})
        forecast_day = data.get("forecast", {}).get("forecastday", [{}])[0].get("day", {})
        loc_info = data.get("location", {})

        return (
            f"Weather for {loc_info.get('name')}, {loc_info.get('region')} ({loc_info.get('country')}):\n"
            f"- Condition: {curr.get('condition', {}).get('text')}\n"
            f"- Temperature: {curr.get('temp_c')}°C (Feels like: {curr.get('feelslike_c')}°C)\n"
            f"- Today's Range: High {forecast_day.get('maxtemp_c')}°C / Low {forecast_day.get('mintemp_c')}°C\n"
            f"- Humidity: {curr.get('humidity')}%\n"
            f"- Wind Speed: {curr.get('wind_kph')} km/h (Gusts: {curr.get('gust_kph')} km/h)\n"
            f"- UV Index: {curr.get('uv')}\n"
            f"- Rain Probability: {forecast_day.get('daily_chance_of_rain')}% ({forecast_day.get('totalprecip_mm')} mm expected)"
        )
    except Exception as e:
        return f"Failed to retrieve weather: {str(e)}"

def get_rainfall_forecast(location: str = "Manila") -> str:
    """Provides rainfall probabilities, accumulation sums, and rain advisories."""
    loc = location.strip() or "Manila"

    if not WEATHER_API_KEY:
        return get_live_weather(loc)

    url = f"https://api.weatherapi.com/v1/forecast.json?key={WEATHER_API_KEY}&q={urllib.parse.quote(loc)}&days=1&aqi=no"

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return f"Rainfall query error: {resp.text}"
            data = resp.json()

        forecast_day = data.get("forecast", {}).get("forecastday", [{}])[0].get("day", {})
        loc_info = data.get("location", {})
        chance = forecast_day.get("daily_chance_of_rain", 0)
        total_mm = forecast_day.get("totalprecip_mm", 0.0)

        advisory = "Rain expected - consider carrying an umbrella!" if int(chance) >= 45 or float(total_mm) > 2.0 else "Minimal rain risk."

        return (
            f"Rainfall Forecast for {loc_info.get('name')}, {loc_info.get('country')}:\n"
            f"- Probability of Rain: {chance}%\n"
            f"- Expected Precipitation: {total_mm} mm\n"
            f"- Condition: {forecast_day.get('condition', {}).get('text')}\n"
            f"- Advisory: {advisory}"
        )
    except Exception as e:
        return f"Failed to retrieve rainfall forecast: {str(e)}"

# ==============================================================================
# CLOUD SEARCH (BRAVE SEARCH PRIMARY + MULTI-TIER FALLBACKS)
# ==============================================================================
def search_web(query: str, max_results: int = 5) -> str:
    clean_query = query.strip()
    if not clean_query:
        return "Please provide a valid search query."

    if BRAVE_API_KEY:
        try:
            url = "https://api.search.brave.com/res/v1/web/search"
            headers = {
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": BRAVE_API_KEY
            }
            params = {"q": clean_query, "count": min(max_results, 10)}
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(url, headers=headers, params=params)
                if resp.status_code == 200:
                    results = resp.json().get("web", {}).get("results", [])
                    if results:
                        formatted = []
                        for i, r in enumerate(results[:max_results], 1):
                            formatted.append(f"{i}. {r.get('title', 'No Title')}\n   {r.get('url', '')}\n   {r.get('description', '')}\n")
                        return "\n".join(formatted)
        except Exception as e:
            logging.warning(f"Brave Search failed: {e}. Falling back...")

    try:
        with DDGS(timeout=7) as ddgs:
            results = list(ddgs.text(clean_query, max_results=max_results, backend="lite"))
            if results:
                return "\n".join([f"{i}. {item.get('title')}\n   {item.get('href')}\n   {item.get('body')}\n" for i, item in enumerate(results, 1)])
    except Exception as e:
        logging.warning(f"DDGS failed on cloud: {e}. Trying direct HTML...")

    try:
        url = "https://html.duckduckgo.com/html/"
        data = {"q": clean_query}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://html.duckduckgo.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        with httpx.Client(timeout=8.0, follow_redirects=True, headers=headers) as client:
            resp = client.post(url, data=data)
            if resp.status_code == 200:
                html = resp.text
                titles = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.DOTALL)
                snippets = [re.sub(r'<[^>]+>', '', unescape(s)).strip() for s in titles]
                urls = re.findall(r'href="//duckduckgo.com/l/\?uddg=([^"&]+)', html)
                clean_urls = [urllib.parse.unquote(u) for u in urls]
                heading_matches = re.findall(r'<a class="result__url[^>]*href="[^"]*"[^>]*>(.*?)</a>', html, re.DOTALL)
                headings = [re.sub(r'<[^>]+>', '', unescape(h)).strip() for h in heading_matches]

                items = []
                count = min(len(snippets), max_results)
                for i in range(count):
                    link = clean_urls[i] if i < len(clean_urls) else "N/A"
                    title = headings[i] if i < len(headings) else f"Result {i+1}"
                    body = snippets[i]
                    if body:
                        items.append(f"{i+1}. {title}\n   {link}\n   {body}\n")
                if items:
                    return "\n".join(items)
    except Exception as e:
        logging.warning(f"Direct HTML scrape failed: {e}")

    try:
        wiki_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(clean_query)}"
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            resp = client.get(wiki_url)
            if resp.status_code == 200:
                data = resp.json()
                title = data.get("title", "")
                extract = data.get("extract", "")
                page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
                if extract:
                    return f"1. {title} (Wikipedia)\n   {page_url}\n   {extract}\n"
    except Exception:
        pass

    return f"No results found for '{clean_query}'. (Search provider rate-limited)."

# ==============================================================================
# SYSTEM DIAGNOSTICS & HELPERS
# ==============================================================================
def test_network_speed() -> str:
    try:
        st = speedtest.Speedtest()
        st.get_best_server()
        down_mbps = round(st.download() / 1e6, 2)
        up_mbps = round(st.upload() / 1e6, 2)
        ping_ms = round(st.results.ping, 1)
        server_info = st.results.server.get("name", "Unknown")
        country = st.results.server.get("country", "")

        return (
            f"Network Speed Test Results:\n"
            f"- Download Speed: {down_mbps} Mbps\n"
            f"- Upload Speed: {up_mbps} Mbps\n"
            f"- Latency (Ping): {ping_ms} ms\n"
            f"- Server: {server_info}, {country}"
        )
    except Exception as e:
        return f"Network speed test failed: {str(e)}"

def scan_wifi_signal() -> str:
    try:
        if os.name == "nt":
            import subprocess
            res = subprocess.run("netsh wlan show interfaces", capture_output=True, text=True, shell=True)
            ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", res.stdout, re.MULTILINE)
            sig = re.search(r"^\s*Signal\s*:\s*(\d+)%", res.stdout, re.MULTILINE)
            if ssid and sig:
                return f"Wi-Fi Status: Connected to '{ssid.group(1).strip()}' with {sig.group(1).strip()}% signal strength."
            return "Wi-Fi is not connected or no wireless interface found."

        net_stats = psutil.net_if_stats()
        active_interfaces = [iface for iface, stats in net_stats.items() if stats.isup and iface != "lo"]
        return (
            f"Cloud Network Status (Render Datacenter):\n"
            f"- Connection Type: High-Speed Fiber Uplink (Virtual Ethernet)\n"
            f"- Active Interfaces: {', '.join(active_interfaces) if active_interfaces else 'eth0'}\n"
            f"- Note: Cloud servers use datacenter fiber uplinks, not wireless 802.11 Wi-Fi."
        )
    except Exception as e:
        return f"Failed to inspect network interface: {str(e)}"

def fetch_page_content(url: str, max_chars: int = 6000) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
            text = trafilatura.extract(resp.text, include_links=False, output_format="txt")
            return text[:max_chars] if text else "Failed to extract clean webpage content."
    except Exception as e:
        return f"Fetch error: {e}"

def execute_python_calc(code: str) -> str:
    try:
        allowed = {"math": math, "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow}
        return str(eval(code, {"__builtins__": {}}, allowed))
    except Exception as e:
        return f"Calculation error: {e}"

# ==============================================================================
# TOOL REGISTRY (CLOUD-NATIVE)
# ==============================================================================
TOOLS = [
    {
        "name": "get_live_weather",
        "description": "Fetches accurate weather data (temperature, feels-like, condition, humidity, UV index, wind speed) for any city or location name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City, province, town, or location name (e.g., 'Manila', 'Baguio', 'Cauayan')."
                }
            },
            "required": ["location"]
        }
    },
    {
        "name": "get_rainfall_forecast",
        "description": "Checks precipitation probabilities, rain mm accumulation, and shower advisories for any given city or area.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City or location name to check for rain."
                }
            },
            "required": ["location"]
        }
    },
    {
        "name": "web_search",
        "description": "Searches the live web via Brave Search with automated multi-tier fallbacks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "test_network_speed",
        "description": "Measures download bandwidth, upload throughput, and ping latency in real-time.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "scan_wifi_signal",
        "description": "Inspects active network connection, Wi-Fi SSID signal strength, and adapter status.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "fetch_page_content",
        "description": "Extracts clean readable text from a given webpage URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "remember_fact",
        "description": "Stores permanent facts and notes in the cloud SQLite database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string"},
                "category": {"type": "string"}
            },
            "required": ["fact"]
        }
    },
    {
        "name": "recall_facts",
        "description": "Retrieves stored facts and user notes from the cloud database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"}
            }
        }
    },
    {
        "name": "execute_python_calc",
        "description": "Performs exact Python math and arithmetic evaluation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"}
            },
            "required": ["code"]
        }
    }
]

# ==============================================================================
# MCP PROTOCOL ROUTER
# ==============================================================================
async def handle_mcp_message(ws, raw_msg: str):
    try:
        data = json.loads(raw_msg)
    except json.JSONDecodeError:
        return

    msg_id = data.get("id")
    method = data.get("method")
    params = data.get("params", {})

    if method == "initialize":
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "xiaozhi-render-cloud", "version": "1.6.0"}
            }
        }))
    elif method == "tools/list":
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS}
        }))
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        result_text, is_err = "", False

        try:
            if name in ("get_live_weather", "get_weather", "weather"):
                loc = args.get("location") or args.get("query") or "Manila"
                result_text = await asyncio.to_thread(get_live_weather, loc)

            elif name in ("get_rainfall_forecast", "rainfall_forecast", "check_rain"):
                loc = args.get("location") or args.get("query") or "Manila"
                result_text = await asyncio.to_thread(get_rainfall_forecast, loc)

            elif name == "web_search":
                result_text = await asyncio.to_thread(search_web, args.get("query", ""))

            elif name in ("test_network_speed", "speed_test", "wifi_speed"):
                result_text = await asyncio.to_thread(test_network_speed)

            elif name in ("scan_wifi_signal", "check_wifi", "wifi_status"):
                result_text = await asyncio.to_thread(scan_wifi_signal)

            elif name == "fetch_page_content":
                result_text = await asyncio.to_thread(fetch_page_content, args.get("url", ""))

            elif name == "remember_fact":
                result_text = remember_fact(args.get("fact"), args.get("category", "general"))

            elif name == "recall_facts":
                result_text = recall_facts(args.get("keyword", ""))

            elif name == "execute_python_calc":
                result_text = execute_python_calc(args.get("code", ""))

            else:
                result_text, is_err = f"Tool '{name}' not found on cloud server.", True

        except Exception as e:
            result_text, is_err = str(e), True

        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": result_text}],
                "isError": is_err
            }
        }))
    elif method == "ping":
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}))

async def run_mcp_bridge():
    ws_url, headers = get_sanitized_connection()
    masked_url = re.sub(r"token=([^&]{4})[^&]+", r"token=\1****", ws_url)
    logging.info(f"Target WebSocket: {masked_url}")

    while True:
        try:
            logging.info("Initiating handshake with XiaoZhi...")
            connect_kwargs = {
                "ping_interval": 20,
                "ping_timeout": 20,
                "close_timeout": 10
            }

            try:
                ws_connect = websockets.connect(ws_url, additional_headers=headers, **connect_kwargs)
            except TypeError:
                ws_connect = websockets.connect(ws_url, extra_headers=headers, **connect_kwargs)

            async with ws_connect as ws:
                logging.info("XiaoZhi Cloud Bridge connected successfully on Render!")
                async for msg in ws:
                    await handle_mcp_message(ws, msg)

        except (websockets.ConnectionClosed, ConnectionRefusedError) as e:
            logging.warning(f"Connection dropped ({e}). Reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Handshake error: {e}. Retrying in 5s...")
            await asyncio.sleep(5)

# ==============================================================================
# AIOHTTP KEEP-ALIVE SERVER (PORT 8000)
# ==============================================================================
async def health_check(request):
    return web.Response(text="XiaoZhi Cloud MCP Bridge is Running 24/7 on Render!")

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
