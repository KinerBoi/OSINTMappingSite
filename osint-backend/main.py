"""
OSINT Conflict Tracker API v5.2
Telegram + OpenSky + AISstream + NOTAM + Claude AI
"""

import os, asyncio, logging, httpx
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from telegram_monitor import TelegramMonitor, THEATERS
from adsb_tracker import ADSBTracker
from ais_tracker import AISTracker
from notam_detector import NOTAMDetector

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("server")

TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET", "")
AISSTREAM_API_KEY = os.getenv("AISSTREAM_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PORT = int(os.getenv("PORT", "8000"))
REFRESH = int(os.getenv("REFRESH_INTERVAL", "300"))
AIRCRAFT_REFRESH = 60  # 60 seconds to conserve credits on cloud

cache = {
    "events": None, "conflicts": None,
    "aircraft": None,
    "ai_summaries": {},
    "updated_events": None, "updated_aircraft": None,
    "status": "starting",
}

telegram = None
adsb = None
ais = None
notam_detector = NOTAMDetector()


async def generate_ai_summary(theater_name, events):
    if not ANTHROPIC_API_KEY or not events: return None
    event_texts = [f"- [{e.get('time','')}] {e.get('text','')[:100]}" for e in events[:15]]
    prompt = f'You are a military intelligence analyst. Write a 2-3 sentence operational briefing for the "{theater_name}" theater based on these Telegram OSINT reports from the last 48 hours. Be concise and factual.\n\nReports:\n' + "\n".join(event_texts) + "\n\nBriefing:"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-sonnet-4-20250514", "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 200: return r.json()["content"][0]["text"]
    except Exception as e:
        logger.warning(f"AI summary error: {e}")
    return None


def build_theater_conflicts(features):
    theaters = {}
    for f in features:
        p = f["properties"]
        theater = p.get("theater", "Global / Other")
        if theater not in theaters:
            info = THEATERS.get(theater, THEATERS["Global / Other"])
            theaters[theater] = {
                "name": theater, "lon": info["center"][0], "lat": info["center"][1],
                "radius": info["radius"], "base_desc": info["desc"],
                "events": [], "event_count": 0, "total_score": 0,
                "critical_count": 0, "high_count": 0, "locations": set(),
                "recent_count": 0, "confirmed_count": 0,
            }
        t = theaters[theater]
        t["event_count"] += 1
        t["total_score"] += p.get("severity_score", 1)
        t["locations"].add(p.get("location", ""))
        lvl = p.get("level", "moderate")
        if lvl == "critical": t["critical_count"] += 1
        elif lvl == "high": t["high_count"] += 1
        if p.get("confidence") in ("confirmed", "corroborated"): t["confirmed_count"] += 1
        time_str = p.get("time", "")
        if "m ago" in time_str or ("h ago" in time_str and int(time_str.split("h")[0]) < 6):
            t["recent_count"] += 1
        t["events"].append({
            "text": p.get("text", "")[:100], "lon": f["geometry"]["coordinates"][0],
            "lat": f["geometry"]["coordinates"][1], "time": p.get("time", ""),
            "level": lvl, "summary": p.get("notes", ""), "url": p.get("url", ""),
            "source": p.get("source", ""), "verified_by": p.get("verified_by", 1),
            "confidence": p.get("confidence", "single source"),
        })
    conflicts = []
    for name, t in theaters.items():
        if t["event_count"] == 0: continue
        if t["critical_count"] >= 2 or t["total_score"] >= 10: theater_level = "critical"
        elif t["critical_count"] >= 1 or t["total_score"] >= 5 or t["high_count"] >= 2: theater_level = "high"
        else: theater_level = "moderate"
        locs = sorted([l for l in t["locations"] if l], key=len, reverse=True)[:5]
        desc = f"{t['event_count']} reports across {', '.join(locs) if locs else 'region'}. {t['critical_count']} critical, {t['high_count']} high. {t['base_desc']}"
        conflicts.append({
            "name": name, "lon": t["lon"], "lat": t["lat"],
            "level": theater_level, "radius": t["radius"] + min(t["event_count"] * 30, 300),
            "desc": desc, "events": t["events"][:20], "event_count": t["event_count"],
            "critical_count": t["critical_count"], "high_count": t["high_count"],
            "recent_count": t["recent_count"], "confirmed_count": t["confirmed_count"],
            "severity_score": t["total_score"], "fatalities": 0,
            "ai_summary": cache["ai_summaries"].get(name),
        })
    conflicts.sort(key=lambda c: ({"critical":0,"high":1,"moderate":2}.get(c["level"],3), -c["severity_score"]))
    return {"conflicts": conflicts, "metadata": {"count": len(conflicts),
            "total_events": sum(c["event_count"] for c in conflicts), "fetched_at": datetime.utcnow().isoformat() + "Z"}}


async def refresh_telegram():
    try:
        raw_messages = await telegram.fetch_all_channels(hours=48)
        ev = await telegram.fetch_events_geojson(hours=48)
        cache["events"] = ev
        cache["conflicts"] = build_theater_conflicts(ev.get("features", []))
        cache["updated_events"] = datetime.utcnow().isoformat() + "Z"
        from telegram_monitor import extract_location
        notam_detector.process_messages(raw_messages, extract_location)
        nd = notam_detector.get_geojson()
        logger.info(f"Telegram: {ev['metadata']['count']} events, {nd['metadata']['count']} NOTAMs")
        if ANTHROPIC_API_KEY:
            for conflict in cache["conflicts"]["conflicts"]:
                summary = await generate_ai_summary(conflict["name"], conflict["events"])
                if summary:
                    cache["ai_summaries"][conflict["name"]] = summary
                    conflict["ai_summary"] = summary
    except Exception as e:
        logger.error(f"Telegram refresh failed: {e}")


async def refresh_aircraft():
    try:
        ac = await adsb.fetch_geojson(military_only=False)
        cache["aircraft"] = ac
        cache["updated_aircraft"] = datetime.utcnow().isoformat() + "Z"
        logger.info(f"Aircraft: {ac['metadata']['count']} ({ac['metadata']['military']} mil) [{ac['metadata'].get('auth','anon')}]")
    except Exception as e:
        logger.error(f"Aircraft refresh failed: {e}")


async def telegram_loop():
    await refresh_telegram()
    while True:
        await asyncio.sleep(REFRESH)
        await refresh_telegram()

async def aircraft_loop():
    await asyncio.sleep(10)
    while True:
        await refresh_aircraft()
        await asyncio.sleep(AIRCRAFT_REFRESH)


@asynccontextmanager
async def lifespan(app):
    global telegram, adsb, ais
    tasks = []
    if TELEGRAM_API_ID and TELEGRAM_API_HASH:
        telegram = TelegramMonitor(TELEGRAM_API_ID, TELEGRAM_API_HASH)
        if await telegram.start():
            tasks.append(asyncio.create_task(telegram_loop()))
            logger.info("✓ Telegram connected (events + NOTAMs)")
    else:
        logger.warning("✗ No Telegram credentials")
    adsb = ADSBTracker(OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET)
    tasks.append(asyncio.create_task(aircraft_loop()))
    logger.info("✓ Aircraft tracker started")
    if AISSTREAM_API_KEY:
        ais = AISTracker(AISSTREAM_API_KEY)
        if await ais.start(): logger.info("✓ Ship tracker started")
    else:
        logger.warning("✗ No AISstream API key")
    if ANTHROPIC_API_KEY: logger.info("✓ Claude AI summaries enabled")
    cache["status"] = "live"
    yield
    for t in tasks: t.cancel()
    if telegram: await telegram.close()
    if adsb: await adsb.close()
    if ais: await ais.close()


app = FastAPI(title="OSINT Conflict Tracker", version="5.2", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def root():
    return {"service": "OSINT Tracker v5.2", "status": cache["status"]}

@app.get("/events")
async def events(theater: str = Query(None), level: str = Query(None)):
    if not cache["events"]: return {"type": "FeatureCollection", "features": []}
    features = cache["events"]["features"]
    if theater: features = [f for f in features if theater.lower() in f["properties"].get("theater", "").lower()]
    if level: features = [f for f in features if f["properties"].get("level") == level]
    return {"type": "FeatureCollection", "features": features, "metadata": cache["events"].get("metadata", {})}

@app.get("/conflicts")
async def conflicts():
    return cache["conflicts"] or {"conflicts": []}

@app.get("/aircraft")
async def aircraft(military: bool = Query(False)):
    if not cache["aircraft"]: return {"type": "FeatureCollection", "features": []}
    features = cache["aircraft"]["features"]
    if military: features = [f for f in features if f["properties"].get("military")]
    return {"type": "FeatureCollection", "features": features, "metadata": cache["aircraft"].get("metadata", {})}

@app.get("/ships")
async def ships(military: bool = Query(False)):
    if not ais: return {"type": "FeatureCollection", "features": [], "metadata": {"count": 0}}
    geojson = ais.get_geojson()
    features = geojson["features"]
    if military: features = [f for f in features if f["properties"].get("military")]
    return {"type": "FeatureCollection", "features": features, "metadata": geojson.get("metadata", {})}

@app.get("/notam")
async def notam():
    return notam_detector.get_geojson()

@app.get("/status")
async def status():
    ac = cache["aircraft"]["metadata"] if cache["aircraft"] else {}
    ship_data = ais.get_geojson()["metadata"] if ais else {}
    ev = cache["events"]["metadata"] if cache["events"] else {}
    nd = notam_detector.get_geojson()["metadata"]
    return {
        "status": cache["status"],
        "telegram": {"events": ev.get("count", 0)},
        "aircraft": {"total": ac.get("count", 0), "military": ac.get("military", 0), "auth": ac.get("auth", "none")},
        "ships": {"total": ship_data.get("count", 0), "military": ship_data.get("military", 0)},
        "notam": {"total": nd["count"], "airspace": nd["restricted_firs"], "gps": nd["gps_zones"]},
        "ai_summaries": len(cache["ai_summaries"]),
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)