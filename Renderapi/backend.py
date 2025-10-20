from flask import Blueprint, request, jsonify
import requests
import pyproj
import geopandas as gpd
from shapely.geometry import Point
import numpy as np
import io

bp = Blueprint("backend", __name__)


def get_coordinates(address):
    """Get latitude and longitude from address using Nominatim API"""
    try:
        url = f"https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1"
        response = requests.get(url, headers={"User-Agent": "risk-app"}, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data:
            return None
        return float(data[0]['lat']), float(data[0]['lon'])
    except Exception as e:
        print(f"Error getting coordinates: {e}")
        return None


def get_air_quality(lat, lon):
    """Get air quality index from Open-Meteo API"""
    try:
        url = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&hourly=us_aqi"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'hourly' in data and 'us_aqi' in data['hourly'] and data['hourly']['us_aqi']:
            # Filter out None values and return the first valid one
            aqi_values = [x for x in data['hourly']['us_aqi'] if x is not None]
            return aqi_values[0] if aqi_values else "Unknown"
        return "Unknown"
    except Exception as e:
        print(f"Error getting air quality: {e}")
        return "Unknown"


def get_faultDis(lat, lon):
    """Calculate normalized distance to nearest fault line"""
    try:
        # Transform coordinates to Web Mercator (EPSG:3857)
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
            "geometry": f"{extent['xmin']},{extent['ymin']},{extent['xmax']},{extent['ymax']}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "1=1",
            "outFields": "*",
            "f": "geojson",
            "resultRecordCount": 500
        }

        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        
        # Check if response has content
        if not response.content:
            print("Empty response from USGS fault service")
            return 0

        # Read GeoJSON directly from response content
        faults = gpd.read_file(io.StringIO(response.text))
        
        if faults.empty:
            print("No faults found in the area")
            return 0
            
        # Transform to appropriate projection for distance calculation
        faults = faults.to_crs(epsg=3310)
        pt = gpd.GeoDataFrame({"geometry": [Point(lon, lat)]}, crs="EPSG:4326").to_crs(epsg=3310)

        # Calculate minimum distance
        distances = faults.geometry.distance(pt.geometry.iloc[0])
        min_dist = distances.min()
        
        if min_dist is not None and min_dist > 0:
            # Return normalized value (inverse of distance, capped for very large distances)
            normalized = min(1.0, 1 / (min_dist / 1000))  # Convert to km first
            return round(normalized, 4)
        else:
            return 0

    except Exception as e:
        print(f"Error calculating fault distance: {e}")
        return 0


def get_siteClass(lat, lon):
    """Get site class based on VS30 values"""
    try:
        url = "https://earthquake.usgs.gov/ws/designmaps/metadata.json"
        params = {
            "referenceDocument": "ASCE7-16",
            "latitude": lat,
            "longitude": lon
        }
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if "response" not in data or "data" not in data["response"]:
            return "C"  # Default site class
            
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
        return "C"  # Default to C


def get_buildingType(lat, lon, length):
    """Get building type from OpenStreetMap"""
    try:
        api = overpy.Overpass()
        s = lat - length
        n = lat + length
        w = lon - length
        e = lon + length

        query = f"""
        [out:json][timeout:25];
        way["building"]({s},{w},{n},{e});
        out tags center;
        """
        result = api.query(query)
        
        for way in result.ways:
            building_type = way.tags.get("building")
            if building_type and building_type != "yes":
                return building_type
        
        # If no specific building type found, check for "yes"
        for way in result.ways:
            if way.tags.get("building") == "yes":
                return "yes"
        
        return None
    except Exception as e:
        print(f"Error getting building type: {e}")
        return None


def get_riskCategory(lat, lon):
    """Determine risk category based on building type"""
    try:
        buildingType = get_buildingType(lat, lon, 0.001)  # Increased search radius slightly
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


def get_pgauh(lat, lon):
    """Get Peak Ground Acceleration with uniform hazard"""
    try:
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
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if "response" in data and "data" in data["response"] and "underlyingData" in data["response"]["data"]:
            hazard_list = data["response"]["data"]["underlyingData"].get("pgauh", [])
            return hazard_list if hazard_list else [0.0]
        return [0.0]
    except Exception as e:
        print(f"Error getting PGAUH: {e}")
        return [0.0]


def get_lhasaRisk(lat, lon, pad):
    """Get landslide hazard assessment risk"""
    try:
        lhasa_url = "https://maps.nccs.nasa.gov/mapping/rest/services/landslide_viewer/Landslide_Susceptibility_Update_2023/MapServer"

        url = lhasa_url + "/identify"
        params = {
            "f": "json",
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": "all:0",
            "tolerance": 3,  # Increased tolerance
            "mapExtent": f"{lon-pad},{lat-pad},{lon+pad},{lat+pad}",
            "imageDisplay": "400,400,96",
            "returnGeometry": "false",
        }
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if not data.get("results") or len(data["results"]) == 0:
            print("No landslide data found for location")
            return 0.0

        value = data["results"][0]["attributes"].get("Raster.Value")
        if value is not None:
            return float(value) / 5.0  # Normalize to 0-1 scale
        return 0.0
    except Exception as e:
        print(f"Error getting landslide risk: {e}")
        return 0.0


def safe_sqrt_transform(x, y, z):
    """Safely compute square root transform with error handling"""
    try:
        # Ensure all values are numeric and non-negative
        x = max(0, float(x) if x is not None else 0)
        y = max(0, float(y) if y is not None else 0)  
        z = max(0, float(z) if z is not None else 0)
        
        vars_array = np.array([x, y, z])
        root_vars = np.sqrt(vars_array)
        avg = np.mean(root_vars)
        return float(avg)
    except Exception as e:
        print(f"Error in sqrt transform: {e}")
        return 0.0


def get_earthquake_risk(lat, lon):
    """Calculate comprehensive earthquake risk score"""
    try:
        faultDis = get_faultDis(lat, lon)
        pgauh = get_pgauh(lat, lon)
        lhasaRisk = get_lhasaRisk(lat, lon, 0.01) or 0
        
        # Handle pgauh - could be list or single value
        if isinstance(pgauh, list) and pgauh:
            pgauh_avg = np.mean([x for x in pgauh if x is not None])
        else:
            pgauh_avg = float(pgauh) if pgauh is not None else 0
            
        return safe_sqrt_transform(faultDis, pgauh_avg, lhasaRisk)
    except Exception as e:
        print(f"Error calculating earthquake risk: {e}")
        return 0.0


@bp.route('/risk-summary', methods=['POST'])
def risk_summary():
    try:
        data = request.json
        if not data or 'address' not in data:
            return jsonify({"error": "Address is required"}), 400
            
        address = data.get('address')
        coords = get_coordinates(address)
        
        if not coords:
            return jsonify({"error": "Address not found"}), 400
            
        lat, lon = coords

        # Get all risk components with error handling
        fault_dis = get_faultDis(lat, lon)
        pgauh = get_pgauh(lat, lon)
        site_class = get_siteClass(lat, lon)
        risk_category = get_riskCategory(lat, lon)
        landslide_risk = get_lhasaRisk(lat, lon, 0.01) or 0
        aqi = get_air_quality(lat, lon)
        earthquake_risk = get_earthquake_risk(lat, lon)

        return jsonify({
            "success": True,
            "address": address,
            "coordinates": {"lat": lat, "lon": lon},
            "airQualityIndex": aqi,
            "landslideRisk": landslide_risk,
            "earthquakeRisk": earthquake_risk,
            "faultDistanceNormalized": fault_dis,
            "pgauh": pgauh,
            "landslideRisk": landslide_risk,
            "siteClass": site_class,
            "riskCategory": risk_category
        })

    except Exception as e:
        print(f"Error in /risk-summary: {e}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500