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
    for attempt in range(3):
        try:
            url = f"https://geocode.maps.co/search?q={address}&format=json&limit=1"
            headers = {"User-Agent": "Georisk/1.0 (contact: Harnoor.Sethi27@bcp.org)"}
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if not data:
                return None
            return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception as e:
            print(f"Attempt {attempt+1}: Request failed: {e}")
            time.sleep(1)
    return None


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



# ---------- NOAA station / precip fix ----------
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
        # stationid must be a string like "GHCND:USW00023188"
        "stationid": s.get("id"),
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
import json

def get_fema_flood_data(lat, lon):
    """
    Fetch FEMA flood zone information for a given latitude and longitude.
    Returns a dictionary with the flood zone or 'Unknown' if not found.
    """
    url = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/0/query"
    
    # Correct geometry format for NFHL service
    params = {
        "geometry": json.dumps({"x": lon, "y": lat}),
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE",
        "returnGeometry": "false",
        "f": "json"
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()  # Raises HTTPError for 4xx/5xx
        data = response.json()
        
        # Check if any features returned
        if "features" in data and len(data["features"]) > 0:
            fld_zone = data["features"][0]["attributes"].get("FLD_ZONE", "Unknown")
            return {"fema_flood_zone": fld_zone}
        else:
            return {"fema_flood_zone": "Unknown"}
    
    except requests.exceptions.HTTPError as e:
        # Specific handling for 404 or other HTTP errors
        if response.status_code == 404:
            return {"fema_flood_zone": "Not Found"}
        else:
            return {"fema_flood_zone": "Error"}
    
    except requests.exceptions.RequestException as e:
        # Network errors, timeouts, etc.
        return {"fema_flood_zone": "Error"}


def normalize_fema_zone(zone):
    """
    Convert FEMA flood zone code into normalized 0–1 risk score.
    Accepts things like 'AE', 'A', 'A99', 'VE', 'X', 'D', None.
    """
    if not zone:
        return 0.2  # unknown / unmapped baseline

    z = str(zone).upper().strip()

    # map of canonical keys (longer prefixes first)
    # values chosen to reflect relative flood risk (0..1)
    mapping = {
        "VE": 1.0,   # coastal high velocity
        "V": 0.95,
        "AE": 0.9,
        "A": 0.8,
        "AH": 0.75,
        "AO": 0.72,
        "AR": 0.6,
        "A99": 0.55,
        "D": 0.4,
        "X": 0.2,
        "B": 0.2,
        "C": 0.2
    }

    # Try exact matches first
    if z in mapping:
        return mapping[z]

    # Try prefix matches for special cases (A99, AE##, etc.)
    # Check longer prefixes first
    prefixes = sorted(mapping.keys(), key=len, reverse=True)
    for p in prefixes:
        if z.startswith(p):
            return mapping[p]

    # fallback
    return 0.2




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


def clamp(x, min_val=0.0, max_val=1.0):
    return max(min(x, max_val), min_val)

def calculate_combined_landslide_score(data):
    """
    Combines landslide_risk, landslide_fema_score, elevation_m, and nearestWaterbody_km
    into one normalized 0–1 landslide risk value.
    """

    # --- Normalize each factor ---
    # LHASA landslide risk (already 0–1)
    lhasa = clamp(data.get("landslide_risk", 0))

    # FEMA landslide score (expected 0–1)
    fema = clamp(data.get("landslide_fema_score", 0))

    # Elevation proxy: higher = steeper terrain
    norm_elev = clamp(1 - math.exp(-data.get("elevation_m", 0) / 800.0))

    # Distance to nearest waterbody: closer = more erosion/saturation
    norm_water = clamp(1 - (data.get("nearestWaterbody_km", 10) / 10.0))

    # --- Weighted combination ---
    combined = (
        0.45 * lhasa +   # core landslide susceptibility
        0.25 * fema +    # FEMA soil/hazard component
        0.20 * norm_elev + 
        0.10 * norm_water
    )

    return clamp(combined)
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



def get_landslide_risk_score(lat, lon):
    """
    Fetch landslide risk from FEMA/LightBox API.
    Returns normalized score (0-1) or None if unavailable.
    """
    try:
        wkt_str = f"POINT({lon} {lat})"
        url = "https://api.lightboxre.com/v1/riskindexes/us/geometry"
        
        params = {
            "wkt": wkt_str,
            "bufferDistance": 50,
            "bufferUnit": "m"
        }
        
        headers = {"x-api-key": "wWdq6qAKQw2dF1G0SW9HDsKs6Km7DcSJ1VATLRckeDVqejGK"}
        
        response = requests.get(url, params=params, headers=headers, timeout=12)
        
        # Check for HTTP errors
        if response.status_code != 200:
            print(f"LightBox API returned status {response.status_code}: {response.text[:200]}")
            return None
            
        response.raise_for_status()
        data = response.json()
        
        # Validate response structure
        if not data or 'riskIndexes' not in data:
            print(f"LightBox API missing 'riskIndexes' key. Response: {data}")
            return None
            
        if not data['riskIndexes'] or len(data['riskIndexes']) == 0:
            print("LightBox API returned empty riskIndexes array")
            return None
            
        if 'hazards' not in data['riskIndexes'][0]:
            print(f"LightBox API missing 'hazards' key. Response: {data['riskIndexes'][0]}")
            return None
        
        # Find landslide hazard
        landslide = next(
            (haz for haz in data['riskIndexes'][0]['hazards'] 
             if haz.get('hazardType') == 'Landslide'), 
            None
        )
        
        if landslide:
            raw_score = landslide.get('hazardTypeRiskIndex', {}).get('score', 0)
            normalized_score = raw_score / 100.0 if raw_score > 1 else raw_score
            return normalized_score
            
        print("No Landslide hazard found in response")
        return None
        
    except requests.exceptions.RequestException as e:
        print(f"LightBox API request error: {e}")
        return None
    except (KeyError, IndexError, TypeError) as e:
        print(f"LightBox API response parsing error: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error in get_landslide_risk_score: {e}")
        return None

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


def clamp(x, min_val=0.0, max_val=1.0):
    return max(min(x, max_val), min_val)

def calculate_landslide_risk(data):
    """
    Returns a normalized (0–1) landslide risk score.
    Uses: lhasaRisk, elevation_m, noaa_precip_mm, nearestWaterbody_km, fema_flood_zone
    """
    norm_elev = clamp(1 - math.exp(-data.get("elevation_m", 0) / 800.0))
    norm_precip = clamp(data.get("noaa_precip_mm", 0) / 2000.0)
    norm_water = clamp(1 - (data.get("nearestWaterbody_km", 10) / 10.0))

    fema_zone = str(data.get("fema_flood_zone", "X")).upper()
    fema_weights = {"A": 1.0, "AE": 0.9, "AH": 0.8, "AO": 0.8, "VE": 0.7, "V": 0.7, "X": 0.3}
    norm_fema = fema_weights.get(fema_zone, 0.5)

    lhasa = clamp(data.get("lhasaRisk", 0))

    risk = (
        0.40 * lhasa +
        0.20 * norm_precip +
        0.20 * norm_elev +
        0.10 * norm_water +
        0.10 * norm_fema
    )

    return clamp(risk)

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
                ex.submit(get_noaa_precip, lat, lon, "2024-10-01", "2025-09-11"): "noaa_precip",
                ex.submit(get_fema_flood_data, lat, lon): "fema_zone",  # ✅ new
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

        try:
            fault_dis = get_faultDis(lat, lon)
        except Exception as e:
            print(f"fault_dis error: {e}")
            fault_dis = 0.0

              # after the parallel futures complete:
        elevation = results.get("elevation")
        noaa_precip = results.get("noaa_precip")
        fema_result = results.get("fema_zone")   # this should be the dict returned by get_fema_flood_data
        waterbody = results.get("waterbody")
        aqi = results.get("aqi", "Unknown")
        landslide_risk = results.get("lhasa", 0.0)
        landslide_fema_score = get_landslide_risk_score(lat, lon)
        if landslide_fema_score is None:
            landslide_fema_score = 0.0
        # extract zone string robustly
     # extract zone string robustly
        fema_zone = None
        if isinstance(fema_result, dict):
            fema_zone = fema_result.get("fema_flood_zone") 
        elif isinstance(fema_result, str):
            fema_zone = fema_result
        else:
            fema_zone = None


        # normalize
        norm_fema = normalize_fema_zone(fema_zone)

        # distance normalization (default if not found)
        if isinstance(waterbody, (int, float)):
            dist_km = float(waterbody)
        else:
            dist_km = 8.5

        def clamp(v, lo=0, hi=1):
            try:
                return max(lo, min(hi, float(v)))
            except:
                return 0.0

        norm_dist = clamp(1 - (float(dist_km) / 10.0))
        norm_elevation = clamp(1 - (float(elevation or 0) / 500.0))
        norm_precip = clamp((float(noaa_precip or 0) / 2000.0))
        # Weighted flood risk components
        elev_w, precip_w, fema_w, dist_w = 0.35, 0.25, 0.30, 0.10
        flood_risk = (
            (norm_elevation * elev_w)
            + (norm_precip * precip_w)
            + (norm_fema * fema_w)
            + (norm_dist * dist_w)
        )
        data = {
        "landslide_risk": landslide_risk,          # NASA LHASA risk (0–1)
         "landslide_fema_score": landslide_fema_score,  # FEMA LightBox risk (0–1)
        "elevation_m": elevation or 0.0,
        "nearestWaterbody_km": waterbody or 10.0
        }
        combined_landslide_score = calculate_combined_landslide_score(data)



        return jsonify({
            "success": True,
            "address": address,
            "coordinates": {"lat": lat, "lon": lon},
            "airQualityIndex": aqi,
            "landslideRisk": landslide_risk,
            "landslideFema": landslide_fema_score,
            "earthquakeRisk": earthquake_risk,
            "faultDistanceNormalized": fault_dis,
            "pgauh": pgauh,
            "siteClass": site_class,
            "riskCategory": risk_category,
            "elevation_m": elevation,
            "noaa_precip_mm": noaa_precip,
            "fema_flood_zone": fema_zone,            
            "fema_flood_raw": fema_result,             
            "nearestWaterbody_km": dist_km,
            "normalizedElevation": norm_elevation,
            "normalizedPrecipitation": norm_precip,
            "normalizedFEMAZone": norm_fema,
            "normalizedDistanceToWater": norm_dist,
            "landslide_combined_score": combined_landslide_score,
            "floodRisk": round(float(flood_risk), 3)
        })


    except Exception as e:
        print(f"Error in /risk-summary: {e}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500
