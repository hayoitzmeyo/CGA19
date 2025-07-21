from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import pyproj
import numpy as np
import math

app = Flask(__name__)
CORS(app)

def latlontowebmercator(lat, lon):
    transformer = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x, y = transformer.transform(lon, lat)
    return x, y

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


def get_coordinates(address):
    url = f"https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1"
    response1 = requests.get(url, headers={"User-Agent": "risk-app"})
    data = response1.json()
    if not data:
        return None
    return float(data[0]['lat']), float(data[0]['lon'])

def gethousingunitrisk(lat, lon):
    #risk for INDIVIDUAL establishment, could parse in spread like burn probability but this one returns valid data for all pixels so no biggie
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
    response = requests.get(url, params=params)
    data = response.json()
    value = data.get('value')
    try:
        if value is not None and value != "NoData":
            return float(value)
    except Exception:
        pass
    values = data.get('properties', {}).get('Values', [])
    for v in values:
        if v is not None and v != "NoData":
            try:
                return float(v)
            except Exception:
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
    response = requests.get(url, params=params)
    data = response.json()
    value = data.get('value')
    #parse through set of integers until real number
    try:
        if value is not None and value != "NoData":
            return float(value)
    except Exception:
        pass
    values = data.get('properties', {}).get('Values', [])
    for v in values:
        if v is not None and v != "NoData":
            try:
                return float(v)
            except Exception:
                continue
    return None


def normalizefirecount(firecount, radius_km=60, years=5, min_density=0, max_density=0.00049):
    area_km2 = math.pi * (radius_km ** 2)
    annual_density = firecount / (area_km2 * years)
    normalized = (annual_density - min_density) / (max_density - min_density)
    normalized = max(0, min(normalized, 1)) 
    return normalized


def quantilenormalizer(burnprob, high_risk_cutoff, min_burnprob):
    if burnprob is None or burnprob == "NoData":
        return None
    try:
        burnprob = float(burnprob)
    except (ValueError, TypeError):
        return None
    burnprob = max(min_burnprob, burnprob)
    normalized = (burnprob - min_burnprob) / (high_risk_cutoff - min_burnprob)
    return min(normalized, 1.0)

def searchburnprobfast(lat, lon, radius_km=20):
    # searches in 3x3 matrix
    import math
    offsets = [
        (0, 0),  
        (radius_km/111, 0),  
        (-radius_km/111, 0),
        (0, radius_km/111), 
        (0, -radius_km/111), 
        (radius_km/111/math.sqrt(2), radius_km/111/math.sqrt(2)),   
        (radius_km/111/math.sqrt(2), -radius_km/111/math.sqrt(2)),  
        (-radius_km/111/math.sqrt(2), radius_km/111/math.sqrt(2)),  
        (-radius_km/111/math.sqrt(2), -radius_km/111/math.sqrt(2)), 
    ]
    for dlat, dlon in offsets:
        lat2 = lat + dlat
        lon2 = lon + dlon
        burnprob = getburnprobability(lat2, lon2)
        if burnprob is not None and burnprob != "NoData":
            try:
                return float(burnprob)
            except:
                continue
    return None


def historicalfiredensity(lat, lon, radius_km=60):
    """
    Returns the number of fire perimeters that intersect the given point (within optional buffer radius).
    """
    url = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_FireOccurrenceAndPerimeter_01/MapServer/8/query"
    params = {
        "where": "1=1",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_km * 1000,  # meters
        "units": "esriSRUnit_Meter",
        "returnCountOnly": "true",
        "f": "json"
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
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
    response = requests.get(url, params=params)
    data = response.json()

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



@app.route('/risk-summary', methods=['POST'])
def risk_summary():
    try:
        data = request.json
        address = data.get('address')
        coords = get_coordinates(address)
        if not coords:
            return jsonify({"error": "no address"}), 400
        normfiredensityweight = 0.30
        normsdiweight = 0.15
        burnriskweight = 0.25
        normhuweight = 0.3
        lat, lon = coords
        burnprob = getburnprobability(lat, lon)
        harprobability = quantilenormalizer(burnprob, 1000, 50)
        hurisk = gethousingunitrisk(lat, lon)
        normhurisk = quantilenormalizer(hurisk, 700, 0)
        sdi = getsuppressiondifficulty(lat, lon)
        normsdi = normalizesdi(sdi)
        firecount = historicalfiredensity(lat, lon)
        normfiredensity = normalizefirecount(firecount)
        generalweightedrisk = (harprobability * burnriskweight) + (normhurisk * normhuweight) + (normfiredensity * normfiredensityweight) + (normsdi * normsdiweight)
        return jsonify({
            "harprobability": harprobability,
            "normhurisk": normhurisk,
            "normsdi": normsdi,
            "normfiredensity": normfiredensity,
            "generalweightedrisk": generalweightedrisk
        })
    except Exception as e:
        print("Error in /risk-summary:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(port=3000)