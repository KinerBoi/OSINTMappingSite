"""
AIS Ship Tracker via AISstream.io WebSocket
- Connects to WebSocket, receives real-time ship positions
- Filters for conflict zone bounding boxes
- Tags military/naval vessels by MMSI ranges and ship type
- Maintains rolling cache of recent positions
"""

import json, logging, asyncio
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("ais")

# Conflict zone bounding boxes for AISstream: [[lat_min, lon_min], [lat_max, lon_max]]
AIS_ZONES = [
    [[-10, 25], [45, 75]],    # Middle East + Indian Ocean + Persian Gulf
    [[40, 25], [50, 42]],     # Black Sea / Ukraine coast
    [[-5, 35], [22, 55]],     # Red Sea + Gulf of Aden + Horn of Africa
    [[0, 100], [30, 130]],    # South China Sea + Taiwan Strait
    [[50, 10], [66, 35]],     # Baltic Sea
    [[25, -90], [35, -75]],   # US East Coast (carrier groups)
]

# Naval vessel MMSI prefixes (MID = Maritime Identification Digits)
# Military vessels often use specific MMSI ranges
NAVAL_MMSI_PREFIXES = [
    "111",  # US Navy (some)
    "338",  # US Government
    "369",  # US Government
    "503",  # Australia Navy
]

# Ship type codes indicating military/government vessels
MILITARY_SHIP_TYPES = {
    35: "Military",
    55: "Law Enforcement",
    50: "SAR (Search & Rescue)",
    51: "SAR Aircraft",
    53: "Port Tender",
    58: "Medical Transport",
}

# Keywords in ship names suggesting military
MILITARY_NAME_KEYWORDS = [
    "NAVY", "NAVAL", "WARSHIP", "USS ", "HMS ", "INS ", "RFS ",
    "USCG", "COAST GUARD", "PATROL", "FRIGATE", "DESTROYER",
    "CARRIER", "CORVETTE", "SUBMARINE", "AMPHIBIOUS",
    "OILER", "REPLENISHMENT", "SUPPLY",
]

# Ship type descriptions
SHIP_TYPES = {
    0: "Not available", 20: "Wing in Ground", 30: "Fishing",
    31: "Towing", 32: "Towing (large)", 33: "Dredging",
    34: "Diving ops", 35: "Military ops", 36: "Sailing",
    37: "Pleasure craft", 40: "High speed craft",
    50: "SAR", 51: "SAR Aircraft",
    52: "Tug", 53: "Port Tender", 54: "Anti-pollution",
    55: "Law Enforcement", 58: "Medical Transport",
    60: "Passenger", 61: "Passenger (Hazmat)",
    70: "Cargo", 71: "Cargo (Hazmat A)", 72: "Cargo (Hazmat B)",
    73: "Cargo (Hazmat C)", 74: "Cargo (Hazmat D)",
    80: "Tanker", 81: "Tanker (Hazmat A)", 82: "Tanker (Hazmat B)",
    83: "Tanker (Hazmat C)", 84: "Tanker (Hazmat D)",
    89: "Tanker (No info)", 90: "Other",
}


def get_ship_type_name(type_code):
    """Get human-readable ship type from numeric code."""
    if type_code in SHIP_TYPES:
        return SHIP_TYPES[type_code]
    # Type code ranges
    if 20 <= type_code <= 29: return "Wing in Ground"
    if 40 <= type_code <= 49: return "High Speed Craft"
    if 60 <= type_code <= 69: return "Passenger"
    if 70 <= type_code <= 79: return "Cargo"
    if 80 <= type_code <= 89: return "Tanker"
    return "Other"


def is_naval_vessel(mmsi, ship_name, ship_type):
    """Check if vessel is military/naval."""
    mmsi_str = str(mmsi)
    for prefix in NAVAL_MMSI_PREFIXES:
        if mmsi_str.startswith(prefix):
            return True

    if ship_type in MILITARY_SHIP_TYPES:
        return True

    name_upper = (ship_name or "").upper()
    for kw in MILITARY_NAME_KEYWORDS:
        if kw in name_upper:
            return True

    return False


class AISTracker:
    """
    Tracks ships via AISstream.io WebSocket API.
    Runs a background WebSocket connection that accumulates positions.
    The REST API serves from the cache.
    """

    WS_URL = "wss://stream.aisstream.io/v0/stream"

    def __init__(self, api_key):
        self.api_key = api_key
        self.ships = {}  # MMSI -> latest position data
        self.running = False
        self._task = None
        self.connected = False
        self.last_message = None

    async def start(self):
        """Start the background WebSocket listener."""
        if not self.api_key:
            logger.warning("No AISstream API key — ship tracking disabled")
            return False

        self.running = True
        self._task = asyncio.create_task(self._ws_loop())
        logger.info("AIS tracker started")
        return True

    async def _ws_loop(self):
        """Main WebSocket loop — reconnects on failure."""
        while self.running:
            try:
                import websockets
                async with websockets.connect(self.WS_URL) as ws:
                    # Send subscription
                    sub = {
                        "APIKey": self.api_key,
                        "BoundingBoxes": AIS_ZONES,
                        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                    }
                    await ws.send(json.dumps(sub))
                    self.connected = True
                    logger.info(f"AIS WebSocket connected — monitoring {len(AIS_ZONES)} zones")

                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            self._process_message(data)
                        except json.JSONDecodeError:
                            continue

            except Exception as e:
                logger.warning(f"AIS WebSocket error: {e} — reconnecting in 10s")
                self.connected = False
                await asyncio.sleep(10)

    def _process_message(self, data):
        """Process an AIS message and update ship cache."""
        msg_type = data.get("MessageType", "")
        meta = data.get("MetaData", {})
        mmsi = meta.get("MMSI", 0)
        if not mmsi:
            return

        self.last_message = datetime.now(timezone.utc)

        if msg_type == "PositionReport":
            pos = data.get("Message", {}).get("PositionReport", {})
            lat = meta.get("latitude") or pos.get("Latitude", 0)
            lon = meta.get("longitude") or pos.get("Longitude", 0)

            if lat == 0 and lon == 0:
                return
            if lat == 91 or lon == 181:  # AIS "not available" values
                return

            ship = self.ships.get(mmsi, {})
            ship.update({
                "mmsi": mmsi,
                "lat": lat, "lon": lon,
                "speed": pos.get("Sog", 0),
                "heading": pos.get("TrueHeading", pos.get("Cog", 0)),
                "course": pos.get("Cog", 0),
                "nav_status": pos.get("NavigationalStatus", 0),
                "updated": datetime.now(timezone.utc).isoformat(),
                "name": ship.get("name") or meta.get("ShipName", "").strip(),
            })
            self.ships[mmsi] = ship

        elif msg_type == "ShipStaticData":
            static = data.get("Message", {}).get("ShipStaticData", {})
            ship = self.ships.get(mmsi, {"mmsi": mmsi})
            ship.update({
                "name": (static.get("Name", "") or meta.get("ShipName", "")).strip(),
                "ship_type": static.get("Type", 0),
                "destination": (static.get("Destination", "") or "").strip(),
                "callsign": (static.get("CallSign", "") or "").strip(),
                "imo": static.get("ImoNumber", 0),
                "lat": meta.get("latitude", ship.get("lat", 0)),
                "lon": meta.get("longitude", ship.get("lon", 0)),
                "updated": datetime.now(timezone.utc).isoformat(),
            })
            self.ships[mmsi] = ship

        # Prune old entries (older than 30 min)
        if len(self.ships) > 5000:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            self.ships = {k: v for k, v in self.ships.items() if v.get("updated", "") > cutoff}

    def get_geojson(self):
        """Return current ship positions as GeoJSON."""
        features = []
        for mmsi, ship in self.ships.items():
            lat = ship.get("lat", 0)
            lon = ship.get("lon", 0)
            if lat == 0 and lon == 0:
                continue

            name = ship.get("name", "Unknown")
            ship_type_code = ship.get("ship_type", 0)
            ship_type_name = get_ship_type_name(ship_type_code)
            military = is_naval_vessel(mmsi, name, ship_type_code)
            speed = ship.get("speed", 0)
            heading = ship.get("heading", 0)
            dest = ship.get("destination", "")

            # Determine display level
            if military:
                level = "high"
            elif ship_type_code in (80, 81, 82, 83, 84, 89):
                level = "moderate"  # Tankers (interesting in conflict zones)
            else:
                level = "low"

            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "mmsi": mmsi,
                    "name": name,
                    "ship_type": ship_type_name,
                    "ship_type_code": ship_type_code,
                    "speed": round(speed, 1),
                    "heading": round(heading, 0),
                    "destination": dest,
                    "military": military,
                    "level": level,
                    "callsign": ship.get("callsign", ""),
                    "imo": ship.get("imo", 0),
                    "updated": ship.get("updated", ""),
                },
            })

        military_count = sum(1 for f in features if f["properties"]["military"])
        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "count": len(features),
                "military": military_count,
                "civilian": len(features) - military_count,
                "fetched_at": datetime.now(timezone.utc).isoformat() + "Z",
                "source": "AISstream.io",
                "ws_connected": self.connected,
            },
        }

    async def close(self):
        self.running = False
        if self._task:
            self._task.cancel()