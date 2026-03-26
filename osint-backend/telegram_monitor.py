"""
Telegram OSINT Monitor v5
- Context-aware geolocation (understands USS Tripoli ≠ Tripoli city)
- Cross-channel verification scoring
- 25+ channels for maximum coverage
- Preposition-weighted location extraction
"""

import re, logging, asyncio
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from telethon import TelegramClient

logger = logging.getLogger("telegram")

# ============================================================
# 25+ CHANNELS — maximum global coverage
# ============================================================
OSINT_CHANNELS = [
    # Intelligence aggregators
    "intelooperx", "TheIntelLab", "CIG_telegram",
    # Regional conflict
    "Middle_East_Spectator", "UkraineNow",
    # Military analysis
    "AMilTHINK", "militabordeaux", "osabordeaux", "warabordeaux", "ryabordeaux",
    # Geopolitical / military tracking
    "GeoPWatch", "AirAlertUA",
    # Breaking conflict
    "breakabordeaux", "SputnikInt",
    # Maritime / shipping
    "MarineTraffic_bot", "SeaSecurityBot",
    # Additional OSINT
    "IntelDoge", "Liveuamap", "ELINTNews",
    "RALee85", "sentdefender", "oabordeaux",
    "NotaWar", "TWZ_DriveMilitary",
    "GeoConfirmed",
]

# ============================================================
# MILITARY NAMES — vessels, systems, operations that share city names
# These should NOT be treated as locations
# ============================================================
MILITARY_NAMES = {
    # Ships named after cities
    "uss tripoli": True, "uss bataan": True, "uss kearsarge": True,
    "uss normandy": True, "uss monterey": True, "uss princeton": True,
    "uss mobile": True, "uss charleston": True, "uss savannah": True,
    "uss denver": True, "uss portland": True, "uss san antonio": True,
    "uss detroit": True, "uss st. louis": True, "uss indianapolis": True,
    "uss philippine sea": True, "uss lake erie": True, "uss cape st. george": True,
    "uss san jacinto": True, "uss vicksburg": True, "uss hue city": True,
    "uss leyte gulf": True, "uss anzio": True, "uss cowpens": True,
    "uss chancellorsville": True, "uss shiloh": True, "uss antietam": True,
    "hms richmond": True, "hms lancaster": True, "hms kent": True,
    # Operations
    "operation tripoli": True, "operation phoenix": True,
    # Designators
    "lha-7": True, "lhd-5": True, "ddg-": True, "cvn-": True,
}

# Check if a location name is actually a military vessel/operation name
def is_military_name(text, place, pos):
    """Check if 'place' at position 'pos' in 'text' is part of a military name."""
    lower = text.lower()
    # Check surrounding context for military designators
    context_start = max(0, pos - 30)
    context = lower[context_start:pos + len(place) + 20]
    for mil_name in MILITARY_NAMES:
        if mil_name in context:
            return True
    # Check if preceded by "USS ", "HMS ", etc.
    prefix = lower[max(0, pos-5):pos].strip()
    if prefix.endswith(("uss", "hms", "ins", "rfs")):
        return True
    return False


# ============================================================
# PREPOSITION WEIGHTING
# Locations after "at", "in", "near", "over", "towards", "on" are more
# likely to be the actual target/location than the subject/actor
# ============================================================
LOCATION_PREPOSITIONS = ["at ", "in ", "near ", "over ", "towards ", "toward ",
                          "on ", "from ", "off ", "outside "]
ACTOR_PREPOSITIONS = ["by ", "from ", "fired by ", "launched by "]


# ============================================================
# SEVERITY
# ============================================================
SEVERITY_WEIGHTS = {
    "critical": {
        "words": ["airstrike", "missile", "bombing", "explosion", "killed",
                  "strike", "casualties", "dead", "MIRV", "ballistic",
                  "nuclear", "WMD", "air raid", "siren", "intercept",
                  "shoot down", "destroyed", "shelling", "impact"],
        "weight": 3,
    },
    "high": {
        "words": ["troops", "military", "combat", "clash", "fighting",
                  "drone", "UAV", "offensive", "attack", "naval",
                  "carrier", "submarine", "convoy", "deploy", "departed",
                  "shahed", "FPV", "HIMARS", "sortie", "scramble",
                  "warship", "destroyer", "frigate"],
        "weight": 2,
    },
    "moderate": {
        "words": ["sanctions", "ceasefire", "tension", "NOTAM",
                  "airspace", "exercise", "surveillance", "patrol",
                  "deployment", "AIS", "transponder", "signal off"],
        "weight": 1,
    },
}

TACTICAL_KEYWORDS = [
    "airstrike", "missile", "strike", "bombing", "shelling", "artillery",
    "drone", "UAV", "UCAV", "intercept", "shoot down", "explosion",
    "troops", "deploy", "military", "naval", "carrier", "warship",
    "attack", "offensive", "battle", "combat", "clash", "fighting",
    "killed", "casualties", "dead", "destroyed",
    "NOTAM", "airspace", "siren", "air raid", "air alert",
    "AIS", "transponder", "dark ship", "signal off",
    "shahed", "FPV", "HIMARS", "ATACMS", "tomahawk", "kalibr",
    "departed", "underway", "transit", "sailed", "fleet",
    "fighter", "bomber", "tanker", "AWACS", "sortie",
    "convoy", "armored", "tank", "satellite image",
    "B-52", "B-1", "F-35", "F-22", "Su-35", "Su-34", "MiG-31",
]


def score_severity(text):
    lower = text.lower()
    total = 0
    max_level = "moderate"
    for level, config in SEVERITY_WEIGHTS.items():
        for word in config["words"]:
            if word.lower() in lower:
                total += config["weight"]
                if config["weight"] == 3: max_level = "critical"
                elif config["weight"] == 2 and max_level != "critical": max_level = "high"
    return max_level, total


def is_relevant(text):
    lower = text.lower()
    return sum(1 for kw in TACTICAL_KEYWORDS if kw.lower() in lower) >= 1


# ============================================================
# THEATERS
# ============================================================
THEATERS = {
    "Iran / Israel": {"center": [49.0, 32.0], "radius": 600,
        "desc": "Iran-Israel confrontation — strikes, missile exchanges, proxy conflicts."},
    "Levant / Gaza": {"center": [35.0, 32.0], "radius": 300,
        "desc": "Gaza conflict, Hezbollah-Israel clashes, Levant instability."},
    "Russia / Ukraine": {"center": [35.0, 48.5], "radius": 500,
        "desc": "Frontline combat, drone warfare, deep strikes."},
    "Red Sea / Yemen": {"center": [42.0, 15.0], "radius": 400,
        "desc": "Houthi anti-shipping, coalition strikes on Yemen."},
    "Persian Gulf": {"center": [52.0, 26.0], "radius": 400,
        "desc": "Shipping disruptions, military activity, Hormuz transit."},
    "South China Sea": {"center": [114.5, 12.0], "radius": 500,
        "desc": "Maritime tensions — China, Philippines, regional powers."},
    "Taiwan Strait": {"center": [119.5, 24.0], "radius": 300,
        "desc": "PLA activity, ADIZ incursions, naval exercises."},
    "Europe / Baltic": {"center": [24.0, 56.0], "radius": 400,
        "desc": "NATO-Russia tensions, infrastructure, Baltic deployments."},
    "Indian Ocean / CENTCOM": {"center": [65.0, 15.0], "radius": 600,
        "desc": "US naval movements, Diego Garcia, Arabian Sea patrols."},
    "Africa — Horn": {"center": [42.0, 8.0], "radius": 500,
        "desc": "Somalia, Sudan, Ethiopia — insurgency and civil conflict."},
    "Africa — Sahel": {"center": [5.0, 15.0], "radius": 600,
        "desc": "Jihadist insurgency — Mali, Niger, Burkina Faso, Nigeria."},
    "Global / Other": {"center": [0, 20], "radius": 300,
        "desc": "Events outside defined theater zones."},
}

# ============================================================
# LOCATION GAZETTEER — [lon, lat, theater]
# ============================================================
LOCATIONS = {
    # IRAN / ISRAEL
    "tehran": [51.39, 35.69, "Iran / Israel"], "isfahan": [51.68, 32.65, "Iran / Israel"],
    "tabriz": [46.30, 38.08, "Iran / Israel"], "shiraz": [52.58, 29.59, "Iran / Israel"],
    "bushehr": [50.84, 28.97, "Iran / Israel"], "natanz": [51.72, 33.51, "Iran / Israel"],
    "parchin": [51.77, 35.52, "Iran / Israel"], "fordow": [51.58, 34.88, "Iran / Israel"],
    "iran": [53.0, 32.5, "Iran / Israel"], "kharg island": [50.33, 29.23, "Iran / Israel"],
    "chabahar": [60.64, 25.29, "Iran / Israel"], "bandar imam": [49.08, 30.43, "Iran / Israel"],
    "irgc": [53.0, 32.5, "Iran / Israel"], "quds force": [53.0, 32.5, "Iran / Israel"],

    # LEVANT / GAZA
    "haifa": [35.00, 32.82, "Levant / Gaza"], "tel aviv": [34.78, 32.08, "Levant / Gaza"],
    "jerusalem": [35.21, 31.77, "Levant / Gaza"], "ben gurion": [34.89, 32.01, "Levant / Gaza"],
    "gaza": [34.47, 31.50, "Levant / Gaza"], "rafah": [34.25, 31.28, "Levant / Gaza"],
    "khan younis": [34.30, 31.35, "Levant / Gaza"], "jabalia": [34.48, 31.53, "Levant / Gaza"],
    "israel": [35.0, 31.5, "Levant / Gaza"], "palestine": [35.2, 31.9, "Levant / Gaza"],
    "west bank": [35.3, 31.9, "Levant / Gaza"], "golan heights": [35.78, 33.0, "Levant / Gaza"],
    "beirut": [35.50, 33.89, "Levant / Gaza"], "tyre": [35.20, 33.27, "Levant / Gaza"],
    "sidon": [35.37, 33.56, "Levant / Gaza"], "lebanon": [35.86, 33.87, "Levant / Gaza"],
    "hezbollah": [35.5, 33.5, "Levant / Gaza"], "hamas": [34.47, 31.50, "Levant / Gaza"],
    "idf": [35.0, 31.5, "Levant / Gaza"], "dimona": [35.02, 31.07, "Levant / Gaza"],
    "beer sheva": [34.79, 31.25, "Levant / Gaza"], "eilat": [34.95, 29.56, "Levant / Gaza"],
    "nevatim": [34.94, 31.21, "Levant / Gaza"], "ramon": [34.67, 30.78, "Levant / Gaza"],
    "damascus": [36.29, 33.51, "Levant / Gaza"], "aleppo": [37.16, 36.20, "Levant / Gaza"],
    "latakia": [35.78, 35.52, "Levant / Gaza"], "syria": [38.0, 35.0, "Levant / Gaza"],
    "homs": [36.72, 34.73, "Levant / Gaza"], "deir ez-zor": [40.14, 35.33, "Levant / Gaza"],
    "northern israel": [35.3, 33.0, "Levant / Gaza"], "southern lebanon": [35.4, 33.3, "Levant / Gaza"],

    # RUSSIA / UKRAINE
    "kyiv": [30.52, 50.45, "Russia / Ukraine"], "kharkiv": [36.25, 49.99, "Russia / Ukraine"],
    "odesa": [30.73, 46.48, "Russia / Ukraine"], "zaporizhzhia": [35.14, 47.84, "Russia / Ukraine"],
    "dnipro": [35.05, 48.46, "Russia / Ukraine"], "lviv": [24.03, 49.84, "Russia / Ukraine"],
    "kherson": [32.62, 46.64, "Russia / Ukraine"], "mariupol": [37.55, 47.10, "Russia / Ukraine"],
    "bakhmut": [38.00, 48.60, "Russia / Ukraine"], "avdiivka": [37.74, 48.14, "Russia / Ukraine"],
    "donetsk": [37.80, 48.00, "Russia / Ukraine"], "luhansk": [39.30, 48.57, "Russia / Ukraine"],
    "donbas": [38.0, 48.2, "Russia / Ukraine"], "crimea": [34.1, 44.95, "Russia / Ukraine"],
    "sevastopol": [33.52, 44.62, "Russia / Ukraine"], "moscow": [37.62, 55.76, "Russia / Ukraine"],
    "rostov": [39.72, 47.24, "Russia / Ukraine"], "belgorod": [36.59, 50.60, "Russia / Ukraine"],
    "kursk": [36.19, 51.73, "Russia / Ukraine"], "novorossiysk": [37.77, 44.72, "Russia / Ukraine"],
    "ukraine": [35.5, 48.5, "Russia / Ukraine"], "russia": [37.6, 55.75, "Russia / Ukraine"],
    "black sea": [34.0, 43.5, "Russia / Ukraine"], "sea of azov": [36.5, 46.0, "Russia / Ukraine"],
    "pokrovsk": [37.18, 48.29, "Russia / Ukraine"], "sumy": [34.80, 50.91, "Russia / Ukraine"],
    "poltava": [34.55, 49.59, "Russia / Ukraine"], "mykolaiv": [32.00, 46.97, "Russia / Ukraine"],
    "kramatorsk": [37.56, 48.74, "Russia / Ukraine"], "sloviansk": [37.60, 48.85, "Russia / Ukraine"],
    "melitopol": [35.37, 46.85, "Russia / Ukraine"], "vuhledar": [37.25, 47.77, "Russia / Ukraine"],
    "kerch": [36.47, 45.36, "Russia / Ukraine"], "tokmak": [35.71, 47.25, "Russia / Ukraine"],
    "primorsk": [28.60, 60.36, "Russia / Ukraine"],

    # RED SEA / YEMEN
    "sanaa": [44.21, 15.35, "Red Sea / Yemen"], "hodeidah": [42.95, 14.80, "Red Sea / Yemen"],
    "aden": [45.04, 12.79, "Red Sea / Yemen"], "yemen": [44.0, 15.5, "Red Sea / Yemen"],
    "houthi": [44.0, 15.5, "Red Sea / Yemen"], "ansar allah": [44.0, 15.5, "Red Sea / Yemen"],
    "red sea": [39.0, 19.0, "Red Sea / Yemen"], "bab el-mandeb": [43.3, 12.6, "Red Sea / Yemen"],
    "bab al-mandab": [43.3, 12.6, "Red Sea / Yemen"], "gulf of aden": [47.0, 12.0, "Red Sea / Yemen"],
    "suez canal": [32.34, 30.46, "Red Sea / Yemen"], "marib": [45.32, 15.47, "Red Sea / Yemen"],

    # PERSIAN GULF
    "strait of hormuz": [56.3, 26.6, "Persian Gulf"], "hormuz": [56.3, 26.6, "Persian Gulf"],
    "persian gulf": [52.0, 26.5, "Persian Gulf"], "arabian sea": [62.0, 15.0, "Persian Gulf"],
    "bahrain": [50.55, 26.07, "Persian Gulf"], "qatar": [51.53, 25.29, "Persian Gulf"],
    "uae": [54.37, 24.45, "Persian Gulf"], "oman": [57.0, 21.5, "Persian Gulf"],
    "saudi arabia": [45.0, 24.0, "Persian Gulf"], "riyadh": [46.72, 24.71, "Persian Gulf"],
    "jeddah": [39.17, 21.49, "Persian Gulf"], "ras laffan": [51.53, 25.90, "Persian Gulf"],
    "iraq": [44.0, 33.0, "Persian Gulf"], "baghdad": [44.36, 33.31, "Persian Gulf"],
    "erbil": [44.01, 36.19, "Persian Gulf"], "basra": [47.78, 30.51, "Persian Gulf"],
    "bandar abbas": [56.27, 27.19, "Persian Gulf"], "al udeid": [51.31, 25.12, "Persian Gulf"],
    "al dhafra": [54.55, 24.25, "Persian Gulf"], "kuwait": [47.98, 29.37, "Persian Gulf"],

    # INDIAN OCEAN / CENTCOM
    "diego garcia": [72.41, -7.32, "Indian Ocean / CENTCOM"],
    "indian ocean": [73.0, -5.0, "Indian Ocean / CENTCOM"],
    "arabian sea": [62.0, 15.0, "Indian Ocean / CENTCOM"],
    "gulf of oman": [59.0, 24.5, "Indian Ocean / CENTCOM"],
    "camp lemonnier": [43.15, 11.55, "Indian Ocean / CENTCOM"],
    "djibouti": [43.15, 11.59, "Indian Ocean / CENTCOM"],

    # SOUTH CHINA SEA
    "south china sea": [114.0, 12.0, "South China Sea"],
    "scarborough shoal": [117.76, 15.23, "South China Sea"],
    "second thomas shoal": [115.85, 9.72, "South China Sea"],
    "spratly": [114.0, 10.0, "South China Sea"], "paracel": [112.0, 16.5, "South China Sea"],
    "manila": [120.98, 14.60, "South China Sea"], "philippines": [121.0, 12.0, "South China Sea"],
    "hainan": [110.0, 19.0, "South China Sea"], "fiery cross": [112.89, 9.55, "South China Sea"],
    "mischief reef": [115.53, 9.90, "South China Sea"],

    # TAIWAN STRAIT
    "taiwan": [121.0, 23.7, "Taiwan Strait"], "taipei": [121.56, 25.03, "Taiwan Strait"],
    "taiwan strait": [119.5, 24.0, "Taiwan Strait"],

    # EUROPE / BALTIC
    "baltic sea": [20.0, 57.0, "Europe / Baltic"], "gulf of finland": [27.0, 60.0, "Europe / Baltic"],
    "kaliningrad": [20.51, 54.71, "Europe / Baltic"], "gotland": [18.47, 57.47, "Europe / Baltic"],

    # AFRICA
    "mogadishu": [45.34, 2.05, "Africa — Horn"], "somalia": [46.2, 5.15, "Africa — Horn"],
    "khartoum": [32.53, 15.59, "Africa — Horn"], "port sudan": [37.22, 19.62, "Africa — Horn"],
    "sudan": [30.0, 15.0, "Africa — Horn"], "ethiopia": [38.7, 9.0, "Africa — Horn"],
    "eritrea": [38.9, 15.3, "Africa — Horn"],
    "mali": [-8.0, 17.5, "Africa — Sahel"], "niger": [8.0, 17.6, "Africa — Sahel"],
    "burkina faso": [-1.5, 12.3, "Africa — Sahel"], "sahel": [0.0, 15.0, "Africa — Sahel"],
    "libya": [17.0, 27.0, "Africa — Sahel"], "nigeria": [7.5, 9.1, "Africa — Sahel"],
    "tripoli": [13.18, 32.90, "Africa — Sahel"],

    # GLOBAL
    "pentagon": [-77.06, 38.87, "Global / Other"], "washington": [-77.04, 38.90, "Global / Other"],
    "nato": [4.36, 50.84, "Global / Other"], "centcom": [-82.6, 28.4, "Global / Other"],
    "myanmar": [96.0, 19.7, "Global / Other"], "north korea": [125.75, 39.02, "Global / Other"],
    "south korea": [127.0, 37.5, "Global / Other"], "japan": [139.7, 35.7, "Global / Other"],
    "china": [116.4, 39.9, "Global / Other"], "pla": [116.4, 39.9, "Global / Other"],
    "afghanistan": [67.7, 33.9, "Global / Other"], "pakistan": [73.0, 33.7, "Global / Other"],
    "egypt": [31.24, 30.04, "Global / Other"], "turkey": [32.86, 39.93, "Global / Other"],
}


def extract_location(text):
    """
    Context-aware location extraction.
    1. Skip military vessel/operation names
    2. Prefer locations after prepositions (at, in, near)
    3. Longer (more specific) names always beat shorter ones
    """
    lower = text.lower()
    sorted_locs = sorted(LOCATIONS.items(), key=lambda x: len(x[0]), reverse=True)

    best_match = None
    best_score = -1  # composite score: specificity + preposition bonus

    for place, data in sorted_locs:
        pos = lower.rfind(place)
        if pos < 0:
            continue

        # Skip if part of a military vessel name
        if is_military_name(text, place, pos):
            continue

        # Base score = name length (specificity)
        score = len(place) * 10

        # Bonus if preceded by a location preposition
        prefix = lower[max(0, pos - 12):pos].strip()
        for prep in LOCATION_PREPOSITIONS:
            if prefix.endswith(prep.strip()):
                score += 50  # Strong bonus
                break

        # Penalty if preceded by actor preposition (for short/country names)
        if len(place) < 6:
            for prep in ACTOR_PREPOSITIONS:
                if prefix.endswith(prep.strip()):
                    score -= 100
                    break

        # Bonus for appearing later in text (target > subject)
        score += pos * 0.1

        if score > best_score:
            best_match = place
            best_score = score

    if best_match:
        d = LOCATIONS[best_match]
        return best_match.title(), [d[0], d[1]], d[2]
    return None, None, None


def generate_summary(text):
    clean = re.sub(r'[^\w\s.,;:!?\-\'\"()/]', '', text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    sentences = re.split(r'[.!?]+', clean)
    summary = '. '.join(s.strip() for s in sentences[:2] if s.strip())
    return (summary[:200] if summary else clean[:200]) or "No details"


def text_similarity(a, b):
    """Quick similarity check between two texts."""
    return SequenceMatcher(None, a[:80].lower(), b[:80].lower()).ratio()


class TelegramMonitor:
    def __init__(self, api_id, api_hash, session_name="osint_session"):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.session_name = session_name
        self.client = None
        self.events = []
        self.started = False

    async def start(self):
        try:
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
            await self.client.start()
            me = await self.client.get_me()
            logger.info(f"Telegram connected as {me.first_name}")
            self.started = True
            return True
        except Exception as e:
            logger.error(f"Telegram failed: {e}")
            return False

    async def fetch_channel_messages(self, channel, hours=48, limit=100):
        if not self.started: return []
        try:
            entity = await self.client.get_entity(channel)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            msgs = []
            async for msg in self.client.iter_messages(entity, limit=limit):
                if msg.date < cutoff: break
                if msg.text and len(msg.text) > 20:
                    msgs.append({"text": msg.text, "date": msg.date, "channel": channel, "id": msg.id})
            return msgs
        except Exception as e:
            logger.warning(f"@{channel}: {e}")
            return []

    async def fetch_all_channels(self, hours=48):
        if not self.started: return []
        results = await asyncio.gather(
            *[self.fetch_channel_messages(ch, hours) for ch in OSINT_CHANNELS],
            return_exceptions=True
        )
        msgs = []
        for r in results:
            if isinstance(r, list): msgs.extend(r)
        logger.info(f"Telegram: {len(msgs)} messages from {len(OSINT_CHANNELS)} channels")
        return msgs

    def _compute_verification(self, features):
        """
        Cross-reference events across channels.
        Events reported by multiple channels get higher confidence.
        
        Returns features with 'verified_by' count and 'confidence' level.
        """
        for i, f in enumerate(features):
            channels_reporting = set()
            channels_reporting.add(f["properties"].get("source", ""))

            for j, other in enumerate(features):
                if i == j: continue
                # Same approximate location (within ~1 degree)
                d_lon = abs(f["geometry"]["coordinates"][0] - other["geometry"]["coordinates"][0])
                d_lat = abs(f["geometry"]["coordinates"][1] - other["geometry"]["coordinates"][1])
                if d_lon > 2 or d_lat > 2:
                    continue
                # Similar text content
                sim = text_similarity(
                    f["properties"].get("text", ""),
                    other["properties"].get("text", "")
                )
                if sim > 0.35:
                    channels_reporting.add(other["properties"].get("source", ""))

            n = len(channels_reporting)
            f["properties"]["verified_by"] = n
            if n >= 3:
                f["properties"]["confidence"] = "confirmed"
            elif n == 2:
                f["properties"]["confidence"] = "corroborated"
            else:
                f["properties"]["confidence"] = "single source"

        return features

    async def fetch_events_geojson(self, hours=48):
        messages = await self.fetch_all_channels(hours)
        features = []
        seen = set()

        for msg in messages:
            text = msg["text"]
            if not is_relevant(text): continue

            loc_name, coords, theater = extract_location(text)
            if not coords: continue

            text_key = text[:50].lower().strip()
            if text_key in seen: continue
            seen.add(text_key)

            level, sev_score = score_severity(text)
            summary = generate_summary(text)
            short_text = text[:120].replace('\n', ' ')

            msg_date = msg["date"]
            delta = datetime.now(timezone.utc) - (msg_date if msg_date.tzinfo else msg_date.replace(tzinfo=timezone.utc))
            secs = delta.total_seconds()
            time_str = f"{int(secs/60)}m ago" if secs < 3600 else f"{int(secs/3600)}h ago" if secs < 86400 else f"{int(secs/86400)}d ago"

            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": coords},
                "properties": {
                    "text": short_text, "time": time_str, "conflict": loc_name,
                    "level": level, "severity_score": sev_score,
                    "theater": theater or "Global / Other",
                    "event_type": "Telegram OSINT", "sub_event_type": "",
                    "actor1": "", "actor2": "", "fatalities": 0,
                    "country": loc_name, "location": loc_name,
                    "date": msg_date.strftime("%Y-%m-%d"),
                    "notes": summary,
                    "source": f"Telegram @{msg['channel']}",
                    "url": f"https://t.me/{msg['channel']}/{msg['id']}",
                    "verified_by": 1, "confidence": "single source",
                },
            })

        # Cross-channel verification
        features = self._compute_verification(features)

        features.sort(key=lambda f: f["properties"]["date"], reverse=True)
        self.events = features
        
        confirmed = sum(1 for f in features if f["properties"]["confidence"] == "confirmed")
        corroborated = sum(1 for f in features if f["properties"]["confidence"] == "corroborated")
        logger.info(f"Telegram: {len(features)} events ({confirmed} confirmed, {corroborated} corroborated)")

        return {
            "type": "FeatureCollection", "features": features,
            "metadata": {
                "count": len(features),
                "confirmed": confirmed, "corroborated": corroborated,
                "fetched_at": datetime.utcnow().isoformat() + "Z",
                "source": "Telegram", "channels": len(OSINT_CHANNELS), "hours": hours,
            }
        }

    async def close(self):
        if self.client: await self.client.disconnect()