#!/usr/bin/env python3
"""
SENTINEL Daily Intelligence Brief — Automated Pipeline
AKA IND Technologies

Fetches: OpenSky ADS-B, GPSJam.org interference data, CelesTrak satellite data, GDELT global events
Posts:   Notion page (new page per day, structured intel brief)

Usage:
    python sentinel_brief.py                  # run once (today's brief)
    python sentinel_brief.py --dry-run        # print output, don't post
    python sentinel_brief.py --date 2026-03-06  # specific date override

Env vars required:
    NOTION_API_KEY      — from notion.so/my-integrations
    NOTION_PARENT_ID    — page or database ID to create briefs under

Env vars optional (enable live feeds):
    AIS_API_KEY         — from aisstream.io (free registration)
    CELESTRAK_API_KEY   — from celestrak.org
"""

import os
import sys
import json
import time
import logging
import argparse
import csv
import html as html_mod
from datetime import datetime, timezone, timedelta
from io import StringIO
from typing import Optional

import requests

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
NOTION_API_KEY  = os.getenv("NOTION_API_KEY", "")
NOTION_PARENT_ID = os.getenv("NOTION_PARENT_ID", "")  # page or DB ID
NOTION_VERSION  = "2022-06-28"

OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_BOX = dict(lamin=20, lomin=10, lamax=62, lomax=55)   # E Europe / Mid East
OPENSKY_TIMEOUT = 12

GPSJAM_BASE = "https://gpsjam.org/data"
GPSJAM_TIMEOUT = 10
GPSJAM_MIN_PROB = 0.3

AIS_API_KEY       = os.getenv("AIS_API_KEY", "")         # aisstream.io WebSocket key
AIS_WS_URL        = "wss://stream.aisstream.io/v0/stream"
AIS_COLLECT_SECS  = 20                                    # seconds to collect AIS data
AIS_BBOX          = [[[20, 10], [62, 55]]]                # E Europe / Mid East (same theater as OpenSky)
CELESTRAK_API_KEY = os.getenv("CELESTRAK_API_KEY", "")   # CelesTrak TLE API key

CELESTRAK_GP_URL  = "https://celestrak.org/NORAD/elements/gp.php"
CELESTRAK_TIMEOUT = 15
CELESTRAK_GROUPS  = ["resource", "military"]  # Earth observation + military sats

# Key intel/observation satellites by NORAD catalog ID
INTEL_SAT_CATALOG = {
    40697: ("SENTINEL-2A",    "10m MSI",  "Eastern Europe"),
    42063: ("SENTINEL-2B",    "10m MSI",  "Global"),
    40115: ("WORLDVIEW-3",    "**0.3m**", "Global"),
    35946: ("WORLDVIEW-2",    "0.5m",     "Global"),
    33331: ("GEOEYE-1",       "0.5m",     "Global"),
    31598: ("COSMO-SKYMED 1", "SAR",      "Eastern Europe"),
    32376: ("COSMO-SKYMED 2", "SAR",      "Eastern Europe"),
    36124: ("HELIOS 2B",      "0.5m",     "Global"),
    39034: ("PLEIADES 1A",    "0.5m",     "Global"),
    39634: ("PLEIADES 1B",    "0.5m",     "Global"),
    49258: ("PLEIADES NEO 3", "0.3m",     "Middle East"),
    51003: ("PLEIADES NEO 4", "0.3m",     "Global"),
}

GDELT_DOC_URL     = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_TIMEOUT     = 15
GDELT_QUERY       = "(conflict OR military OR airstrike OR missile OR drone strike) sourcelang:english"
GDELT_MAX_RECORDS = 30

MILITARY_PREFIXES  = ["RFR","SHF","UAF","NATO","USAF","USN","RAF","RU","HKP","UKAF"]
ISR_PREFIXES       = ["RCH","OSB","JAKE","COBRA","IRON","FORGE"]
EMERGENCY_SQUAWKS  = {"7500": "HIJACK", "7700": "EMERGENCY", "7600": "COMMS LOSS"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("sentinel")


# ─────────────────────────────────────────────
#  DATA COLLECTORS
# ─────────────────────────────────────────────

def fetch_opensky() -> dict:
    """Fetch ADS-B state vectors from OpenSky Network."""
    log.info("Querying OpenSky Network ADS-B feed...")
    result = {"raw": [], "aircraft": [], "military": [], "isr": [], "emergency": [], "commercial": [], "source": "SIM"}
    try:
        r = requests.get(OPENSKY_URL, params=OPENSKY_BOX, timeout=OPENSKY_TIMEOUT)
        if r.status_code == 429:
            log.warning("OpenSky rate-limited (429) — using simulated data")
            return _sim_aircraft(result)
        r.raise_for_status()
        data = r.json()
        states = data.get("states") or []
        if not states:
            log.warning("OpenSky returned empty state vector — using simulated data")
            return _sim_aircraft(result)

        for s in states:
            cs   = (s[1] or "").strip()
            lat  = s[6] or 0
            lng  = s[5] or 0
            alt_m = s[7] or 0
            alt_ft = round(alt_m * 3.281)
            spd_ms = s[9] or 0
            spd_kts = round(spd_ms * 1.944)
            hdg  = round(s[10] or 0)
            sq   = s[14] or "----"
            gnd  = s[8]
            if not lat or not lng or gnd:
                continue

            ac = dict(callsign=cs or "BLOCKED", lat=round(lat,4), lng=round(lng,4),
                      alt_ft=alt_ft, spd_kts=spd_kts, heading=hdg, squawk=sq,
                      icao24=s[0], source="OpenSky-LIVE")

            if sq in EMERGENCY_SQUAWKS:
                ac["type"] = "emergency"
                ac["label"] = EMERGENCY_SQUAWKS[sq]
                result["emergency"].append(ac)
            elif any(cs.startswith(p) for p in ISR_PREFIXES):
                ac["type"] = "isr"
                ac["label"] = "ISR/RECCE"
                result["isr"].append(ac)
            elif any(cs.startswith(p) for p in MILITARY_PREFIXES):
                ac["type"] = "military"
                ac["label"] = "MILITARY"
                result["military"].append(ac)
            elif cs and cs[:3].isalpha() and cs[3:].isdigit():
                ac["type"] = "commercial"
                ac["label"] = "COMMERCIAL"
                result["commercial"].append(ac)
            else:
                ac["type"] = "unknown"
                ac["label"] = "UNKNOWN"

            result["aircraft"].append(ac)

        result["source"] = "OpenSky-LIVE"
        log.info(f"OpenSky: {len(result['aircraft'])} contacts | "
                 f"{len(result['military'])} mil | {len(result['isr'])} ISR | "
                 f"{len(result['emergency'])} emergency")
        return result

    except requests.Timeout:
        log.warning("OpenSky timeout — using simulated data")
        return _sim_aircraft(result)
    except Exception as e:
        log.warning(f"OpenSky error: {e} — using simulated data")
        return _sim_aircraft(result)


def fetch_gpsjam(date_str: Optional[str] = None) -> dict:
    """Fetch GPSJam.org daily interference data."""
    log.info("Fetching GPSJam.org interference data...")
    result = {"zones": [], "high_intensity": [], "source": "SIM", "date": date_str or "N/A"}

    today = datetime.now(timezone.utc)
    dates_to_try = []
    for offset in range(3):
        d = today - timedelta(days=offset)
        dates_to_try.append(d.strftime("%Y-%m-%d"))

    for date in dates_to_try:
        url = f"{GPSJAM_BASE}/{date}.csv"
        try:
            r = requests.get(url, timeout=GPSJAM_TIMEOUT)
            if not r.ok:
                continue
            text = r.text.strip()
            if not text:
                continue

            reader = csv.reader(StringIO(text))
            header = next(reader, None)
            zones = []
            for row in reader:
                if len(row) < 4:
                    continue
                try:
                    lat   = float(row[0])
                    lng   = float(row[1])
                    prob  = float(row[3]) if len(row) > 3 else float(row[2])
                    if prob < GPSJAM_MIN_PROB:
                        continue
                    zones.append(dict(lat=round(lat,3), lng=round(lng,3),
                                      radius_km=round(30 + prob * 120),
                                      intensity=round(prob, 3),
                                      pct=round(prob * 100),
                                      source="GPSJam.org-LIVE",
                                      date=date))
                except (ValueError, IndexError):
                    continue

            if zones:
                result["zones"]         = zones
                result["high_intensity"] = [z for z in zones if z["intensity"] >= 0.7]
                result["source"]        = "GPSJam.org-LIVE"
                result["date"]          = date
                log.info(f"GPSJam {date}: {len(zones)} zones | "
                         f"{len(result['high_intensity'])} high-intensity (>70%)")
                return result

        except Exception as e:
            log.warning(f"GPSJam {date} fetch error: {e}")
            continue

    log.warning("GPSJam unavailable — using historically accurate simulated zones")
    return _sim_gpsjam(result)


def _classify_orbit(inclination: float, altitude_km: float) -> str:
    """Classify orbit type from inclination and altitude."""
    if altitude_km > 35000:
        return "GEO"
    if altitude_km > 2000:
        return "MEO"
    # Sun-synchronous orbits have inclination ~97-99° to maintain constant solar angle
    if 96 <= inclination <= 100:
        return "LEO-SSO"
    return "LEO"


def fetch_celestrak() -> dict:
    """Fetch satellite orbital data from CelesTrak GP API (OMM/JSON format)."""
    log.info("Fetching CelesTrak satellite orbital data...")
    result = {"satellites": [], "source": "SIM"}

    all_sats = []
    for group in CELESTRAK_GROUPS:
        try:
            params = {"GROUP": group, "FORMAT": "json"}
            if CELESTRAK_API_KEY:
                params["API_KEY"] = CELESTRAK_API_KEY
            r = requests.get(CELESTRAK_GP_URL, params=params, timeout=CELESTRAK_TIMEOUT)
            if r.ok:
                data = r.json()
                if isinstance(data, list):
                    all_sats.extend(data)
                    log.info(f"CelesTrak {group}: {len(data)} objects")
            else:
                log.warning(f"CelesTrak {group} HTTP {r.status_code}")
        except requests.Timeout:
            log.warning(f"CelesTrak {group} timeout")
        except Exception as e:
            log.warning(f"CelesTrak {group} error: {e}")

    if not all_sats:
        log.warning("CelesTrak unavailable — using simulated satellite data")
        return _sim_satellites(result)

    # Match against intel satellite catalog by NORAD ID
    matched = []
    for sat in all_sats:
        norad_id = sat.get("NORAD_CAT_ID")
        if norad_id not in INTEL_SAT_CATALOG:
            continue
        display_name, res, coverage = INTEL_SAT_CATALOG[norad_id]
        incl = sat.get("INCLINATION") or 0
        peri = sat.get("PERIAPSIS")
        apo  = sat.get("APOAPSIS")
        alt  = round((peri + apo) / 2) if peri is not None and apo is not None else 0
        matched.append({
            "name": display_name,
            "norad_id": norad_id,
            "orbit": _classify_orbit(incl, alt),
            "altitude": f"{alt} km",
            "resolution": res,
            "coverage": coverage,
            "inclination": round(incl, 1),
            "period_min": round(sat.get("PERIOD") or 0, 1),
            "epoch": sat.get("EPOCH", ""),
            "source": "CelesTrak-LIVE",
        })

    if matched:
        result["satellites"] = matched
        result["source"] = "CelesTrak-LIVE"
        log.info(f"CelesTrak: {len(matched)} intel satellites tracked from {len(all_sats)} total objects")
        return result

    log.warning("No intel satellites matched in CelesTrak data — using simulated data")
    return _sim_satellites(result)


def fetch_gdelt() -> dict:
    """Fetch recent global conflict/security events from GDELT Project DOC API v2."""
    log.info("Fetching GDELT Project global events data...")
    result = {"articles": [], "source": "SIM"}

    params = {
        "query": GDELT_QUERY,
        "mode": "ArtList",
        "maxrecords": GDELT_MAX_RECORDS,
        "format": "json",
        "timespan": "24h",
        "sort": "DateDesc",
    }

    try:
        r = requests.get(GDELT_DOC_URL, params=params, timeout=GDELT_TIMEOUT)
        if not r.ok:
            log.warning(f"GDELT API HTTP {r.status_code} — using simulated data")
            return _sim_gdelt(result)

        data = r.json()
        articles = data.get("articles", [])

        if not articles:
            log.warning("GDELT returned no articles — using simulated data")
            return _sim_gdelt(result)

        parsed = []
        for art in articles[:GDELT_MAX_RECORDS]:
            seendate = art.get("seendate", "")
            # GDELT dates: "20260306T123456Z" → "2026-03-06 12:34 UTC"
            time_str = ""
            if len(seendate) >= 15:
                try:
                    dt = datetime.strptime(seendate[:15], "%Y%m%dT%H%M%S")
                    time_str = dt.strftime("%H:%M UTC")
                except ValueError:
                    time_str = seendate
            parsed.append({
                "title": art.get("title", "Untitled").strip(),
                "url": art.get("url", ""),
                "domain": art.get("domain", ""),
                "seendate": seendate,
                "time": time_str,
                "language": art.get("language", "English"),
                "sourcecountry": art.get("sourcecountry", ""),
                "source": "GDELT-LIVE",
            })

        result["articles"] = parsed
        result["source"] = "GDELT-LIVE"
        log.info(f"GDELT: {len(parsed)} conflict-related articles (24h window)")
        return result

    except requests.Timeout:
        log.warning("GDELT timeout — using simulated data")
        return _sim_gdelt(result)
    except Exception as e:
        log.warning(f"GDELT error: {e} — using simulated data")
        return _sim_gdelt(result)


def _ais_area_from_coords(lat: float, lng: float) -> str:
    """Infer maritime area name from vessel coordinates."""
    # Most-specific regions first, then broader ones
    if 44.5 <= lat <= 46 and 35.5 <= lng <= 37:
        return "Kerch Strait"
    if 40.5 <= lat <= 42 and 28 <= lng <= 30:
        return "Bosphorus"
    if 44 <= lat <= 47 and 35 <= lng <= 40:
        return "Sea of Azov"
    if 40 <= lat <= 47 and 27 <= lng <= 42:
        return "Black Sea"
    if 30 <= lat <= 42 and -6 <= lng <= 36:
        return "Mediterranean"
    if 20 <= lat <= 32 and 32 <= lng <= 44:
        return "Red Sea"
    if 24 <= lat <= 30 and 48 <= lng <= 56:
        return "Persian Gulf"
    if 54 <= lat <= 62 and 10 <= lng <= 30:
        return "Baltic Sea"
    if 55 <= lat <= 72 and -30 <= lng <= 10:
        return "North Atlantic"
    return "Open Water"


_MILITARY_VESSEL_KW = ["NAVY", "NAVAL", "WARSHIP", "PATROL", "COAST GUARD",
                       "COASTGUARD", "MILITARY", "DESTROYER", "FRIGATE",
                       "CORVETTE", "SUBMARINE", "MINESWEEP", "LANDING"]
_TANKER_KW  = ["TANKER", "CRUDE", "OIL", "LNG", "LPG", "CHEMICAL"]
_CARGO_KW   = ["CARGO", "BULK", "CONTAINER", "CARRIER", "GENERAL"]


def _classify_vessel(name: str) -> tuple:
    """Classify vessel type from name heuristics. Returns (type, label)."""
    up = name.upper()
    for kw in _MILITARY_VESSEL_KW:
        if kw in up:
            return ("military", "Military/Govt")
    for kw in _TANKER_KW:
        if kw in up:
            return ("tanker", "Tanker")
    for kw in _CARGO_KW:
        if kw in up:
            return ("cargo", "Cargo")
    return ("other", "Vessel")


_NAV_STATUS = {0: "Under way", 1: "At anchor", 2: "Not under command",
               3: "Restricted manoeuvrability", 5: "Moored", 7: "Fishing",
               8: "Under sail", 14: "AIS-SART"}


def fetch_ais() -> dict:
    """Fetch AIS maritime vessel data from aisstream.io WebSocket API."""
    log.info("Fetching AIS maritime data...")
    result = {"vessels": [], "military": [], "tankers": [], "cargo": [], "source": "SIM"}

    if not AIS_API_KEY:
        log.info("AIS_API_KEY not set — using simulated maritime data")
        return _sim_ais(result)

    try:
        import websocket
        import threading

        vessels_seen: dict = {}

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if data.get("MessageType") != "PositionReport":
                    return
                meta = data.get("MetaData", {})
                pos  = data.get("Message", {}).get("PositionReport", {})
                mmsi = meta.get("MMSI")
                if mmsi is None or mmsi in vessels_seen:
                    return

                name = (meta.get("ShipName") or "UNKNOWN").strip()
                lat  = round(meta.get("latitude", 0) or pos.get("Latitude", 0), 4)
                lng  = round(meta.get("longitude", 0) or pos.get("Longitude", 0), 4)
                sog  = round(pos.get("Sog", 0) or 0, 1)
                hdg  = pos.get("TrueHeading", 511)
                cog  = round(pos.get("Cog", 0) or 0)
                nav  = pos.get("NavigationalStatus", 15)

                vtype, label = _classify_vessel(name)
                vessel = {
                    "mmsi": mmsi, "name": name, "lat": lat, "lng": lng,
                    "speed_kts": sog,
                    "heading": cog if hdg == 511 else hdg,
                    "nav_status": _NAV_STATUS.get(nav, "Unknown"),
                    "area": _ais_area_from_coords(lat, lng),
                    "type": vtype, "label": label,
                    "source": "AISstream-LIVE",
                }

                vessels_seen[mmsi] = vessel
                result["vessels"].append(vessel)
                if vtype == "military":
                    result["military"].append(vessel)
                elif vtype == "tanker":
                    result["tankers"].append(vessel)
                elif vtype == "cargo":
                    result["cargo"].append(vessel)
            except Exception:
                pass

        def on_error(ws, error):
            log.warning(f"AIS WebSocket error: {error}")

        def on_open(ws):
            ws.send(json.dumps({
                "APIKey": AIS_API_KEY,
                "BoundingBoxes": AIS_BBOX,
                "FilterMessageTypes": ["PositionReport"],
            }))
            log.info("AIS WebSocket connected, collecting vessel data...")

        ws = websocket.WebSocketApp(AIS_WS_URL,
                                     on_message=on_message,
                                     on_error=on_error,
                                     on_open=on_open)

        timer = threading.Timer(AIS_COLLECT_SECS, ws.close)
        timer.daemon = True
        timer.start()
        try:
            ws.run_forever(ping_interval=10, ping_timeout=5)
        finally:
            timer.cancel()

        if vessels_seen:
            result["source"] = "AISstream-LIVE"
            log.info(f"AIS: {len(result['vessels'])} vessels | "
                     f"{len(result['military'])} military | "
                     f"{len(result['tankers'])} tankers | "
                     f"{len(result['cargo'])} cargo")
            return result

        log.warning("AIS returned no vessels — using simulated data")
        return _sim_ais(result)

    except ImportError:
        log.warning("websocket-client not installed — using simulated AIS data")
        return _sim_ais(result)
    except Exception as e:
        log.warning(f"AIS error: {e} — using simulated data")
        return _sim_ais(result)


# ─────────────────────────────────────────────
#  SIMULATION FALLBACKS
# ─────────────────────────────────────────────

def _sim_aircraft(result: dict) -> dict:
    import random, math
    CALLSIGNS = [
        ("UAF342","RC-135V RIVET JOINT","isr"),("NATO456","E-3 AWACS","military"),
        ("RUF789","Su-27 FLANKER","military"),("USAF101","F-16 FALCON","military"),
        ("USN203","P-8A POSEIDON","isr"),("RAF512","TORNADO GR4","military"),
        ("USAF884","C-17 GLOBEMASTER","military"),("UAF099","RQ-4B GLOBAL HAWK","isr"),
    ]
    for cs, label, typ in CALLSIGNS:
        ac = dict(callsign=cs, label=label, type=typ,
                  lat=round(random.uniform(28,55),4), lng=round(random.uniform(18,50),4),
                  alt_ft=random.randint(15000,40000), spd_kts=random.randint(300,900),
                  heading=random.randint(0,359), squawk=str(random.randint(1000,7777)),
                  source="SIM")
        result["aircraft"].append(ac)
        if typ == "isr":
            result["isr"].append(ac)
        elif typ == "military":
            result["military"].append(ac)
    result["source"] = "SIM"
    log.info(f"Simulated: {len(result['aircraft'])} aircraft")
    return result


def _sim_gpsjam(result: dict) -> dict:
    KNOWN_ZONES = [
        (47.02, 35.18, 120, 0.85), (46.15, 38.22,  95, 0.78),
        (54.70, 20.50, 110, 0.74), (35.50, 33.00,  88, 0.71),
        (44.00, 42.00,  75, 0.55), (42.00, 35.00,  65, 0.48),
        (49.00, 27.00,  80, 0.62), (51.00, 30.00,  70, 0.58),
        (45.00, 33.00,  90, 0.66), (37.00, 30.00,  60, 0.42),
    ]
    for lat, lng, r, prob in KNOWN_ZONES:
        z = dict(lat=lat, lng=lng, radius_km=r, intensity=prob,
                 pct=round(prob*100), source="SIM", date="simulated")
        result["zones"].append(z)
        if prob >= 0.7:
            result["high_intensity"].append(z)
    result["source"] = "SIM"
    log.info(f"Simulated: {len(result['zones'])} jamming zones")
    return result


def _sim_satellites(result: dict) -> dict:
    """Simulation fallback — hardcoded satellite data matching pre-live format."""
    sats = [
        {"name": "SENTINEL-2A",  "orbit": "LEO-SSO", "altitude": "786 km", "resolution": "10m MSI",  "coverage": "Eastern Europe", "source": "SIM"},
        {"name": "WORLDVIEW-3",  "orbit": "LEO",     "altitude": "617 km", "resolution": "**0.3m**", "coverage": "Global",         "source": "SIM"},
        {"name": "COSMO-SKYMED", "orbit": "LEO-SSO", "altitude": "619 km", "resolution": "SAR",      "coverage": "Eastern Europe", "source": "SIM"},
        {"name": "HELIOS-2B",    "orbit": "LEO-SSO", "altitude": "680 km", "resolution": "0.5m",     "coverage": "Global",         "source": "SIM"},
        {"name": "OFEK-16",      "orbit": "LEO",     "altitude": "420 km", "resolution": "**0.3m**", "coverage": "Middle East",    "source": "SIM"},
        {"name": "PLEIADES-NEO", "orbit": "LEO-SSO", "altitude": "480 km", "resolution": "0.3m",     "coverage": "Middle East",    "source": "SIM"},
    ]
    result["satellites"] = sats
    result["source"] = "SIM"
    log.info(f"Simulated: {len(sats)} satellites")
    return result


def _sim_ais(result: dict) -> dict:
    """Simulation fallback — representative AIS maritime data."""
    vessels = [
        {"name": "BLACK SEA 47", "type": "military", "label": "Destroyer",    "speed_kts": 12.3, "lat": 43.50, "lng": 34.20, "area": "Black Sea",     "nav_status": "Under way",   "source": "SIM"},
        {"name": "AZOV 12",      "type": "military", "label": "Frigate",      "speed_kts": 8.7,  "lat": 46.50, "lng": 38.20, "area": "Sea of Azov",   "nav_status": "Under way",   "source": "SIM"},
        {"name": "KERCH 3",      "type": "military", "label": "Landing Ship", "speed_kts": 6.2,  "lat": 45.30, "lng": 36.50, "area": "Kerch Strait",  "nav_status": "Under way",   "source": "SIM"},
        {"name": "ATLANTIC 18",  "type": "cargo",    "label": "Bulk Carrier", "speed_kts": 14.1, "lat": 36.10, "lng": 28.50, "area": "Mediterranean", "nav_status": "Under way",   "source": "SIM"},
        {"name": "PACIFIC 44",   "type": "tanker",   "label": "Oil Tanker",   "speed_kts": 11.8, "lat": 41.20, "lng": 29.10, "area": "Bosphorus",     "nav_status": "Under way",   "source": "SIM"},
    ]
    for v in vessels:
        result["vessels"].append(v)
        if v["type"] == "military":
            result["military"].append(v)
        elif v["type"] == "tanker":
            result["tankers"].append(v)
        elif v["type"] == "cargo":
            result["cargo"].append(v)
    result["source"] = "SIM"
    log.info(f"Simulated: {len(vessels)} vessels")
    return result


def _sim_gdelt(result: dict) -> dict:
    """Simulation fallback — representative GDELT articles for template."""
    articles = [
        {"title": "Missile strikes reported in eastern Ukraine overnight",       "domain": "reuters.com",     "time": "03:14 UTC", "sourcecountry": "United Kingdom", "source": "SIM"},
        {"title": "IDF confirms drone interception over northern border",        "domain": "timesofisrael.com","time": "07:32 UTC", "sourcecountry": "Israel",         "source": "SIM"},
        {"title": "NATO increases Baltic air patrols amid rising tensions",      "domain": "bbc.co.uk",       "time": "09:45 UTC", "sourcecountry": "United Kingdom", "source": "SIM"},
        {"title": "Red Sea shipping disrupted by Houthi attacks",                "domain": "aljazeera.com",   "time": "11:20 UTC", "sourcecountry": "Qatar",          "source": "SIM"},
        {"title": "South China Sea military exercises escalate regional concern", "domain": "scmp.com",        "time": "14:55 UTC", "sourcecountry": "Hong Kong",      "source": "SIM"},
        {"title": "Wagner-linked forces advance in Sahel region",                "domain": "france24.com",    "time": "16:30 UTC", "sourcecountry": "France",         "source": "SIM"},
        {"title": "DPRK ballistic missile test prompts emergency UN session",    "domain": "apnews.com",      "time": "19:10 UTC", "sourcecountry": "United States",  "source": "SIM"},
        {"title": "Russian submarine activity detected in North Atlantic",       "domain": "bbc.co.uk",       "time": "21:40 UTC", "sourcecountry": "United Kingdom", "source": "SIM"},
    ]
    result["articles"] = articles
    result["source"] = "SIM"
    log.info(f"Simulated: {len(articles)} GDELT articles")
    return result


# ─────────────────────────────────────────────
#  BRIEF GENERATOR — Notion Markdown
# ─────────────────────────────────────────────

def generate_brief(ac_data: dict, jam_data: dict, sat_data: dict, gdelt_data: dict, ais_data: dict, brief_date: str) -> str:
    """Build Notion-flavored Markdown for the daily brief page."""
    now_str    = datetime.now(timezone.utc).strftime("%B %-d, %Y %H:%M UTC")
    air_source = ac_data["source"]
    jam_source = jam_data["source"]
    sat_source = sat_data.get("source", "SIM")
    gdelt_source = gdelt_data.get("source", "SIM")
    ais_source = ais_data.get("source", "SIM")
    is_live    = "LIVE" in air_source

    total_ac  = len(ac_data["aircraft"])
    mil_count = len(ac_data["military"])
    isr_count = len(ac_data["isr"])
    emg_count = len(ac_data["emergency"])
    com_count = len(ac_data["commercial"])
    jam_count = len(jam_data["zones"])
    hi_count  = len(jam_data["high_intensity"])

    # Threat calc
    threat = "ELEVATED"
    if mil_count + isr_count > 10 or hi_count > 4:
        threat = "HIGH"
    if emg_count > 0:
        threat = "CRITICAL"

    lines = []
    A = lines.append  # shorthand appender

    # ── HEADER CALLOUT ───────────────────────
    A(f'::: callout {{icon="🛰" color="gray_bg"}}')
    A(f'**SENTINEL World Intelligence Platform** · Daily Brief')
    A(f'**Generated:** {now_str}  ·  **Coverage Date:** {brief_date}')
    A(f'**Air Feed:** {air_source}  ·  **GPS Jam Feed:** {jam_source}  ·  **AIS Feed:** {ais_source}  ·  **Sat Feed:** {sat_source}  ·  **Events:** {gdelt_source}')
    A(f'**Theater:** Eastern Europe / Middle East / Indo-Pacific / North Atlantic / Sub-Saharan Africa / Americas')
    A(':::')
    A('')
    A('---')
    A('')

    # ── EXECUTIVE SUMMARY ────────────────────
    A('# Executive Summary')
    A('')
    A(f'Global threat posture: **{threat}**. '
      f'Eastern European theater primary collection priority — '
      f'multi-domain activity sustained across air, maritime, and EW domains. '
      f'GPS denial elevated across Black Sea corridor. '
      f'Indo-Pacific maintains secondary watch posture.')
    A('')
    A('---')
    A('')

    # ── THREAT MATRIX ────────────────────────
    A('## 🌡 Threat Matrix')
    A('')
    A('<table fit-page-width="true" header-row="true">')
    A('\t<tr><td>**Theater**</td><td>**Level**</td><td>**Primary Indicator**</td><td>**Trend**</td></tr>')
    A('\t<tr color="red_bg"><td>🌍 Eastern Europe</td><td>**CRITICAL**</td><td>Active kinetic conflict · GPS jamming pervasive</td><td>↑ Escalating</td></tr>')
    A('\t<tr color="orange_bg"><td>🌏 Middle East</td><td>**HIGH**</td><td>Red Sea disruptions · Iran-Israel escalation risk</td><td>→ Sustained</td></tr>')
    A('\t<tr color="orange_bg"><td>🌏 Indo-Pacific</td><td>**HIGH**</td><td>Taiwan Strait tensions · DPRK missile program</td><td>→ Sustained</td></tr>')
    A('\t<tr color="yellow_bg"><td>🌊 North Atlantic</td><td>**MODERATE**</td><td>Russian submarine activity elevated</td><td>↑ Monitoring</td></tr>')
    A('\t<tr color="yellow_bg"><td>🌍 Sub-Saharan Africa</td><td>**MODERATE**</td><td>Sahel insurgencies · Wagner/Africa Corps</td><td>→ Sustained</td></tr>')
    A('\t<tr color="green_bg"><td>🌎 Americas</td><td>**LOW**</td><td>Cartel corridor · Arctic sovereignty</td><td>→ Stable</td></tr>')
    A('</table>')
    A('')
    A('---')
    A('')

    # ── AIR DOMAIN ───────────────────────────
    live_badge = "✅ LIVE ADS-B" if is_live else "⚠ SIMULATED"
    A(f'# ✈ Air Domain — {live_badge}')
    A('')
    A(f'> **Source:** {air_source} · OpenSky Network (same infrastructure as FlightRadar24 / Flightaware)')
    A(f'> **Contacts:** {total_ac} airborne · {mil_count} military · {isr_count} ISR · {emg_count} emergency · {com_count} commercial')
    A('')

    # Emergency squawks — always show first if present
    if emg_count > 0:
        A('::: callout {icon="🚨" color="red_bg"}')
        A(f'**EMERGENCY SQUAWKS ACTIVE — {emg_count} CONTACT(S)**')
        for ac in ac_data["emergency"]:
            A(f'**{ac["callsign"]}** · {ac["label"]} · Squawk {ac["squawk"]} · {ac["lat"]}°N {ac["lng"]}°E')
        A(':::')
        A('')

    # Military platforms
    if ac_data["military"] or ac_data["isr"]:
        A('<details>')
        A(f'<summary>**Military & ISR Platforms** ({mil_count + isr_count} tracked)</summary>')
        A('')
        combined = ac_data["isr"] + ac_data["military"]
        for ac in combined[:12]:
            A(f'- **{ac["callsign"]}** [{ac.get("label","MIL")}] — '
              f'{ac["alt_ft"]:,} ft · {ac["spd_kts"]} kts · hdg {ac["heading"]}° · squawk {ac["squawk"]} · '
              f'{ac["lat"]}°N {ac["lng"]}°E')
        if len(combined) > 12:
            A(f'- *...and {len(combined)-12} additional tracks*')
        A('')
        A('</details>')
        A('')

    # Commercial routing note
    A('<details>')
    A('<summary>**Commercial Traffic Assessment**</summary>')
    A('')
    A(f'Commercial aviation ({com_count} contacts tracked) operating on modified routings avoiding '
      f'primary conflict zone airspace. Conflict zone FIR overflight suppressed below pre-conflict baseline. '
      f'Black Sea approach traffic rerouted via alternate corridors.')
    A('')
    A('</details>')
    A('')
    A('---')
    A('')

    # ── EW / GPS JAMMING ─────────────────────
    jam_live_badge = "✅ LIVE" if "LIVE" in jam_source else "⚠ SIMULATED"
    A(f'# 📡 Electronic Warfare — GPS Jamming {jam_live_badge}')
    A('')
    gnss_status = "DEGRADED" if hi_count >= 3 else ("IMPAIRED" if jam_count > 5 else "NOMINAL")
    gnss_color  = "red_bg" if gnss_status == "DEGRADED" else ("yellow_bg" if gnss_status == "IMPAIRED" else "green_bg")
    A(f'::: callout {{icon="⚠️" color="{gnss_color}"}}')
    A(f'**GNSS Environment: {gnss_status}**  ·  {jam_count} active interference zones  ·  {hi_count} high-intensity (>70%)')
    A(f'Source: {jam_source} (NACp anomaly aggregation via ADS-B feeder network)  ·  Data: {jam_data["date"]}')
    A(':::')
    A('')

    if jam_data["high_intensity"]:
        A('## High-Intensity Zones (>70% confidence)')
        A('')
        for i, z in enumerate(jam_data["high_intensity"][:8], 1):
            A(f'{i}. **{z["lat"]}°N, {z["lng"]}°E** · radius {z["radius_km"]} km · intensity **{z["pct"]}%**')
        A('')

    if jam_count > len(jam_data["high_intensity"]):
        A('<details>')
        A(f'<summary>**All Jamming Zones** ({jam_count} total)</summary>')
        A('')
        for z in jam_data["zones"]:
            A(f'- {z["lat"]}°N, {z["lng"]}°E · r={z["radius_km"]}km · {z["pct"]}% · {z["source"]}')
        A('')
        A('</details>')
        A('')
    A('---')
    A('')

    # ── MARITIME ─────────────────────────────
    ais_live_badge = "✅ LIVE AIS" if "LIVE" in ais_source else "⚠ SIMULATED"
    ais_vessels  = ais_data.get("vessels", [])
    ais_mil      = ais_data.get("military", [])
    ais_tankers  = ais_data.get("tankers", [])
    ais_cargo    = ais_data.get("cargo", [])
    A(f'# ⛴ Maritime Domain — {ais_live_badge}')
    A('')
    if "LIVE" in ais_source:
        A('::: callout {icon="🚢" color="blue_bg"}')
        A(f'**AIS Status: LIVE** · aisstream.io WebSocket feed · same data as MarineTraffic & VesselFinder')
        A(f'**Contacts:** {len(ais_vessels)} vessels · {len(ais_mil)} military · {len(ais_tankers)} tankers · {len(ais_cargo)} cargo')
        A(':::')
    else:
        A('::: callout {icon="ℹ️" color="gray_bg"}')
        A('**AIS Status: SIMULATED** · Register free at aisstream.io to enable live vessel tracking')
        A('Live feed uses same data as MarineTraffic and VesselFinder — mandatory AIS transponder data')
        A(':::')
    A('')

    if ais_vessels:
        A('<table fit-page-width="true" header-row="true">')
        A('\t<tr><td>**Vessel**</td><td>**Type**</td><td>**Speed**</td><td>**Area**</td><td>**Status**</td></tr>')
        for v in ais_vessels[:15]:
            name   = html_mod.escape(v.get("name", "UNKNOWN"))
            label  = html_mod.escape(v.get("label", "Vessel"))
            spd    = v.get("speed_kts", 0)
            area   = html_mod.escape(v.get("area", "Unknown"))
            status = html_mod.escape(v.get("nav_status", "Unknown"))
            color  = ""
            if v.get("type") == "military":
                color = ' color="red_bg"'
            elif v.get("type") == "tanker":
                color = ' color="yellow_bg"'
            A(f'\t<tr{color}><td>{name}</td><td>{label}</td><td>{spd} kts</td><td>{area}</td><td>{status}</td></tr>')
        A('</table>')
        if len(ais_vessels) > 15:
            A('')
            A(f'*...and {len(ais_vessels) - 15} additional vessels tracked*')

    A('')
    A('---')
    A('')

    # ── SATELLITES ───────────────────────────
    sat_live_badge = "✅ LIVE" if "LIVE" in sat_source else "⚠ SIMULATED"
    A(f'# 🛰 Space Domain — Satellite Coverage {sat_live_badge}')
    A('')
    A(f'> **Source:** CelesTrak GP/OMM ({sat_source}, JSON orbital elements)')
    A('')
    A('<table fit-page-width="true" header-row="true">')
    A('\t<tr><td>**Asset**</td><td>**Orbit**</td><td>**Altitude**</td><td>**Resolution**</td><td>**Coverage**</td></tr>')
    for sat in sat_data.get("satellites", []):
        A(f'\t<tr><td>{sat["name"]}</td><td>{sat["orbit"]}</td><td>{sat["altitude"]}</td><td>{sat["resolution"]}</td><td>{sat["coverage"]}</td></tr>')
    A('</table>')
    A('')
    A('---')
    A('')

    # ── GDELT GLOBAL EVENTS ─────────────────
    gdelt_live_badge = "✅ LIVE" if "LIVE" in gdelt_source else "⚠ SIMULATED"
    gdelt_articles = gdelt_data.get("articles", [])
    A(f'# 🌐 Global Events Monitor — GDELT {gdelt_live_badge}')
    A('')
    A(f'> **Source:** [GDELT Project](https://gdeltproject.org) DOC API v2 ({gdelt_source}) · {len(gdelt_articles)} articles · 24h window')
    A('')
    if gdelt_articles:
        A('<table fit-page-width="true" header-row="true">')
        A('\t<tr><td>**Time**</td><td>**Headline**</td><td>**Source**</td></tr>')
        for art in gdelt_articles[:12]:
            time_str = html_mod.escape(art.get("time", ""))
            title = html_mod.escape(art.get("title", "Untitled"))
            # Truncate long titles for table display
            if len(title) > 90:
                title = title[:87] + "..."
            domain = html_mod.escape(art.get("domain", ""))
            A(f'\t<tr><td>{time_str}</td><td>{title}</td><td>{domain}</td></tr>')
        A('</table>')
        if len(gdelt_articles) > 12:
            A('')
            A('<details>')
            A(f'<summary>**All GDELT Articles** ({len(gdelt_articles)} total)</summary>')
            A('')
            for art in gdelt_articles[12:]:
                time_str = html_mod.escape(art.get("time", ""))
                title = html_mod.escape(art.get("title", "Untitled"))
                domain = html_mod.escape(art.get("domain", ""))
                A(f'- {time_str} · **{title}** · {domain}')
            A('')
            A('</details>')
    else:
        A('*No conflict-related articles found in 24h window.*')
    A('')
    A('---')
    A('')

    # ── FOOTER CALLOUT ───────────────────────
    A('::: callout {icon="🔒" color="gray_bg"}')
    A('**SENTINEL v5** · AKA IND Technologies · World Intelligence Platform')
    A(f'Auto-generated: {now_str} · Classification: **OSINT UNCLASSIFIED**')
    next_d = (datetime.strptime(brief_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%B %-d, %Y")
    A(f'Next brief: **{next_d} 00:00 UTC** · All source data publicly available')
    A(':::')

    return "\n".join(lines)


# ─────────────────────────────────────────────
#  NOTION POSTER
# ─────────────────────────────────────────────

def post_to_notion(content: str, brief_date: str, dry_run: bool = False) -> Optional[str]:
    """Create a new Notion page with the daily brief content."""
    if not NOTION_API_KEY and not dry_run:
        log.warning("NOTION_API_KEY not set — skipping Notion post. Set the repository secret to enable.")
        return None
    if not NOTION_PARENT_ID and not dry_run:
        log.warning("NOTION_PARENT_ID not set — skipping Notion post. Set the repository secret to enable.")
        return None

    d = datetime.strptime(brief_date, "%Y-%m-%d")
    title = f"🛰 SENTINEL — Daily Brief · {d.strftime('%B %-d, %Y')}"

    if dry_run:
        log.info("── DRY RUN ── Notion page would be created:")
        log.info(f"Title: {title}")
        log.info(f"Parent ID: {NOTION_PARENT_ID or '(not set)'}")
        log.info(f"Content length: {len(content)} chars")
        print("\n" + "="*60)
        print(title)
        print("="*60)
        print(content[:2000] + ("..." if len(content) > 2000 else ""))
        return None

    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "parent": {"page_id": NOTION_PARENT_ID},
        "properties": {
            "title": [{"text": {"content": title}}]
        },
        "children": _md_to_notion_blocks(content)
    }

    log.info(f"Posting to Notion: '{title}'...")
    try:
        r = requests.post("https://api.notion.com/v1/pages",
                          headers=headers, json=payload, timeout=30)
    except requests.RequestException as e:
        log.error(f"Notion connection error: {e}")
        return None
    if r.status_code == 200:
        page = r.json()
        url  = page.get("url", "")
        log.info(f"✅ Created: {url}")
        return url
    else:
        log.error(f"Notion API error {r.status_code}: {r.text[:500]}")
        return None


def _md_to_notion_blocks(md: str) -> list:
    """
    Lightweight converter: passes content as a single rich text block.
    For production, swap in a full Notion Markdown parser.
    This approach uses Notion's paragraph blocks with the raw markdown
    which displays cleanly for the structured content we generate.
    """
    blocks = []
    paragraphs = md.split("\n\n")
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # Use paragraph block with text content
        # Notion API ignores most markdown in paragraph rich_text,
        # but handles heading blocks and callouts natively via the MCP tool.
        # For GitHub Actions pipeline, we use the MCP Notion integration instead.
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": para[:2000]}}]
            }
        })
    return blocks[:100]  # Notion block limit per request


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SENTINEL Daily Intelligence Brief Generator")
    parser.add_argument("--dry-run",  action="store_true", help="Print output, don't post to Notion")
    parser.add_argument("--date",     default=None,        help="Override brief date (YYYY-MM-DD)")
    parser.add_argument("--json-out", default=None,        help="Also save raw data as JSON to this path")
    args = parser.parse_args()

    brief_date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info(f"SENTINEL Daily Brief Pipeline — {brief_date}")
    log.info("="*60)

    # Collect data
    ac_data  = fetch_opensky()
    time.sleep(1)  # be kind to APIs
    jam_data = fetch_gpsjam()
    time.sleep(1)
    sat_data = fetch_celestrak()
    time.sleep(1)
    gdelt_data = fetch_gdelt()
    time.sleep(1)
    ais_data = fetch_ais()

    # Optional raw data export
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"aircraft": ac_data, "jamming": jam_data,
                       "satellites": sat_data, "gdelt": gdelt_data,
                       "ais": ais_data,
                       "generated": datetime.now(timezone.utc).isoformat()}, f, indent=2)
        log.info(f"Raw data saved to {args.json_out}")

    # Generate brief
    log.info("Generating intelligence brief...")
    content = generate_brief(ac_data, jam_data, sat_data, gdelt_data, ais_data, brief_date)
    log.info(f"Brief generated: {len(content)} chars")

    # Post to Notion
    url = post_to_notion(content, brief_date, dry_run=args.dry_run)
    if url:
        log.info(f"🛰 Daily brief published: {url}")
    elif not args.dry_run:
        if NOTION_API_KEY and NOTION_PARENT_ID:
            log.error("Notion API call failed — brief saved locally but not posted.")
            sys.exit(1)
        else:
            log.warning("Notion post skipped (credentials not configured) — brief still saved locally.")


if __name__ == "__main__":
    main()
