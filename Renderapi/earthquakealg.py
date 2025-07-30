import requests
import geopandas as gpd
import overpy
from shapely.geometry import Point



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
    d = {'geometry': [Point(lat, lon)]}
    pt = gpd.GeoDataFrame(d, crs="EPSG:4326").to_crs(epsg=3310)
    min_dist = faults.distance(pt.geometry.iloc[0]).min()
    return round(min_dist, 2)

#Analyzing PGA Data
def get_siteClass(lat, lon):
    meta_url = "https://earthquake.usgs.gov/ws/designmaps/metadata.json"
    params = {
        "latitude": lat,
        "longitude": lon,
        "referenceDocument": "ASCE7-16"
    }
    response = requests.get(meta_url, params=params)
    print(response.status_code)
    #if response.status_code != 200:
    #    raise RuntimeError(f"HTTP {response.status_code} returned for metadata.json: {response.text[:200]}")
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
        return "Low"

    buildingType = buildingType.lower()

    high_risk = {"hospital", "school", "fire_station", "police", "industrial", "public", "government"}
    moderate_risk = {"commercial", "retail", "warehouse", "hotel", "office", "yes"}  # 'yes' generic assigned moderate
    low_risk = {"residential", "house", "detached", "apartments"}

    if buildingType in high_risk:
        return "High"
    elif buildingType in moderate_risk:
        return "Moderate"
    elif buildingType in low_risk:
        return "Low"
    else:
        return "Low"
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
        #"title": title
    }
    response = requests.get(url, params = params)
    data = response.json()
    hazard_list = data["response"]["data"]["underlyingData"]["pgauh"]
    return hazard_list

#Getting Lhasa Risk
lhasa_file = "today.tif"
def get_lhasaRisk(lat, lon):
    with rasterio.open(lhasa_file) as src:
        col, row= src.index(lon, lat)
        value = src.read(1)[row, col]
        # Handle NaN or NoData
        if np.isnan(value):
            return 0
        else:
            return value

#Final Functions
def get_earthquake_risk(lat, lon):
    faultDis = get_faultDis(lat, lon)
    pgauh = get_pgauh(lat, lon)
    lhasaRisk = get_lhasaRisk(lat, lon)







