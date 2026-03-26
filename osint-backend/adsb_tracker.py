"""
ADS-B Aircraft Tracker via OpenSky Network
- OAuth2 with automatic fallback to anonymous access
- Detailed error logging for cloud deployment debugging
- Reduced zones to conserve API credits
"""

import httpx, logging, re
from datetime import datetime, timedelta

logger = logging.getLogger("adsb")

MILITARY_PREFIXES = [
    "RCH", "REACH", "EVAC", "DUKE", "VALOR", "FATE", "DOOM", "SPAR", "SAM",
    "VENUS", "TOPCT", "GORDO", "KNIFE", "JAKE", "HAWK", "VIPER", "COBRA",
    "NATO", "ASCOT", "RRR", "GAF", "GERM", "FAF", "CTM", "IAM",
    "IRON", "STEEL", "PEARL", "FORTE", "LAGR", "HOMER", "PNTHR",
]

MILITARY_HEX_RANGES = [
    ("ae0000", "afffff"),
    ("3b0000", "3bffff"),
    ("3f0000", "3fffff"),
    ("43c000", "43cfff"),
]

CONFLICT_ZONES = {
    "Middle East": [20, 30, 42, 65],
    "Ukraine / Black Sea": [43, 25, 55, 42],
    "Red Sea / Horn": [5, 35, 22, 55],
    "South China Sea": [5, 105, 25, 125],
}


def is_military(callsign, icao24):
    cs = (callsign or "").strip().upper()
    for prefix in MILITARY_PREFIXES:
        if cs.startswith(prefix):
            return True
    try:
        hex_val = int(icao24, 16)
        for low, high in MILITARY_HEX_RANGES:
            if int(low, 16) <= hex_val <= int(high, 16):
                return True
    except (ValueError, TypeError):
        pass
    return False


def classify_aircraft(callsign, icao24, on_ground, velocity, altitude):
    cs = (callsign or "").strip().upper()
    if is_military(callsign, icao24):
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
    return "Civilian", "low"


class ADSBTracker:
    AUTH_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    API_URL = "https://opensky-network.org/api/states/all"

    def __init__(self, client_id=None, client_secret=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expiry = None
        self.auth_failed = False
        self.http = httpx.AsyncClient(timeout=60.0)

    async def _get_token(self):
        if self.auth_failed:
            return None
        if self.token and self.token_expiry and datetime.utcnow() < self.token_expiry:
            return self.token
        if not self.client_id or not self.client_secret:
            return None
        try:
            logger.info("Requesting OpenSky OAuth2 token...")
            r = await self.http.post(self.AUTH_URL, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15.0)
            if r.status_code == 200:
                data = r.json()
                self.token = data["access_token"]
                expires_in = data.get("expires_in", 300)
                self.token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - 30)
                logger.info(f"OpenSky token OK (expires in {expires_in}s)")
                return self.token
            elif r.status_code == 401:
                logger.error("OpenSky 401 — invalid credentials, switching to anonymous")
                self.auth_failed = True
                return None
            else:
                logger.warning(f"OpenSky auth HTTP {r.status_code}: {r.text[:100]}")
                return None
        except httpx.ConnectError:
            logger.warning("OpenSky auth: connection refused — switching to anonymous")
            self.auth_failed = True
            return None
        except httpx.TimeoutException:
            logger.warning("OpenSky auth: timeout — will retry next cycle")
            return None
        except Exception as e:
            logger.warning(f"OpenSky auth: {type(e).__name__}: {e}")
            return None

    async def fetch_zone(self, zone_name, bbox):
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        params = {"lamin": bbox[0], "lomin": bbox[1], "lamax": bbox[2], "lomax": bbox[3]}
        try:
            r = await self.http.get(self.API_URL, params=params, headers=headers, timeout=20.0)
            if r.status_code == 200:
                states = r.json().get("states", []) or []
                logger.info(f"OpenSky {zone_name}: {len(states)} aircraft")
                return states
            elif r.status_code == 429:
                logger.warning(f"OpenSky {zone_name}: rate limited (429)")
                return []
            else:
                logger.warning(f"OpenSky {zone_name}: HTTP {r.status_code}")
                return []
        except httpx.TimeoutException:
            logger.warning(f"OpenSky {zone_name}: timeout")
            return []
        except httpx.ConnectError:
            logger.warning(f"OpenSky {zone_name}: connection failed")
            return []
        except Exception as e:
            logger.warning(f"OpenSky {zone_name}: {type(e).__name__}: {e}")
            return []

    async def fetch_all_zones(self):
        all_states = []
        seen = set()
        for name, bbox in CONFLICT_ZONES.items():
            states = await self.fetch_zone(name, bbox)
            for s in states:
                if s[0] not in seen:
                    seen.add(s[0])
                    all_states.append((name, s))
        logger.info(f"OpenSky total: {len(all_states)} aircraft across {len(CONFLICT_ZONES)} zones")
        return all_states

    async def fetch_geojson(self, military_only=False):
        all_states = await self.fetch_all_zones()
        features = []
        for zone_name, s in all_states:
            try:
                icao24 = s[0] or ""
                callsign = (s[1] or "").strip()
                country = s[2] or ""
                lon, lat = s[5], s[6]
                altitude = s[7] or s[13] or 0
                on_ground = s[8] or False
                velocity = s[9] or 0
                heading = s[10] or 0
                vert_rate = s[11] or 0
                squawk = s[14] or ""
                if lon is None or lat is None: continue
                ac_type, level = classify_aircraft(callsign, icao24, on_ground, velocity, altitude)
                if military_only and ac_type == "Civilian": continue
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "icao24": icao24, "callsign": callsign, "country": country,
                        "altitude_ft": round(altitude * 3.281) if altitude else 0,
                        "speed_kts": round(velocity * 1.944) if velocity else 0,
                        "heading": round(heading), "vert_rate": round(vert_rate, 1) if vert_rate else 0,
                        "on_ground": on_ground, "squawk": squawk,
                        "type": ac_type, "level": level,
                        "military": ac_type != "Civilian", "zone": zone_name,
                    },
                })
            except (IndexError, TypeError): continue
        return {
            "type": "FeatureCollection", "features": features,
            "metadata": {
                "count": len(features),
                "military": sum(1 for f in features if f["properties"]["military"]),
                "civilian": sum(1 for f in features if not f["properties"]["military"]),
                "fetched_at": datetime.utcnow().isoformat() + "Z",
                "source": "OpenSky Network",
                "auth": "token" if self.token else "anonymous",
            },
        }

    async def close(self):
        await self.http.aclose()