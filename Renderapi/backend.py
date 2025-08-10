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

import requests
import geopandas as gpd
import overpy
from shapely.geometry import Point
import rasterio

import numpy as np
from rasterio.windows import from_bounds


#The Final Probability should be a weighted average of the below

#Analyzing min_distance to a fault

def get_faultDis(lat, lon):
    url = "https://earthquake.usgs.gov/arcgis/rest/services/haz/NSHM_Fault_Sources/MapServer/0/query"
    params = {
        "where": "1=1",
        "outFields": "*",
        "f": "geojson"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()  # raise error if request fails
    faults = gpd.read_file(response.url).to_crs(epsg=3310) #returns geojson file
    d = {'geometry': [Point(lon, lat)]}
    pt = gpd.GeoDataFrame(d, crs="EPSG:4326").to_crs(epsg=3310)
    min_dist = faults.geometry.distance(pt.geometry.iloc[0]).min()
    return round(min_dist, 2)

#Analyzing PGA Data
def get_siteClass(lat, lon):

    url = "https://earthquake.usgs.gov/ws/designmaps/metadata.json"
    params = {
        "referenceDocument": "ASCE7-16",
        "latitude": lat,
        "longitude": lon
    }
    response = requests.get(url, params=params)



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

#Getting Risk_Category_Data using OSM Data from the python overpy module
def get_buildingType(lat, lon, length):
    api = overpy.Overpass()
    s = lat - length
    n = lat + length
    w = lon - length
    e = lon + length
#    print(s, n, w, e)
    api = overpy.Overpass()
    query = f"""
    [out:json];
    way["building"]({s},{w},{n},{e});
    out tags center;
    """
    result = api.query(query)
    for way in result.ways:
        return(way.tags.get("building", "n/a"))

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

#print(get_riskCategory(37.239887174704414, -121.89716243138542))

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
    response = requests.get(url, params = params)
    data = response.json()
    hazard_list = data["response"]["data"]["underlyingData"]["pgauh"]
    return hazard_list

#Getting Lhasa Risk
def get_lhasaRisk(lat, lon, delta):
    lhasa_file = r"C:\CGA19\Renderapi\today.tif"

    min_lon = lon - delta
    max_lon = lon + delta
    min_lat = lat - delta
    max_lat = lat + delta

    with rasterio.open(lhasa_file) as src:
        #col, row = src.index(lon, lat)
        raster_bounds = src.bounds
        clipped_min_lon = max(min_lon, raster_bounds.left + 1e-10)
        clipped_max_lon = min(max_lon, raster_bounds.right - 1e-10)
        clipped_min_lat = max(min_lat, raster_bounds.bottom + 1e-10)
        clipped_max_lat = min(max_lat, raster_bounds.top - 1e-10)

        window = from_bounds(clipped_min_lon, clipped_min_lat, clipped_max_lon, clipped_max_lat, src.transform)
        cropped_data = src.read(1, window=window) #Returned the cropped rasterio window
        if cropped_data.size == 0:
            return 0
        clean_data = cropped_data[~np.isnan(cropped_data)]
        max_value = clean_data.max()
        print(max_value)
        # Handle NaN or NoData

        return float(max_value)

#normalization (min-max scaling) function
def square_root_transform(x, y, z):
    vars = np.array([x, y, z])
    print(np.sqrt(vars))
    avg = sum(np.sqrt(vars))/3
    return avg

#use standardization based on the historical data maybe
#Final Functions
def get_earthquake_risk(lat, lon):
    faultDis = 1 / get_faultDis(lat, lon)#normalize this data
    #get_faultDis(lat, lon)
    pgauh = get_pgauh(lat, lon)
    lhasaRisk = get_lhasaRisk(lat, lon, 0.01) #already normalized between 0 and 1
    return square_root_transform(faultDis, pgauh, lhasaRisk)

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
        landslide_risk = get_lhasaRisk(lat, lon, 0.01)
        earthquake_risk = get_earthquake_risk(lat, lon)
        return jsonify({
            "wildfireRisk": "Mock",
            "floodRisk": flood_risk,
            "crimeRate": "Mock",
            "airQualityIndex": aqi,
            "landslideRisk": landslide_risk, 
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
