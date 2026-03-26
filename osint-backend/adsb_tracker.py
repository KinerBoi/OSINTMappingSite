"""
ADS-B Aircraft Tracker via OpenSky Network
- OAuth2 token auth (client_credentials flow)
- Polls conflict zone bounding boxes for aircraft
- Filters/tags military aircraft by callsign patterns
- Returns GeoJSON for map overlay
"""

import httpx, logging, re
from datetime import datetime, timedelta

logger = logging.getLogger("adsb")

# Military callsign prefixes and patterns
MILITARY_PREFIXES = [
    # US Military
    "RCH", "REACH", "EVAC", "DUKE", "VALOR", "FATE", "DOOM",
    "SPAR",  # Special Air Resource (VIP)
    "SAM",   # Special Air Mission
    "VENUS", "TOPCT", "GORDO", "KNIFE",
    "JAKE", "HAWK", "VIPER", "COBRA",
    "MOOSE", "BULL", "TEAL", "NCHO",
    # NATO / Allied
    "NATO", "ASCOT", "RRR",  # RAF
    "GAF", "GERM",  # German Air Force
    "FAF", "CTM",   # French Air Force
    "IAM", "ITAL",  # Italian Air Force
    "TUAF",         # Turkish Air Force
    # Russian
    "RSD", "RFF", "AFL",
    # Tankers / AWACS / ISR
    "IRON", "STEEL", "PEARL",
    "FORTE",  # Global Hawk
    "LAGR",   # MC-12
    "HOMER",  # P-8 Poseidon
    "PNTHR",  # E-2 Hawkeye
]

# Military ICAO hex ranges (partial — US military aircraft)
# Full list at: https://www.ads-b.nl/
MILITARY_HEX_RANGES = [
    ("ae0000", "afffff"),  # US Military
    ("3b0000", "3bffff"),  # French Military
    ("3f0000", "3fffff"),  # German Military
    ("43c000", "43cfff"),  # UK Military
]

# Conflict zone bounding boxes: [lat_min, lon_min, lat_max, lon_max]
CONFLICT_ZONES = {
    "Middle East": [20, 30, 40, 65],
    "Ukraine / Black Sea": [43, 25, 55, 45],
    "Eastern Mediterranean": [30, 25, 40, 37],
    "Red Sea / Horn": [5, 35, 20, 55],
    "South China Sea": [5, 105, 25, 125],
    "Persian Gulf": [22, 45, 32, 60],
    "Baltic": [53, 15, 62, 32],
}

# Aircraft type codes that are military
MILITARY_TYPES = {
    "F16", "F15", "F18", "F22", "F35", "F14",
    "B52", "B1", "B2", "B21",
    "C17", "C5", "C130", "C40",
    "KC135", "KC10", "KC46",
    "E3", "E6", "E8", "E2",
    "P3", "P8",
    "RQ4", "MQ9", "MQ1",  # Drones
    "V22",  # Osprey
    "H60", "H53", "H47",  # Helicopters
    "A10",  # Warthog
    "RC135", "EP3", "U2",  # ISR
    "SU27", "SU30", "SU34", "SU35", "SU57",
    "MIG29", "MIG31",
    "TU95", "TU160", "IL76", "AN124",
    "EUFI",  # Eurofighter
    "RFAL",  # Rafale
    "TORNADO",
}


def is_military(callsign, icao24):
    """Check if aircraft is military by callsign pattern or ICAO hex range."""
    cs = (callsign or "").strip().upper()

    # Check callsign prefixes
    for prefix in MILITARY_PREFIXES:
        if cs.startswith(prefix):
            return True

    # Check ICAO hex range
    try:
        hex_val = int(icao24, 16)
        for low, high in MILITARY_HEX_RANGES:
            if int(low, 16) <= hex_val <= int(high, 16):
                return True
    except (ValueError, TypeError):
        pass

    # Check if callsign looks non-commercial (no airline prefix + number pattern)
    if cs and not re.match(r'^[A-Z]{2,3}\d{1,4}[A-Z]?$', cs):
        # Doesn't match typical airline callsign pattern — could be military
        if len(cs) >= 4 and not cs[0:3].isdigit():
            return True

    return False


def classify_aircraft(callsign, icao24, on_ground, velocity, altitude):
    """Classify aircraft type based on available data."""
    cs = (callsign or "").strip().upper()

    if is_military(callsign, icao24):
        # Try to guess type
        if any(cs.startswith(p) for p in ["FORTE", "RQ", "MQ", "LAGR"]):
            return "ISR / Drone", "critical"
        if any(cs.startswith(p) for p in ["IRON", "STEEL", "KC"]):
            return "Tanker", "high"
        if any(cs.startswith(p) for p in ["RCH", "REACH", "EVAC"]):
            return "Transport", "moderate"
        if any(cs.startswith(p) for p in ["HOMER", "PNTHR"]):
            return "Maritime Patrol", "high"
        if any(cs.startswith(p) for p in ["SAM", "SPAR"]):
            return "VIP / Government", "high"
        return "Military", "high"
    else:
        return "Civilian", "low"


class ADSBTracker:
    """
    Tracks aircraft using OpenSky Network REST API.
    Uses OAuth2 client_credentials for authentication.
    """

    AUTH_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    API_URL = "https://opensky-network.org/api/states/all"

    def __init__(self, client_id=None, client_secret=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expiry = None
        self.http = httpx.AsyncClient(timeout=60.0)

    async def _get_token(self):
        """Get OAuth2 token using client_credentials flow."""
        if self.token and self.token_expiry and datetime.utcnow() < self.token_expiry:
            return self.token

        if not self.client_id or not self.client_secret:
            logger.info("No OpenSky credentials — using anonymous access")
            return None

        try:
            r = await self.http.post(self.AUTH_URL, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})

            if r.status_code == 200:
                data = r.json()
                self.token = data["access_token"]
                expires_in = data.get("expires_in", 300)
                self.token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - 30)
                logger.info(f"OpenSky token obtained (expires in {expires_in}s)")
                return self.token
            else:
                logger.error(f"OpenSky auth failed: {r.status_code}")
                return None
        except Exception as e:
            logger.error(f"OpenSky auth error: {e}")
            return None

    async def fetch_zone(self, zone_name, bbox):
        """Fetch aircraft in a single bounding box."""
        token = await self._get_token()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        params = {
            "lamin": bbox[0], "lomin": bbox[1],
            "lamax": bbox[2], "lomax": bbox[3],
        }

        try:
            r = await self.http.get(self.API_URL, params=params, headers=headers)
            if r.status_code == 200:
                data = r.json()
                states = data.get("states", []) or []
                logger.info(f"OpenSky {zone_name}: {len(states)} aircraft")
                return states
            elif r.status_code == 429:
                logger.warning("OpenSky rate limited")
                return []
            else:
                logger.warning(f"OpenSky {zone_name}: {r.status_code}")
                return []
        except Exception as e:
            logger.error(f"OpenSky {zone_name} error: {e}")
            return []

    async def fetch_all_zones(self):
        """Fetch aircraft from all conflict zones."""
        all_states = []
        seen_icao = set()

        for zone_name, bbox in CONFLICT_ZONES.items():
            states = await self.fetch_zone(zone_name, bbox)
            for s in states:
                icao = s[0]
                if icao in seen_icao:
                    continue
                seen_icao.add(icao)
                all_states.append((zone_name, s))

        logger.info(f"OpenSky total: {len(all_states)} unique aircraft across {len(CONFLICT_ZONES)} zones")
        return all_states

    async def fetch_geojson(self, military_only=False):
        """Fetch aircraft and return as GeoJSON."""
        all_states = await self.fetch_all_zones()
        features = []

        for zone_name, s in all_states:
            # OpenSky state vector fields:
            # 0:icao24, 1:callsign, 2:origin_country, 3:time_position,
            # 4:last_contact, 5:longitude, 6:latitude, 7:baro_altitude,
            # 8:on_ground, 9:velocity, 10:true_track, 11:vertical_rate,
            # 12:sensors, 13:geo_altitude, 14:squawk, 15:spi, 16:position_source
            try:
                icao24 = s[0] or ""
                callsign = (s[1] or "").strip()
                country = s[2] or ""
                lon = s[5]
                lat = s[6]
                altitude = s[7] or s[13] or 0
                on_ground = s[8] or False
                velocity = s[9] or 0
                heading = s[10] or 0
                vert_rate = s[11] or 0
                squawk = s[14] or ""

                if lon is None or lat is None:
                    continue

                ac_type, level = classify_aircraft(callsign, icao24, on_ground, velocity, altitude)

                if military_only and ac_type == "Civilian":
                    continue

                # Convert m/s to knots, meters to feet
                speed_kts = round(velocity * 1.944, 0) if velocity else 0
                alt_ft = round(altitude * 3.281, 0) if altitude else 0

                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "icao24": icao24,
                        "callsign": callsign,
                        "country": country,
                        "altitude_ft": alt_ft,
                        "speed_kts": speed_kts,
                        "heading": round(heading, 0),
                        "vert_rate": round(vert_rate, 1) if vert_rate else 0,
                        "on_ground": on_ground,
                        "squawk": squawk,
                        "type": ac_type,
                        "level": level,
                        "military": ac_type != "Civilian",
                        "zone": zone_name,
                    },
                })
            except (IndexError, TypeError):
                continue

        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "count": len(features),
                "military": sum(1 for f in features if f["properties"]["military"]),
                "civilian": sum(1 for f in features if not f["properties"]["military"]),
                "fetched_at": datetime.utcnow().isoformat() + "Z",
                "source": "OpenSky Network",
            },
        }

    async def close(self):
        await self.http.aclose()