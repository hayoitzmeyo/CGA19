from flask import Blueprint, request, jsonify
import requests

bp = Blueprint("backend", __name__)

def get_coordinates(address):
    url = f"https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1"
    response = requests.get(url, headers={"User-Agent": "risk-app"})
    data = response.json()
    if not data:
        return None
    return float(data[0]['lat']), float(data[0]['lon'])

def get_air_quality(lat, lon):
    url = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&hourly=us_aqi"
    res = requests.get(url)
    data = res.json()
    return data['hourly']['us_aqi'][0] if 'hourly' in data else "Unknown"

def get_flood_risk(lat, lon):
    url = "https://flood-api.open-meteo.com/v1/flood"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "river_discharge"
    }
    response = requests.get(url, params=params)
    data = response.json()
    river_discharge = data['daily']['river_discharge'][0]
    if river_discharge > 200:
        risk = "High"
    elif river_discharge > 100:
        risk = "Moderate"
    else:
        risk = "Low"
    return risk

meta_url = "https://earthquake.usgs.gov/ws/designmaps/metadata.json"
earthquake_url = "https://earthquake.usgs.gov/ws/designmaps/asce7-22.json"
def get_siteClass(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "referenceDocument": "ASCE7-16"
    }
    response = requests.get(meta_url, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} returned for metadata.json: {response.text[:200]}")
    data = response.json()
    vs30 = data["response"]["data"]["vs30"]
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

def get_earthquake_risk(lat, lon, siteClass, riskCategory, title):
    params = {
        "latitude": lat,
        "longitude": lon,
        "siteClass": siteClass,
        "riskCategory": riskCategory,
        "title": title
    }
    response = requests.get(earthquake_url, params = params)
    data = response.json()
    hazard_list = data["response"]["data"]["underlyingData"]["pgauh"]
    return hazard_list

@bp.route('/risk-summary', methods=['POST'])
def risk_summary():
    try:
        data = request.json
        address = data.get('address')
        coords = get_coordinates(address)
        if not coords:
            return jsonify({"error": "Address not found"}), 400
        lat, lon = coords
        aqi = get_air_quality(lat, lon)
        flood_risk = get_flood_risk(lat, lon)
        site_class = get_siteClass(lat, lon)
        earthquake_risk = get_earthquake_risk(lat, lon, site_class, "III", "Risk")
        return jsonify({
            "wildfireRisk": "Mock",
            "floodRisk": flood_risk,
            "crimeRate": "Mock",
            "airQualityIndex": aqi,
            "earthquakeRisk": earthquake_risk,
            "recommendations": [
                "Install smoke detectors",
                "Consider flood insurance",
                "Install a security system",
                "Use air purifiers"
            ]
        })
    except Exception as e:
        print("Error in /risk-summary:", e)
        return jsonify({"error": str(e)}), 500
