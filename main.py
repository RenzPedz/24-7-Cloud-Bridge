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

RAW_TOKEN_INPUT = os.environ.get("XIAOZHI_TOKEN", HARDCODED_XIAOZHI_TOKEN).strip().strip('"').strip("'")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", HARDCODED_BRAVE_API_KEY).strip()
PORT = int(os.environ.get("PORT", 8000))

def get_sanitized_connection():
    if not RAW_TOKEN_INPUT:
        logging.error("CRITICAL: No XIAOZHI_TOKEN provided!")
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
# HIGH-PRECISION WEATHER ENGINE (ECMWF / DWD HIGH-RES WITH GEOCODING)
# ==============================================================================
WMO_WEATHER_CODES = {
    0: "Clear sky ☀️",
    1: "Mainly clear 🌤️",
    2: "Partly cloudy ⛅",
    3: "Overcast ☁️",
    45: "Fog 🌫️",
    48: "Depositing rime fog 🌫️",
    51: "Light drizzle 🌦️",
    53: "Moderate drizzle 🌦️",
    55: "Dense drizzle 🌧️",
    61: "Slight rain 🌧️",
    63: "Moderate rain 🌧️",
    65: "Heavy rain 🌧️⚡",
    71: "Slight snowfall 🌨️",
    73: "Moderate snowfall 🌨️",
    75: "Heavy snowfall ❄️",
    80: "Slight rain showers 🌦️",
    81: "Moderate rain showers 🌧️",
    82: "Violent rain showers ⛈️",
    95: "Thunderstorm ⛈️",
    96: "Thunderstorm with slight hail ⛈️🌨️",
    99: "Thunderstorm with heavy hail ⛈️❄️"
}

def resolve_location_coordinates(location_query: str):
    try:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(location_query)}&count=1&language=en&format=json"
        with httpx.Client(timeout=6.0) as client:
            resp = client.get(url)
            data = resp.json()
            if "results" in data and len(data["results"]) > 0:
                target = data["results"][0]
                resolved_name = f"{target.get('name')}, {target.get('admin1', '')} ({target.get('country_code', '')})".replace(",  ", ", ")
                return float(target["latitude"]), float(target["longitude"]), resolved_name
    except Exception as e:
        logging.warning(f"Geocoding error: {e}")
    return None, None, location_query

def get_detailed_weather(location: str = "", latitude: float = None, longitude: float = None) -> str:
    loc_name = location.strip()
    lat, lon = latitude, longitude

    if loc_name and (lat is None or lon is None):
        lat, lon, loc_name = resolve_location_coordinates(loc_name)

    if lat is None or lon is None:
        lat, lon, loc_name = 14.5995, 120.9842, "Default Location (Manila)"

    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": [
                "temperature_2m", "relative_humidity_2m", "apparent_temperature",
                "is_day", "precipitation", "weather_code", "pressure_msl",
                "surface_pressure", "wind_speed_10m", "wind_gusts_10m", "uv_index"
            ],
            "daily": [
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "precipitation_probability_max", "uv_index_max"
            ],
            "timezone": "auto",
            "models": "best_match"
        }

        url = "https://api.open-meteo.com/v1/forecast"
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current", {})
        daily = data.get("daily", {})

        weather_code = current.get("weather_code", 0)
        condition_desc = WMO_WEATHER_CODES.get(weather_code, "Partly Cloudy")

        temp = current.get("temperature_2m", "N/A")
        feels_like = current.get("apparent_temperature", "N/A")
        humidity = current.get("relative_humidity_2m", "N/A")
        wind_spd = current.get("wind_speed_10m", "N/A")
        wind_gust = current.get("wind_gusts_10m", "N/A")
        uv = current.get("uv_index", "N/A")
        pressure = current.get("surface_pressure", "N/A")

        temp_max = daily.get("temperature_2m_max", ["N/A"])[0]
        temp_min = daily.get("temperature_2m_min", ["N/A"])[0]
        rain_sum = daily.get("precipitation_sum", [0])[0]
        rain_prob = daily.get("precipitation_probability_max", [0])[0]

        return (
            f"Weather Report for {loc_name} (Accurate Multi-Model Forecast):\n"
            f"- Condition: {condition_desc}\n"
            f"- Temperature: {temp}°C (Feels like: {feels_like}°C)\n"
            f"- Today's Range: Low {temp_min}°C / High {temp_max}°C\n"
            f"- Humidity: {humidity}%\n"
            f"- Wind Speed: {wind_spd} km/h (Gusts up to {wind_gust} km/h)\n"
            f"- UV Index: {uv} (Peak today: {daily.get('uv_index_max', ['N/A'])[0]})\n"
            f"- Atmospheric Pressure: {pressure} hPa\n"
            f"- Rainfall Outlook: {rain_sum} mm accumulation ({rain_prob}% probability)"
        )
    except Exception as e:
        return f"High-precision weather query failed: {str(e)}"

def get_rainfall_forecast(location: str = "", latitude: float = None, longitude: float = None) -> str:
    loc_name = location.strip()
    lat, lon = latitude, longitude

    if loc_name and (lat is None or lon is None):
        lat, lon, loc_name = resolve_location_coordinates(loc_name)

    if lat is None or lon is None:
        lat, lon, loc_name = 14.5995, 120.9842, "Default Location"

    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&hourly=precipitation_probability,rain,showers"
            f"&daily=precipitation_sum,precipitation_probability_max"
            f"&timezone=auto&models=best_match"
        )
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        today_sum = daily.get("precipitation_sum", [0])[0]
        today_prob = daily.get("precipitation_probability_max", [0])[0]

        curr_h = datetime.datetime.now().hour
        upcoming_probs = hourly.get("precipitation_probability", [])[curr_h:curr_h+6]
        upcoming_rain = hourly.get("rain", [])[curr_h:curr_h+6]

        max_upcoming = max(upcoming_probs) if upcoming_probs else 0
        total_upcoming = round(sum(upcoming_rain), 2) if upcoming_rain else 0.0

        alert = "Rainfall expected - keep an umbrella handy!" if max_upcoming >= 45 or today_sum > 2.0 else "Minimal precipitation risk."

        return (
            f"Rainfall & Precipitation Radar for {loc_name}:\n"
            f"- Total Expected Rain Today: {today_sum} mm\n"
            f"- Daily Max Probability: {today_prob}%\n"
            f"- Next 6-Hour Outlook: Peak {max_upcoming}% chance (~{total_upcoming} mm accumulation)\n"
            f"- Advisory: {alert}"
        )
    except Exception as e:
        return f"Failed to retrieve rainfall forecast: {str(e)}"

# ==============================================================================
# CLOUD SEARCH & SYSTEM DIAGNOSTICS
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

def test_network_speed() -> str:
    try:
        logging.info("Running speedtest benchmark on cloud server...")
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
            f"- Note: Cloud servers run on direct datacenter fiber uplinks, not wireless 802.11 Wi-Fi."
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
# TOOL REGISTRY (CLOUD OPTIMIZED)
# ==============================================================================
TOOLS = [
    {
        "name": "get_live_weather",
        "description": "Fetches meteorological weather data (temperature, real feel, weather condition, UV index, humidity, wind gusts, and air pressure) using high-resolution models. Accepts city names or coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City, town, province, or country name (e.g., 'Manila', 'Baguio', 'Tokyo')."},
                "latitude": {"type": "number", "description": "Optional latitude coordinate."},
                "longitude": {"type": "number", "description": "Optional longitude coordinate."}
            }
        }
    },
    {
        "name": "get_rainfall_forecast",
        "description": "Calculates precise precipitation accumulation in mm, rain probabilities, and next 6-hour shower risks. Accepts city names or coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City or location name."},
                "latitude": {"type": "number", "description": "Optional latitude coordinate."},
                "longitude": {"type": "number", "description": "Optional longitude coordinate."}
            }
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
                "serverInfo": {"name": "xiaozhi-render-cloud", "version": "1.5.0"}
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
                loc = args.get("location", "")
                lat = float(args["latitude"]) if "latitude" in args and args["latitude"] is not None else None
                lon = float(args["longitude"]) if "longitude" in args and args["longitude"] is not None else None
                result_text = await asyncio.to_thread(get_detailed_weather, loc, lat, lon)

            elif name in ("get_rainfall_forecast", "rainfall_forecast", "check_rain"):
                loc = args.get("location", "")
                lat = float(args["latitude"]) if "latitude" in args and args["latitude"] is not None else None
                lon = float(args["longitude"]) if "longitude" in args and args["longitude"] is not None else None
                result_text = await asyncio.to_thread(get_rainfall_forecast, loc, lat, lon)

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
    return web.Response(text="XiaoZhi Cloud MCP Bridge is Running 24/7 with High-Precision Weather!")

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
