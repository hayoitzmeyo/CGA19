from flask import Blueprint, request, jsonify
import requests
import pyproj
import math
import time

bp = Blueprint("firealg", __name__)

# ----------------- Utilities -----------------
def latlontowebmercator(lat, lon):
    transformer = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x, y = transformer.transform(lon, lat)
    return x, y

def safe_get_json(url, params=None, headers=None, retries=3, backoff=2):
    headers = headers or {"User-Agent": "GeoRisk/1.0 (contact: Harnoor.Sethi27@bcp.org)"}
    for attempt in range(1, retries+1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            if resp.status_code != 200:
                print(f"Attempt {attempt}: {url} returned {resp.status_code}")
                time.sleep(backoff ** attempt)
                continue
            return resp.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            print(f"Attempt {attempt}: Invalid JSON from {url}")
            print(resp.text[:500])
            time.sleep(backoff ** attempt)
        except requests.exceptions.RequestException as e:
            print(f"Attempt {attempt}: Request failed: {e}")
            time.sleep(backoff ** attempt)
    return None

def normalizesdi(sdi, high_cutoff=40, min_sdi=0):
    if sdi is None:
        return None
    try:
        sdi = float(sdi)
    except (ValueError, TypeError):
        return None
    sdi = max(min_sdi, sdi)
    normalized = (sdi - min_sdi) / (high_cutoff - min_sdi)
    return min(normalized, 1.0)

def quantilenormalizer(value, high, low):
    if value is None or value == "NoData":
        return None
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    value = max(low, value)
    normalized = (value - low) / (high - low)
    return min(normalized, 1.0)

def normalizefirecount(firecount, radius_km=60, years=5, min_density=0, max_density=0.00049):
    area_km2 = math.pi * (radius_km ** 2)
    annual_density = firecount / (area_km2 * years)
    normalized = (annual_density - min_density) / (max_density - min_density)
    normalized = max(0, min(normalized, 1))
    return normalized

def get_coordinates(address):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": "GeoRisk/1.0 (contact: your_email@example.com)"}
    data = safe_get_json(url, params=params, headers=headers)
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])

def gethousingunitrisk(lat, lon):
    url = "https://apps.fs.usda.gov/fsgisx01/rest/services/RDW_Wildfire/RMRS_WRC_HousingUnitRisk/ImageServer/identify"
    x, y = latlontowebmercator(lat, lon)
    geometry = {"x": x, "y": y}
    params = {
        'geometry': str(geometry).replace("'", '"'),
        'geometryType': 'esriGeometryPoint',
        'sr': 3857,
        'tolerance': 2,
        'mapExtent': f'{x-100},{y-100},{x+100},{y+100}',
        'imageDisplay': '400,400,96',
        'returnGeometry': 'false',
        'f': 'json'
    }
    data = safe_get_json(url, params=params)
    if not data:
        return None

    value = data.get('value')
    if value is not None and value != "NoData":
        try:
            return float(value)
        except:
            pass
    for v in data.get('properties', {}).get('Values', []):
        if v is not None and v != "NoData":
            try:
                return float(v)
            except:
                continue
    return None

def getburnprobability(lat, lon):
    url = "https://apps.fs.usda.gov/fsgisx01/rest/services/RDW_Wildfire/RMRS_WRC_WildfireHazardPotential/ImageServer/identify"
    x, y = latlontowebmercator(lat, lon)
    geometry = {"x": x, "y": y}
    params = {
        'geometry': str(geometry).replace("'", '"'),
        'geometryType': 'esriGeometryPoint',
        'sr': 3857,
        'tolerance': 2,
        'mapExtent': f'{x-100},{y-100},{x+100},{y+100}',
        'imageDisplay': '400,400,96',
        'returnGeometry': 'false',
        'f': 'json'
    }
    data = safe_get_json(url, params=params)
    if not data:
        return None

    value = data.get('value')
    if value is not None and value != "NoData":
        try:
            return float(value)
        except:
            pass
    for v in data.get('properties', {}).get('Values', []):
        if v is not None and v != "NoData":
            try:
                return float(v)
            except:
                continue
    return None

def historicalfiredensity(lat, lon, radius_km=60):
    url = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_FireOccurrenceAndPerimeter_01/MapServer/8/query"
    params = {
        "where": "1=1",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_km * 1000,
        "units": "esriSRUnit_Meter",
        "returnCountOnly": "true",
        "f": "json"
    }
    data = safe_get_json(url, params=params)
    if not data:
        return 0
    return data.get("count", 0)

def getsuppressiondifficulty(lat, lon, radiuskm=60):
    url = "https://apps.fs.usda.gov/fsgisx01/rest/services/RDW_Wildfire/RMRS_Wildfire_Suppression_Difficulty_Index_90thPercentile/ImageServer/identify"
    x, y = latlontowebmercator(lat, lon)
    delta = radiuskm * 1000 
    geometry = {"x": x, "y": y}
    params = {
        'geometry': str(geometry).replace("'", '"'),
        'geometryType': 'esriGeometryPoint',
        'sr': 3857,
        'tolerance': 2,
        'mapExtent': f'{x - delta},{y - delta},{x + delta},{y + delta}',
        'imageDisplay': '400,400,96',
        'returnGeometry': 'false',
        'returnAllPixelValues': 'true', 
        'f': 'json'
    }
    data = safe_get_json(url, params=params)
    if not data:
        return None

    values = []
    if 'properties' in data and 'Values' in data['properties']:
        for v in data['properties']['Values']:
            try:
                if v is not None and v != "NoData":
                    values.append(float(v))
            except:
                continue
    if values:
        return max(values)
    else:
        value = data.get('value')
        try:
            if value is not None and value != "NoData":
                return float(value)
        except:
            pass
    return None

@bp.route('/fire-risk-summary', methods=['POST'])
def fire_risk_summary():
    try:
        data = request.json
        address = data.get('address')
        coords = get_coordinates(address)
        if not coords:
            return jsonify({"error": "Invalid address"}), 400

        lat, lon = coords
        print("Coordinates:", coords)

        # Get all risk inputs safely
        burnprob = getburnprobability(lat, lon) or 0
        hurisk = gethousingunitrisk(lat, lon) or 0
        sdi = getsuppressiondifficulty(lat, lon) or 0
        firecount = historicalfiredensity(lat, lon) or 0

        # Normalize values
        harprobability = quantilenormalizer(burnprob, 1000, 50)
        normhurisk = quantilenormalizer(hurisk, 700, 0)
        normsdi = normalizesdi(sdi)
        normfiredensity = normalizefirecount(firecount)

        # Weights
        burnriskweight = 0.25
        normhuweight = 0.3
        normsdiweight = 0.15
        normfiredensityweight = 0.3

        # Compute general weighted risk safely
        generalweightedrisk = (
            (harprobability or 0) * burnriskweight +
            (normhurisk or 0) * normhuweight +
            (normfiredensity or 0) * normfiredensityweight +
            (normsdi or 0) * normsdiweight
        )

        return jsonify({
            "harprobability": harprobability,
            "normhurisk": normhurisk,
            "normsdi": normsdi,
            "normfiredensity": normfiredensity,
            "generalweightedrisk": generalweightedrisk
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
