from flask import Blueprint, request, jsonify
import requests
import pyproj
import geopandas as gpd
import overpy
from shapely.geometry import Point
import numpy as np

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
    return data.get('hourly', {}).get('us_aqi', [])[0] if 'hourly' in data else "Unknown"

'''
def get_flood_risk(lat, lon):
    url = "https://flood-api.open-meteo.com/v1/flood"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "river_discharge"
    }
    response = requests.get(url, params=params)
    data = response.json()
    river_discharge = data.get('daily', {}).get('river_discharge', [0])
    if river_discharge > 200:
        risk = "High"
    elif river_discharge > 100:
        risk = "Moderate"
    else:
        risk = "Low"
    return risk
'''

def get_faultDis(lat, lon):
    try:
        proj_to_3857 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        x, y = proj_to_3857.transform(lon, lat)

        buffer_m = 500_000

        extent = {
            "type": "extent",
            "xmin": x - buffer_m,
            "ymin": y - buffer_m,
            "xmax": x + buffer_m,
            "ymax": y + buffer_m,
            "spatialReference": {"wkid": 102100, "latestWkid": 3857}
        }

        url = "https://earthquake.usgs.gov/arcgis/rest/services/haz/NSHM_Fault_Sources/MapServer/0/query"
        params = {
            "geometry": extent,
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "1=1",
            "outFields": "*",
            "f": "geojson",
            "resultRecordCount": 500
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        faults = gpd.read_file(response.url).to_crs(epsg=3310)
        pt = gpd.GeoDataFrame({"geometry": [Point(lon, lat)]}, crs="EPSG:4326").to_crs(epsg=3310)

        min_dist = faults.geometry.distance(pt.geometry.iloc[0]).min()
        if min_dist is not None and min_dist > 0:
            return 1 / round(min_dist, 2)
        else:
            return 0

    except Exception:
        return 0


def get_siteClass(lat, lon):
    url = "https://earthquake.usgs.gov/ws/designmaps/metadata.json"
    params = {
        "referenceDocument": "ASCE7-16",
        "latitude": lat,
        "longitude": lon
    }
    response = requests.get(url, params=params)
    data = response.json()
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


def get_buildingType(lat, lon, length):
    api = overpy.Overpass()
    s = lat - length
    n = lat + length
    w = lon - length
    e = lon + length

    query = f"""
    [out:json];
    way["building"]({s},{w},{n},{e});
    out tags center;
    """
    result = api.query(query)
    for way in result.ways:
        return way.tags.get("building", "n/a")
    return None


def get_riskCategory(lat, lon):
    buildingType = get_buildingType(lat, lon, 0.000085)
    if not buildingType:
        return "I"

    buildingType = buildingType.lower()

    essential_facilities = ["hospital", "fire_station", "police"]
    high_risk = ["school", "industrial", "public", "government"]
    moderate_risk = ["commercial", "retail", "warehouse", "hotel", "office", "yes"]
    low_risk = ["residential", "house", "detached", "apartments"]

    if buildingType in essential_facilities:
        return "IV"  # Risk Category IV - Essential Facilities
    elif buildingType in high_risk:
        return "III"  # Risk Category III - High Risk
    elif buildingType in moderate_risk:
        return "II"  # Risk Category II - Standard Risk
    elif buildingType in low_risk:
        return "I"   # Risk Category I - Low Risk
    else:
        return "I"   # Default to lowest if unknown


def get_pgauh(lat, lon):
    siteClass = get_siteClass(lat, lon)
    riskCategory = get_riskCategory(lat, lon)
    url = "https://earthquake.usgs.gov/ws/designmaps/asce7-22.json"
    params = {
        "latitude": lat,
        "longitude": lon,
        "siteClass": siteClass,
        "riskCategory": riskCategory,
        "title": "ASCE7-22"
    }
    response = requests.get(url, params=params)
    data = response.json()
    hazard_list = data["response"]["data"]["underlyingData"]["pgauh"]
    return hazard_list


def get_lhasaRisk(lat, lon, pad):
    lhasa_url = "https://maps.nccs.nasa.gov/mapping/rest/services/landslide_viewer/Landslide_Susceptibility_Update_2023/MapServer"

    url = lhasa_url + "/identify"
    params = {
        "f": "json",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "sr": 4326,
        "layers": "all:0",
        "tolerance": 1,
        "mapExtent": f"{lon-pad},{lat-pad},{lon+pad},{lat+pad}",
        "imageDisplay": "400,400,96",
        "returnGeometry": "false",
    }
    r = requests.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    if not data.get("results"):
        return None

    value = data["results"][0]["attributes"]["Raster.Value"]
    return int(value) / 5


def square_root_transform(x, y, z):
    vars = np.array([x, y, z])
    root_vars = np.sqrt(vars)
    avg = np.mean(root_vars)
    return avg


def get_earthquake_risk(lat, lon):
    faultDis = get_faultDis(lat, lon)
    pgauh = get_pgauh(lat, lon)
    lhasaRisk = get_lhasaRisk(lat, lon, 0.01) or 0  # normalized between 0 and 1, default to 0 if None
    return square_root_transform(faultDis, np.mean(pgauh) if isinstance(pgauh, list) else pgauh, lhasaRisk)


@bp.route('/risk-summary', methods=['POST'])
def risk_summary():
    try:
        data = request.json
        address = data.get('address')
        coords = get_coordinates(address)
        if not coords:
            return jsonify({"error": "Address not found"}), 400
        lat, lon = coords

        fault_dis = get_faultDis(lat, lon)
        pgauh = get_pgauh(lat, lon)
        site_class = get_siteClass(lat, lon)
        risk_category = get_riskCategory(lat, lon)
        landslide_risk = get_lhasaRisk(lat, lon, 0.01) or 0
        aqi = get_air_quality(lat, lon)
        #flood_risk = get_flood_risk(lat, lon)
        earthquake_risk = get_earthquake_risk(lat, lon)

        return jsonify({
            "wildfireRisk": "Mock",
            #"floodRisk": flood_risk,
            "crimeRate": "Mock",
            "airQualityIndex": aqi,
            "landslideRisk": landslide_risk,
            "earthquakeRisk": earthquake_risk,
            "earthquakeParameters": {
                "faultDistanceNormalized": fault_dis,
                "pgauh": pgauh,
                "lhasaRisk": landslide_risk,
                "siteClass": site_class,
                "riskCategory": risk_category
            }
        })

    except Exception as e:
        print("Error in /risk-summary:", e)
        return jsonify({"error": str(e)}), 500
