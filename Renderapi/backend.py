from flask import Blueprint, request, jsonify
import requests
import pyproj
import math
from shapely.geometry import shape, Point, mapping
import numpy as np
import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache, wraps
from math import radians, sin, cos, sqrt, atan2


bp = Blueprint("backend", __name__)

# -----------------------
# Helpers / Simple cache
# -----------------------
def round_coords(lat, lon, ndigits=4):
    return (round(float(lat), ndigits), round(float(lon), ndigits))

def safe_api_get(url, params=None, headers=None, method="get", json_body=None, timeout=12, retries=1):
    """Basic GET/POST with timeout and retries. Returns parsed JSON or None."""
    for attempt in range(retries + 1):
        try:
            if method.lower() == "get":
                r = requests.get(url, params=params, headers=headers, timeout=timeout)
            else:
                r = requests.post(url, params=params, headers=headers, json=json_body, timeout=timeout)
            if r.status_code >= 500:
                # server errors => retry
                time.sleep(0.5 * (attempt + 1))
                continue
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                # not JSON
                return None
        except requests.HTTPError as he:
            # client error, likely bad request - don't retry too much
            if r.status_code in (429, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            print(f"HTTPError for {url}: {he} (status {getattr(r,'status_code',None)})")
            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            print(f"Error calling {url}: {e}")
            return None
    return None

# -----------------------
# Geocoding + misc
# -----------------------
def get_coordinates(address):
    import requests
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": address,
        "format": "json",
        "limit": 1
    }
    headers = {
        "User-Agent": "Georisk/1.0 (contact: Harnoor.Sethi27@bcp.org)"  # <-- MUST include contact
    }

    response = requests.get(url, params=params, headers=headers, timeout=10)
    response.raise_for_status()  
    data = response.json()
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])

def get_elevation(lat, lon):
    try:
        url = "https://api.open-elevation.com/api/v1/lookup"
        body = {"locations": [{"latitude": lat, "longitude": lon}]}
        data = safe_api_get(url, method="post", json_body=body, timeout=10, retries=1)
        if data and "results" in data and data["results"]:
            return data["results"][0].get("elevation")
        return None
    except Exception as e:
        print(f"Elevation fetch error: {e}")
        return None

# -----------------------
# NOAA precipitation (station lookup then data lookup)
# -----------------------
NOAA_TOKEN = "mZrOfwskAmScPKKmsBhejnjbSBVYunzO"

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0  # Earth radius in km
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def find_nearest_noaa_station(lat, lon):
    url = (
        f"https://www.ncei.noaa.gov/cdo-web/api/v2/stations"
        f"?datasetid=GHCND&datatypeid=PRCP"
        f"&extent={lat-0.05},{lon-0.05},{lat+0.05},{lon+0.05}"  # 0.1° box around point
        f"&limit=50"
    )

    headers = {"token": NOAA_TOKEN}
    response = requests.get(url, headers=headers)

    if not response.ok:
        print(f"NOAA stations call {response.status_code}: {response.text}")
        response.raise_for_status()

    data = response.json()
    if "results" not in data or not data["results"]:
        print("No stations found near this coordinate.")
        return None

    # Compute nearest manually
    stations = data["results"]
    nearest = min(
        stations,
        key=lambda s: haversine(lat, lon, s["latitude"], s["longitude"])
    )

    return {
        "id": nearest["id"],
        "name": nearest.get("name", "Unknown"),
        "distance_km": round(haversine(lat, lon, nearest["latitude"], nearest["longitude"]), 2),
        "latitude": nearest["latitude"],
        "longitude": nearest["longitude"]
    }

def get_noaa_precip(lat, lon, startdate, enddate):
    """Return a precipitation value (or None)."""
    # Step 1: find station (cached)
    s = find_nearest_noaa_station(lat, lon)
    if not s:
        return None
    data_url = "https://www.ncei.noaa.gov/cdo-web/api/v2/data"
    headers = {"token": NOAA_TOKEN}
    params = {
        "datasetid": "GHCND",
        "datatypeid": "PRCP",
        "stationid": s,
        "startdate": startdate,
        "enddate": enddate,
        "limit": 1,
        "units": "metric"
    }
    try:
        resp = requests.get(data_url, headers=headers, params=params, timeout=12)
        if resp.status_code >= 400:
            print(f"NOAA data call {resp.status_code}: {resp.text[:512]}")
            resp.raise_for_status()
        data = resp.json()
        if "results" in data and data["results"]:
            return data["results"][0].get("value")
        return None
    except Exception as e:
        print(f"NOAA fetch error: {e}")
        return None

# -----------------------
# FEMA flood with safe return
# -----------------------
FEMA_KEY = "ESKxETVHZm4pZXY6WH9UjkUhtDnAVT73TJXblPg8"

import requests
import time

FEMA_API_URL = "https://api.nationalflooddata.com/v3/data"
FEMA_KEY = "YOUR_FEMA_API_KEY"

def get_fema_flood_data(lat, lon, retries=3):
    """Fetch FEMA flood data with retry + backoff handling."""
    for attempt in range(retries):
        try:
            response = requests.get(
                FEMA_API_URL,
                params={"latitude": lat, "longitude": lon},
                headers={"x-api-key": FEMA_KEY},
                timeout=15
            )
            
            if response.status_code == 429:
                wait = (2 ** attempt) + 1  # exponential backoff
                print(f"Rate limited by FEMA, retrying in {wait}s...")
                time.sleep(wait)
                continue  # retry
                
            response.raise_for_status()
            data = response.json()
            return data  # successful

        except requests.exceptions.RequestException as e:
            print(f"FEMA flood API error: {e}")
            time.sleep(1)
    
    print("FEMA flood API failed after multiple retries.")
    return None


# -----------------------
# Overpass / nearest waterbody (lightweight via requests)
# -----------------------
def get_closest_waterbody(lat, lon, max_radius_m=5000):
    """
    Query Overpass via requests (we avoid overpy to control timeouts & memory).
    Returns float (km) or None.
    """
    # smaller radius to avoid huge responses
    radius = min(max_radius_m, 5000)
    overpass_url = "https://overpass-api.de/api/interpreter"
    # Overpass around: expects meters, lat, lon order; use out center so ways have center
    query = f"""
    [out:json][timeout:10];
    (
      node["natural"="water"](around:{radius},{lat},{lon});
      node["water"](around:{radius},{lat},{lon});
      way["natural"="water"](around:{radius},{lat},{lon});
      way["water"](around:{radius},{lat},{lon});
      way["waterway"="riverbank"](around:{radius},{lat},{lon});
      relation["waterway"="riverbank"](around:{radius},{lat},{lon});
    );
    out center qt 50;
    """
    try:
        resp = requests.post(overpass_url, data={"data": query}, timeout=12)
        if resp.status_code != 200:
            print(f"Overpass returned {resp.status_code}")
            return None
        j = resp.json()
        elements = j.get("elements", [])
        if not elements:
            return None
        min_dist = None
        for el in elements:
            if "lat" in el and "lon" in el:
                wlat, wlon = el["lat"], el["lon"]
            elif "center" in el:
                wlat, wlon = el["center"]["lat"], el["center"]["lon"]
            else:
                continue
            dist = approximate_distance_km(lat, lon, wlat, wlon)
            if min_dist is None or dist < min_dist:
                min_dist = dist
        return min_dist
    except Exception as e:
        print(f"Overpass error: {e}")
        return None

# -----------------------
# Air quality
# -----------------------
def get_air_quality(lat, lon):
    try:
        url = f"https://air-quality-api.open-meteo.com/v1/air-quality"
        params = {"latitude": lat, "longitude": lon, "hourly": "us_aqi"}
        data = safe_api_get(url, params=params, timeout=10, retries=1)
        if data and "hourly" in data and "us_aqi" in data["hourly"]:
            aqi_values = [x for x in data["hourly"]["us_aqi"] if x is not None]
            return aqi_values[0] if aqi_values else "Unknown"
        return "Unknown"
    except Exception as e:
        print(f"Error getting air quality: {e}")
        return "Unknown"

# -----------------------
# Fault distance without geopandas
# -----------------------
def get_faultDis(lat, lon):
    """
    Query USGS fault service, parse GeoJSON features with shapely,
    compute distance in projected coordinates (EPSG:3310).
    Returns normalized inverse-distance 0..1 (higher -> closer).
    """
    try:
        # Build a bounding envelope in Web Mercator (EPSG:3857)
        proj_to_3857 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        x, y = proj_to_3857.transform(lon, lat)
        buffer_m = 500_000
        xmin, ymin, xmax, ymax = x - buffer_m, y - buffer_m, x + buffer_m, y + buffer_m
        url = "https://earthquake.usgs.gov/arcgis/rest/services/haz/NSHM_Fault_Sources/MapServer/0/query"
        params = {
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "1=1",
            "outFields": "*",
            "f": "geojson",
            "resultRecordCount": 500
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        j = r.json()
        features = j.get("features", [])
        if not features:
            # no faults found in envelope
            return 0.0

        # Prepare transformers to EPSG:3310 for accurate planar distances
        proj_to_3310 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3310", always_xy=True)
        pt_x, pt_y = proj_to_3310.transform(lon, lat)
        min_dist_m = None

        for feat in features:
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                g = shape(geom)
            except Exception:
                continue
            # transform geometry coordinates to epsg:3310 by mapping coordinates
            def _transform_coords(xy):
                return proj_to_3310.transform(xy[0], xy[1])
            # shapely's transform not imported to avoid extra dependency; do simple approach:
            # compute distance by sampling representative points from geometry bounds if needed
            # (but we'll compute distance from point to geometry in lat/lon by transforming both)
            # Create shapely point in projected coords and compute distance using shapely if available
            # We'll transform geometry using shapely.ops.transform if available:
            try:
                from shapely.ops import transform as shapely_transform
                geom_proj = shapely_transform(lambda x, y, z=None: proj_to_3310.transform(x, y), g)
                d = geom_proj.distance(Point(pt_x, pt_y))
                if min_dist_m is None or d < min_dist_m:
                    min_dist_m = d
            except Exception:
                # fallback: compute distance approx using centroid
                try:
                    cx, cy = g.centroid.x, g.centroid.y
                    cxp, cyp = proj_to_3310.transform(cx, cy)
                    d = math.hypot(cxp - pt_x, cyp - pt_y)
                    if min_dist_m is None or d < min_dist_m:
                        min_dist_m = d
                except Exception:
                    continue

        if min_dist_m is None:
            return 0.0
        # min_dist_m in meters -> convert to km
        min_dist_km = float(min_dist_m) / 1000.0
        # normalized = min(1.0, 1/(distance_km)) with mild scaling so e.g. 1 km -> 1.0, 10 km -> 0.1
        if min_dist_km <= 0:
            return 1.0
        normalized = min(1.0, 1.0 / min_dist_km)
        return round(float(normalized), 4)
    except Exception as e:
        print(f"Error calculating fault distance: {e}")
        return 0.0


# -----------------------
# Site class and PGAUH (USGS)
# -----------------------
def get_siteClass(lat, lon):
    try:
        url = "https://earthquake.usgs.gov/ws/designmaps/metadata.json"
        params = {"referenceDocument": "ASCE7-16", "latitude": lat, "longitude": lon}
        data = safe_api_get(url, params=params, timeout=10, retries=1)
        if not data or "response" not in data or "data" not in data["response"]:
            return "C"
        vs30 = data["response"]["data"].get("vs30", 0)
        if vs30 > 1500:
            return "A"
        elif 760 <= vs30 <= 1500:
            return "B"
        elif 360 <= vs30 < 760:
            return "C"
        elif 180 <= vs30 < 360:
            return "D"
        elif 120 <= vs30 < 180:
            return "E"
        else:
            return "F"
    except Exception as e:
        print(f"Error getting site class: {e}")
        return "C"

def get_pgauh(lat, lon, siteClass=None, riskCategory=None):
    try:
        if siteClass is None:
            siteClass = get_siteClass(lat, lon)
        if riskCategory is None:
            riskCategory = "I"
        url = "https://earthquake.usgs.gov/ws/designmaps/asce7-22.json"
        params = {"latitude": lat, "longitude": lon, "siteClass": siteClass, "riskCategory": riskCategory, "title": "ASCE7-22"}
        data = safe_api_get(url, params=params, timeout=15, retries=1)
        if data and "response" in data and "data" in data["response"] and "underlyingData" in data["response"]["data"]:
            hazard_list = data["response"]["data"]["underlyingData"].get("pgauh", [])
            return hazard_list if hazard_list else [0.0]
        return [0.0]
    except Exception as e:
        print(f"Error getting PGAUH: {e}")
        return [0.0]

# -----------------------
# Building type via Overpass (requests)
# -----------------------
@lru_cache(maxsize=1024)
def get_buildingType_cached(lat_r, lon_r, length):
    """Cached wrapper expects rounded coords"""
    try:
        lat = float(lat_r)
        lon = float(lon_r)
        api_url = "https://overpass-api.de/api/interpreter"
        # use a very small bbox by default to avoid big downloads
        s = lat - length
        n = lat + length
        w = lon - length
        e = lon + length
        # restrict to ways; out tags center to get center coordinates
        query = f"""
        [out:json][timeout:8];
        way["building"]({s},{w},{n},{e});
        out tags center qt 10;
        """
        resp = requests.post(api_url, data={"data": query}, timeout=10)
        if resp.status_code != 200:
            # non-200 responses are common, handle gracefully
            # print(resp.status_code, resp.text[:200])
            return None
        j = resp.json()
        ways = j.get("elements", [])
        # elements are nodes/ways/relations - pick first meaningful building tag
        for el in ways:
            tags = el.get("tags") or {}
            b = tags.get("building")
            if b and b != "yes":
                return b
        for el in ways:
            tags = el.get("tags") or {}
            if tags.get("building") == "yes":
                return "yes"
        return None
    except Exception as e:
        print(f"Error getting building type (overpass): {e}")
        return None

def get_buildingType(lat, lon, length):
    # use rounding and cached function to avoid repeated network calls for near-identical coords
    lat_r, lon_r = round_coords(lat, lon, ndigits=4)
    return get_buildingType_cached(lat_r, lon_r, length)

# -----------------------
# Risk Category logic
# -----------------------
def get_riskCategory(lat, lon):
    try:
        buildingType = get_buildingType(lat, lon, 0.0005)  # smaller radius
        if not buildingType:
            return "I"
        buildingType = buildingType.lower()
        essential_facilities = ["hospital", "fire_station", "police", "emergency"]
        high_risk = ["school", "industrial", "public", "government", "university"]
        moderate_risk = ["commercial", "retail", "warehouse", "hotel", "office", "yes", "mixed_use"]
        low_risk = ["residential", "house", "detached", "apartments", "apartment"]
        if buildingType in essential_facilities:
            return "IV"
        elif buildingType in high_risk:
            return "III"
        elif buildingType in moderate_risk:
            return "II"
        elif buildingType in low_risk:
            return "I"
        else:
            return "I"
    except Exception as e:
        print(f"Error determining risk category: {e}")
        return "I"

# -----------------------
# Landslide / NASA (unchanged)
# -----------------------
def get_lhasaRisk(lat, lon, pad):
    try:
        lhasa_url = "https://maps.nccs.nasa.gov/mapping/rest/services/landslide_viewer/Landslide_Susceptibility_Update_2023/MapServer"
        url = lhasa_url + "/identify"
        params = {
            "f": "json",
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": "all:0",
            "tolerance": 3,
            "mapExtent": f"{lon-pad},{lat-pad},{lon+pad},{lat+pad}",
            "imageDisplay": "400,400,96",
            "returnGeometry": "false",
        }
        data = safe_api_get(url, params=params, timeout=12, retries=1)
        if not data or not data.get("results"):
            return 0.0
        value = data["results"][0]["attributes"].get("Raster.Value")
        if value is not None:
            return float(value) / 5.0
        return 0.0
    except Exception as e:
        print(f"Error getting landslide risk: {e}")
        return 0.0

# -----------------------
# Earthquake risk aggregator
# -----------------------
def safe_sqrt_transform(x, y, z):
    try:
        x = max(0, float(x) if x is not None else 0)
        y = max(0, float(y) if y is not None else 0)
        z = max(0, float(z) if z is not None else 0)
        arr = np.array([x, y, z])
        roots = np.sqrt(arr)
        return float(np.mean(roots))
    except Exception as e:
        print(f"Error in sqrt transform: {e}")
        return 0.0

def get_earthquake_risk(lat, lon):
    try:
        faultDis = get_faultDis(lat, lon)
        siteClass = get_siteClass(lat, lon)
        riskCategory = get_riskCategory(lat, lon)
        pgauh = get_pgauh(lat, lon, siteClass, riskCategory)
        if isinstance(pgauh, list) and pgauh:
            pgauh_avg = np.mean([x for x in pgauh if x is not None])
        else:
            try:
                pgauh_avg = float(pgauh)
            except Exception:
                pgauh_avg = 0.0
        lhasaRisk = get_lhasaRisk(lat, lon, 0.01) or 0
        return safe_sqrt_transform(faultDis, pgauh_avg, lhasaRisk)
    except Exception as e:
        print(f"Error calculating earthquake risk: {e}")
        return 0.0

# -----------------------
# Distance helper
# -----------------------
def approximate_distance_km(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.320 * math.cos(mean_lat)
    dx = delta_lon * km_per_deg_lon
    dy = delta_lat * km_per_deg_lat
    return math.sqrt(dx*dx + dy*dy)

# -----------------------
# Main route — parallelize independent calls
# -----------------------
@bp.route('/risk-summary', methods=['POST'])
def risk_summary():
    try:
        payload = request.json
        if not payload or 'address' not in payload:
            return jsonify({"error": "Address is required"}), 400
        address = payload.get("address")
        coords = get_coordinates(address)
        if not coords:
            return jsonify({"error": "Address not found"}), 400
        lat, lon = coords

        # We'll parallelize inexpensive independent calls to reduce wall-clock time
        results = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {
                ex.submit(get_elevation, lat, lon): "elevation",
                ex.submit(get_noaa_precip, lat, lon, "2020-01-01", "2025-09-11"): "noaa_precip",
                ex.submit(get_fema_flood_data, lat, lon): "fema",
                ex.submit(get_closest_waterbody, lat, lon): "waterbody",
                ex.submit(get_air_quality, lat, lon): "aqi",
                ex.submit(get_lhasaRisk, lat, lon, 0.01): "lhasa"
            }
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    results[key] = fut.result()
                except Exception as e:
                    print(f"Parallel task {key} error: {e}")
                    results[key] = None

        # Compute building/site/risk-related sequentially (they have dependencies)
        try:
            site_class = get_siteClass(lat, lon)
        except Exception as e:
            print(f"site_class error: {e}")
            site_class = "C"
        try:
            risk_category = get_riskCategory(lat, lon)
        except Exception as e:
            print(f"risk_category error: {e}")
            risk_category = "I"
        try:
            pgauh = get_pgauh(lat, lon, siteClass=site_class, riskCategory=risk_category)
        except Exception as e:
            print(f"pgauh error: {e}")
            pgauh = [0.0]

        # Earthquake risk (light)
        try:
            earthquake_risk = get_earthquake_risk(lat, lon)
        except Exception as e:
            print(f"earthquake_risk error: {e}")
            earthquake_risk = 0.0

        # Fault distance approximated was not in parallel to avoid double USGS calls,
        # but we can get it if not computed inside earthquake risk
        try:
            fault_dis = get_faultDis(lat, lon)
        except Exception as e:
            print(f"fault_dis error: {e}")
            fault_dis = 0.0

        elevation = results.get("elevation")
        noaa_precip = results.get("noaa_precip")
        fema_data = results.get("fema") or {}
        waterbody = results.get("waterbody")
        aqi = results.get("aqi", "Unknown")
        landslide_risk = results.get("lhasa", 0.0)

        def clamp(v, lo=0, hi=1):
            try:
                return max(lo, min(hi, float(v)))
            except:
                return 0.0

        norm_elevation = clamp(1 - (float(elevation or 0) / 500.0))
        norm_precip = clamp((float(noaa_precip or 0) / 2000.0))

        fema_zone = fema_data.get("zone") if isinstance(fema_data, dict) else None
        zone_weights = {
            "V": 1.0, "VE": 1.0, "A": 0.8, "AE": 0.9,
            "AO": 0.7, "AH": 0.7, "X": 0.3, "D": 0.4
        }
        norm_fema = zone_weights.get(str(fema_zone).upper()[:2], 0.2)

        if isinstance(waterbody, (int, float)):
            dist_km = float(waterbody)
        else:
            dist_km = 10.0

        norm_dist = clamp(1 - (float(dist_km) / 10.0))

        elev_w, precip_w, fema_w, dist_w = 0.35, 0.25, 0.30, 0.10
        flood_risk = (
            (norm_elevation * elev_w)
            + (norm_precip * precip_w)
            + (norm_fema * fema_w)
            + (norm_dist * dist_w)
        )

        return jsonify({
            "success": True,
            "address": address,
            "coordinates": {"lat": lat, "lon": lon},
            "airQualityIndex": aqi,
            "landslideRisk": landslide_risk,
            "earthquakeRisk": earthquake_risk,
            "faultDistanceNormalized": fault_dis,
            "pgauh": pgauh,
            "siteClass": site_class,
            "riskCategory": risk_category,
            "elevation_m": elevation,
            "noaa_precip_mm": noaa_precip,
            "fema_flood_zone": fema_data,
            "nearestWaterbody_km": dist_km,
            "normalizedElevation": norm_elevation,
            "normalizedPrecipitation": norm_precip,
            "normalizedFEMAZone": norm_fema,
            "normalizedDistanceToWater": norm_dist,
            "floodRisk": round(float(flood_risk), 3)
        })

    except Exception as e:
        print(f"Error in /risk-summary: {e}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500
