#!/usr/bin/env python3
"""Geocode TRIMARC Jefferson County incidents by interstate mile marker.

Runs after scrape.py. It:
  1. reads data/trimarc.csv and keeps the latest version of each incident,
  2. filters to Jefferson County interstate notices that carry a mile marker,
  3. parses route + mile marker from the title,
  4. locates a lat/lon by linear-referencing the mile marker against KYTC's
     measured route layer (AllRds_M), and
  5. writes the published map to docs/index.html plus docs/trimarc_geo.geojson.

The docs/ folder is what GitHub Pages serves, so committing it publishes the
map. Output is a pure function of the committed data (no wall-clock values), so
re-running on unchanged data produces byte-identical files and no noise commit.

Stdlib only. The one network dependency is KYTC's public ArcGIS REST service
(https://maps.kytc.ky.gov, CC0 public-domain data), which returns route
geometry already reprojected to WGS84 (lon/lat) with an M (mile) value on every
vertex -- so no local reprojection or linear-referencing library is needed.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "trimarc.csv"
DOCS_DIR = ROOT / "docs"
GEOJSON_PATH = DOCS_DIR / "trimarc_geo.geojson"
MAP_PATH = DOCS_DIR / "index.html"

# KYTC measured-route layer; county 056 = Jefferson, prefix "I " = interstate.
KYTC_LAYER = (
    "http://maps.kytc.ky.gov/ArcGIS/rest/services/MeasuredRoute/MapServer/0/query"
)
COUNTY = "Jefferson"
COUNTY_CODE = "056"

# Titles look like "I-64 East-West between MM 11.0 and 11.8 ... in Jefferson County".
ROUTE_RE = re.compile(r"^\s*I-(\d+)\b", re.I)  # this map handles interstates
MM_RE = re.compile(r"\bMM\s*([\d.]+)(?:\s*(?:and|to|-|&)\s*([\d.]+))?", re.I)

USER_AGENT = "trimarc-geocode (+https://github.com/markschaver/TRIMARC)"
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 3


def latest_per_incident(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse the append-only log to the most recent row per incident."""
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        latest[row["incident_id"] or row["title"]] = row
    return list(latest.values())


def parse_incident(title: str) -> dict | None:
    """Extract route number and mile-marker range from a title, or None."""
    route = ROUTE_RE.match(title)
    marker = MM_RE.search(title)
    if not route or not marker:
        return None
    values = [float(v) for v in marker.groups() if v]
    return {
        "route": f"I-{route.group(1)}",
        "route_num": route.group(1),
        "mm_start": min(values),
        "mm_end": max(values),
        "mm_mid": sum((min(values), max(values))) / 2,
    }


def fetch_route_paths(route_num: str) -> list[list[tuple[float, float, float | None]]]:
    """Return the route's polyline paths as [(lon, lat, mile), ...] in WGS84."""
    where = f"RT_UNIQUE LIKE '{COUNTY_CODE}-I -{int(route_num):04d}%'"
    params = {
        "where": where,
        "outFields": "RT_UNIQUE",
        "returnM": "true",
        "outSR": "4326",
        "returnGeometry": "true",
        "f": "json",
    }
    url = KYTC_LAYER + "?" + urllib.parse.urlencode(params)
    last_error: Exception = RuntimeError("fetch never attempted")
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.load(response)
            if "error" in data:
                raise RuntimeError(f"KYTC error for I-{route_num}: {data['error']}")
            paths: list[list[tuple[float, float, float | None]]] = []
            for feature in data.get("features", []):
                for path in feature.get("geometry", {}).get("paths", []):
                    paths.append(
                        [(v[0], v[1], v[2] if len(v) > 2 else None) for v in path]
                    )
            return paths
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt < FETCH_ATTEMPTS:
                time.sleep(FETCH_BACKOFF_SECONDS * attempt)
    raise last_error


def locate(paths, mile: float) -> tuple[float, float] | None:
    """Interpolate a (lon, lat) at the given mile along the route's paths."""
    for path in paths:
        for (lon0, lat0, m0), (lon1, lat1, m1) in zip(path, path[1:]):
            if m0 is None or m1 is None or m0 == m1:
                continue
            if min(m0, m1) <= mile <= max(m0, m1):
                frac = (mile - m0) / (m1 - m0)
                return (lon0 + frac * (lon1 - lon0), lat0 + frac * (lat1 - lat0))
    return None


def build_features(incidents: list[dict[str, str]]):
    """Geocode incidents; return (features, skipped) with route geometry cached."""
    features, skipped = [], []
    cache: dict[str, list] = {}
    for row in incidents:
        if f"in {COUNTY} County" not in row["title"]:
            continue
        info = parse_incident(row["title"])
        if not info:
            skipped.append(("no route/mile marker (likely a ramp)", row["title"]))
            continue
        if info["route_num"] not in cache:
            cache[info["route_num"]] = fetch_route_paths(info["route_num"])
        point = locate(cache[info["route_num"]], info["mm_mid"])
        if point is None:
            skipped.append((f"MM {info['mm_mid']:g} off route", row["title"]))
            continue
        lon, lat = point
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                "properties": {
                    "incident_id": row["incident_id"],
                    "route": info["route"],
                    "mm_start": info["mm_start"],
                    "mm_end": info["mm_end"],
                    "title": row["title"],
                    "description": row["description"],
                    "pubDate": row["pubDate"],
                    "first_seen": row["first_seen"],
                },
            }
        )
    # Stable order keeps the committed GeoJSON/HTML diffs clean.
    features.sort(key=lambda f: (f["properties"]["route"], f["properties"]["mm_start"]))
    return features, skipped


MAP_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TRIMARC — Jefferson County incidents</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  #wrap{display:flex;flex-direction:column;height:100%}
  header{padding:10px 14px;background:#0b3d59;color:#fff}
  header h1{margin:0;font-size:16px;font-weight:600}
  header .meta{font-size:12px;opacity:.85;margin-top:3px}
  header a{color:#9fd3ff}
  #map{flex:1}
</style>
</head>
<body>
<div id="wrap">
  <header>
    <h1>TRIMARC — Jefferson County traffic incidents &amp; construction</h1>
    <div class="meta">__COUNT__ locations &middot; latest update __UPDATED__ &middot;
      source <a href="https://www.trimarc.org" target="_blank" rel="noopener">trimarc.org</a> &middot;
      <a href="https://github.com/markschaver/TRIMARC" target="_blank" rel="noopener">data &amp; code</a></div>
  </header>
  <div id="map"></div>
</div>
<script>
const data = __DATA__;
const map = L.map("map").setView([38.22, -85.74], 11);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"}).addTo(map);
const esc = s => (s || "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const layer = L.geoJSON(data, {onEachFeature: (f, l) => {
  const p = f.properties;
  const range = p.mm_end !== p.mm_start ? p.mm_start + "\\u2013" + p.mm_end : p.mm_start;
  l.bindPopup(
    "<b>" + esc(p.route) + " &mdash; MM " + range + "</b><br>" +
    "#" + esc(p.incident_id) + " &middot; " + esc(p.pubDate) + "<br>" +
    "<small>" + esc((p.description || "").slice(0, 320)) + "</small>",
    {maxWidth: 320});
}}).addTo(map);
if (layer.getBounds().isValid()) map.fitBounds(layer.getBounds().pad(0.1));
</script>
</body>
</html>
"""


def write_map(feature_collection: dict, updated: str, path: Path) -> None:
    """Write a self-contained Leaflet map (OpenStreetMap tiles) as one HTML file."""
    # Guard against a description accidentally closing the <script> tag.
    data_js = json.dumps(feature_collection).replace("</", "<\\/")
    html = (
        MAP_TEMPLATE
        .replace("__DATA__", data_js)
        .replace("__COUNT__", str(len(feature_collection["features"])))
        .replace("__UPDATED__", updated or "n/a")
    )
    path.write_text(html, encoding="utf-8")


def main() -> int:
    with CSV_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    incidents = latest_per_incident(rows)

    features, skipped = build_features(incidents)

    updated = max((f["properties"]["first_seen"] for f in features), default="")
    updated = updated.replace("T", " ").replace("Z", " UTC")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    fc = {"type": "FeatureCollection", "features": features}
    GEOJSON_PATH.write_text(json.dumps(fc, indent=2), encoding="utf-8")
    write_map(fc, updated, MAP_PATH)

    lats = [f["geometry"]["coordinates"][1] for f in features]
    lons = [f["geometry"]["coordinates"][0] for f in features]
    print(f"incidents (latest per id): {len(incidents)}")
    print(f"located: {len(features)}   skipped: {len(skipped)}")
    if features:
        print(f"lat range: {min(lats):.3f}..{max(lats):.3f}   "
              f"lon range: {min(lons):.3f}..{max(lons):.3f}")
    reasons: dict[str, int] = {}
    for reason, _ in skipped:
        key = "MM off route" if "off route" in reason else reason
        reasons[key] = reasons.get(key, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  skipped: {count:3}  {reason}")
    print(f"wrote {MAP_PATH.relative_to(ROOT)} and {GEOJSON_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # surface failures; the workflow won't block data on it
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
