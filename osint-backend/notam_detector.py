"""
NOTAM Detector — Telegram-sourced airspace restrictions and GPS jamming
- Detects NOTAM/airspace/jamming keywords in Telegram messages
- Assigns TTL (time-to-live) — dots expire after 24h if not re-reported
- Cancellation keywords ("reopened", "lifted") actively remove entries
- Cross-references with location gazetteer for geolocation
"""

import re, logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("notam")

# ============================================================
# NOTAM DETECTION KEYWORDS
# ============================================================
NOTAM_KEYWORDS = [
    # Airspace restrictions
    "airspace closed", "airspace closure", "FIR closed", "FIR closure",
    "no-fly zone", "no fly zone", "restricted airspace", "prohibited airspace",
    "NOTAM", "notam", "TFR",
    "airspace shut", "flights suspended", "flights grounded",
    "airport closed", "airport closure", "runway closed",
    "flight ban", "overflight ban", "overflight prohibited",
    # GPS / Electronic warfare
    "GPS jamming", "GPS spoofing", "GNSS interference", "GNSS jamming",
    "GPS interference", "GPS disruption", "navigation interference",
    "electronic warfare", "signal jamming", "radar jamming",
    "EGPWS", "false EGPWS", "spurious EGPWS",
    # Military airspace
    "military activity", "military exercise", "live fire exercise",
    "missile test", "weapons test", "air defense active",
    "SAM active", "air defense engagement",
]

# Keywords that indicate a restriction is LIFTED
CANCELLATION_KEYWORDS = [
    "reopened", "reopening", "re-opened", "re-opening",
    "lifted", "lifting", "resumed", "resuming",
    "restored", "restoring", "normalized",
    "flights resume", "airspace open", "back to normal",
    "restriction lifted", "ban lifted",
    "NOTAM cancelled", "NOTAM canceled",
]

# Classify NOTAM type
NOTAM_TYPES = {
    "airspace_closed": ["airspace closed", "FIR closed", "airspace closure", "FIR closure",
                        "no-fly zone", "no fly zone", "flights suspended", "airport closed",
                        "flight ban", "overflight ban", "airspace shut", "flights grounded",
                        "prohibited airspace", "overflight prohibited", "airport closure"],
    "restricted": ["restricted airspace", "NOTAM", "TFR", "military activity",
                   "military exercise", "live fire", "runway closed"],
    "gps_jamming": ["GPS jamming", "GPS spoofing", "GNSS interference", "GNSS jamming",
                    "GPS interference", "GPS disruption", "navigation interference",
                    "electronic warfare", "signal jamming", "EGPWS", "radar jamming"],
    "military_activity": ["SAM active", "air defense", "missile test", "weapons test",
                          "air defense engagement", "air defense active"],
}

# FIR identifiers → locations
FIR_LOCATIONS = {
    "OIIX": [53.0, 32.5, "Tehran FIR (Iran)"],
    "ORBB": [44.0, 33.0, "Baghdad FIR (Iraq)"],
    "LLLL": [35.0, 31.5, "Tel Aviv FIR (Israel)"],
    "OSTT": [38.0, 35.0, "Damascus FIR (Syria)"],
    "OLBB": [35.86, 33.87, "Beirut FIR (Lebanon)"],
    "OYSC": [44.0, 15.5, "Sanaa FIR (Yemen)"],
    "OKAC": [47.5, 29.3, "Kuwait FIR"],
    "OMAE": [54.5, 24.5, "Emirates FIR (UAE)"],
    "OBBB": [50.55, 26.07, "Bahrain FIR"],
    "OTDF": [51.5, 25.3, "Doha FIR (Qatar)"],
    "OOMM": [57.0, 21.5, "Muscat FIR (Oman)"],
    "OEJD": [39.2, 21.5, "Jeddah FIR (Saudi)"],
    "HCSM": [45.3, 2.0, "Mogadishu FIR (Somalia)"],
    "OAKX": [69.2, 34.5, "Kabul FIR (Afghanistan)"],
}

# TTL for NOTAM events (hours)
DEFAULT_TTL_HOURS = 24
CONFIRMED_TTL_HOURS = 48  # Longer TTL for multi-source confirmed events


def is_notam_related(text):
    """Check if a message is about airspace restrictions, NOTAMs, or GPS jamming."""
    lower = text.lower()
    return any(kw.lower() in lower for kw in NOTAM_KEYWORDS)


def is_cancellation(text):
    """Check if a message indicates a restriction being lifted."""
    lower = text.lower()
    return any(kw.lower() in lower for kw in CANCELLATION_KEYWORDS)


def classify_notam_type(text):
    """Determine the NOTAM sub-type."""
    lower = text.lower()
    for ntype, keywords in NOTAM_TYPES.items():
        for kw in keywords:
            if kw.lower() in lower:
                return ntype
    return "restricted"


def extract_fir(text):
    """Try to extract FIR identifier from text."""
    upper = text.upper()
    for fir_code, data in FIR_LOCATIONS.items():
        if fir_code in upper:
            return fir_code, data
    return None, None


class NOTAMDetector:
    """
    Processes Telegram messages to detect and track airspace restrictions.
    
    Usage:
        detector = NOTAMDetector()
        # Call with each batch of telegram messages
        detector.process_messages(messages, location_extractor)
        # Get current active NOTAMs as GeoJSON
        geojson = detector.get_geojson()
    """

    def __init__(self):
        self.active_notams = {}  # key -> notam data
        self.cancelled = set()   # keys that have been cancelled

    def _make_key(self, location_name, notam_type):
        """Create a unique key for deduplication."""
        return f"{location_name.lower().strip()}|{notam_type}"

    def process_messages(self, messages, extract_location_fn):
        """
        Process raw telegram messages and extract NOTAM events.
        
        Args:
            messages: list of {"text", "date", "channel", "id"} dicts
            extract_location_fn: function(text) -> (name, [lon,lat], theater)
        """
        now = datetime.now(timezone.utc)

        for msg in messages:
            text = msg["text"]

            # Check cancellations first (before NOTAM filter, since
            # "reopened" messages may not contain NOTAM keywords)
            if is_cancellation(text):
                loc_name, coords, theater = extract_location_fn(text)
                if loc_name:
                    for ntype in NOTAM_TYPES:
                        key = self._make_key(loc_name, ntype)
                        if key in self.active_notams:
                            self.cancelled.add(key)
                            del self.active_notams[key]
                            logger.info(f"NOTAM cancelled: {loc_name} ({ntype})")
                continue

            if not is_notam_related(text):
                continue

            # Extract location
            loc_name, coords, theater = extract_location_fn(text)

            # Also try FIR codes
            if not coords:
                fir_code, fir_data = extract_fir(text)
                if fir_data:
                    coords = [fir_data[0], fir_data[1]]
                    loc_name = fir_data[2]
                    theater = None

            if not coords:
                continue

            notam_type = classify_notam_type(text)
            key = self._make_key(loc_name, notam_type)

            # Skip if previously cancelled
            if key in self.cancelled:
                # But re-add if reported again after cancellation
                self.cancelled.discard(key)

            msg_date = msg["date"]
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)

            # Calculate time string
            delta = now - msg_date
            secs = delta.total_seconds()
            time_str = (f"{int(secs/60)}m ago" if secs < 3600
                       else f"{int(secs/3600)}h ago" if secs < 86400
                       else f"{int(secs/86400)}d ago")

            # Clean summary
            clean = re.sub(r'[^\w\s.,;:!?\-\'\"()/]', '', text)
            clean = re.sub(r'\s+', ' ', clean).strip()
            summary = clean[:200]

            if key in self.active_notams:
                # Update existing — refresh TTL and add source
                existing = self.active_notams[key]
                existing["sources"].add(f"@{msg['channel']}")
                existing["last_reported"] = msg_date.isoformat()
                existing["report_count"] += 1
                # Keep the most recent text
                existing["text"] = text[:120].replace('\n', ' ')
                existing["summary"] = summary
            else:
                # New NOTAM entry
                self.active_notams[key] = {
                    "location": loc_name,
                    "coords": coords,
                    "theater": theater,
                    "notam_type": notam_type,
                    "text": text[:120].replace('\n', ' '),
                    "summary": summary,
                    "time_str": time_str,
                    "first_reported": msg_date.isoformat(),
                    "last_reported": msg_date.isoformat(),
                    "sources": {f"@{msg['channel']}"},
                    "report_count": 1,
                    "url": f"https://t.me/{msg['channel']}/{msg['id']}",
                }
                logger.info(f"NOTAM detected: {loc_name} ({notam_type}) from @{msg['channel']}")

        # Expire old entries
        self._expire_old(now)

    def _expire_old(self, now):
        """Remove NOTAM entries older than their TTL."""
        expired = []
        for key, notam in self.active_notams.items():
            last = datetime.fromisoformat(notam["last_reported"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)

            ttl = CONFIRMED_TTL_HOURS if notam["report_count"] >= 3 else DEFAULT_TTL_HOURS
            if (now - last).total_seconds() > ttl * 3600:
                expired.append(key)

        for key in expired:
            logger.info(f"NOTAM expired: {key}")
            del self.active_notams[key]

    def get_geojson(self):
        """Return active NOTAMs as GeoJSON."""
        features = []

        for key, notam in self.active_notams.items():
            ntype = notam["notam_type"]
            is_gps = ntype == "gps_jamming"
            is_closed = ntype == "airspace_closed"
            is_military = ntype == "military_activity"

            # Determine visual properties
            if is_closed:
                status = "CLOSED"
                color = "#ff3333"
                radius_nm = 200
            elif is_gps:
                status = "GPS INTERFERENCE"
                color = "#ff8c00"
                radius_nm = 150
            elif is_military:
                status = "MILITARY ACTIVE"
                color = "#ffaa00"
                radius_nm = 100
            else:
                status = "RESTRICTED"
                color = "#ff8800"
                radius_nm = 120

            # Confidence based on number of reports
            n = notam["report_count"]
            sources = list(notam["sources"])
            if len(sources) >= 3:
                confidence = "confirmed"
            elif len(sources) >= 2:
                confidence = "corroborated"
            else:
                confidence = "single source"

            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": notam["coords"]},
                "properties": {
                    "name": f"{status}: {notam['location']}",
                    "status": status,
                    "type": "gps_jamming" if is_gps else "restricted_airspace",
                    "notam_type": ntype,
                    "notes": notam["summary"],
                    "radius_nm": radius_nm,
                    "color": color,
                    "text": notam["text"],
                    "time": notam["time_str"],
                    "sources": ", ".join(sources),
                    "report_count": n,
                    "confidence": confidence,
                    "first_reported": notam["first_reported"],
                    "last_reported": notam["last_reported"],
                    "url": notam["url"],
                    "authority": "Telegram OSINT",
                },
            })

        gps_count = sum(1 for f in features if f["properties"]["notam_type"] == "gps_jamming")
        airspace_count = len(features) - gps_count

        return {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "count": len(features),
                "restricted_firs": airspace_count,
                "gps_zones": gps_count,
                "source": "Telegram OSINT (live detection)",
                "updated": datetime.now(timezone.utc).isoformat() + "Z",
            },
        }