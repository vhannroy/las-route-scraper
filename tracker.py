import pygame
import requests
import hashlib
import os
import csv
import time
import sys
from datetime import datetime, timedelta
from math import radians, cos, sin, atan2, degrees, sqrt
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- CRITICAL CONFIG ---
csv.field_size_limit(10000000)

# --- CONFIGURATION ---
RES = (480, 320)
BG_COLOR = (0, 0, 0)
CYAN = (0, 255, 255)
YELLOW = (255, 255, 0)
WHITE = (255, 255, 255)
GRAY = (150, 150, 150)
RED = (255, 50, 50)
GREEN = (0, 255, 0)
BASE_DIR = "/home/vhannroy/flight_tracker"  # Adjust if not on Raspberry Pi
AIRCRAFT_CSV = f"{BASE_DIR}/aircraft.csv"
FONT_PATH = f"{BASE_DIR}/Verdana-Bold.ttf"
FONT_PATH_REGULAR = f"{BASE_DIR}/Verdana.ttf"  # Assuming you have a regular version; adjust if needed
LOGO_DIR = f"{BASE_DIR}/fins"

# LAS airport coordinates for distance/heading calculations
LAS_LAT = 36.080
LAS_LON = -115.152

# KHND (Henderson) and KVGT (North Las Vegas) coordinates
KHND_LAT = 35.976
KHND_LON = -115.134
KVGT_LAT = 36.211
KVGT_LON = -115.195

# Runway threshold coordinates (approximate, in degrees)
RUNWAY_THRESHOLDS = {
    "01L": (36.058, -115.158),
    "01R": (36.058, -115.145),
    "08L": (36.069, -115.178),
    "08R": (36.069, -115.165),
    "19L": (36.101, -115.139),
    "19R": (36.101, -115.152),
    "26L": (36.092, -115.126),
    "26R": (36.092, -115.139),
}

# Common airline IATA to ICAO mappings for LAS
IATA_TO_ICAO = {
    'WN': 'SWA',  # Southwest
    'DL': 'DAL',  # Delta
    'AA': 'AAL',  # American
    'UA': 'UAL',  # United
    'AS': 'ASA',  # Alaska
    'F9': 'FFT',  # Frontier
    'NK': 'NKS',  # Spirit
    'B6': 'JBU',  # JetBlue
    'G4': 'AAY',  # Allegiant
    # Add more if needed
}

# Full airline name to IATA mappings
FULL_NAME_TO_IATA = {
    'AMERICAN AIRLINES': 'AA',
    'SOUTHWEST AIRLINES': 'WN',
    'DELTA AIR LINES': 'DL',
    'UNITED AIRLINES': 'UA',
    'ALASKA AIRLINES': 'AS',
    'FRONTIER AIRLINES': 'F9',
    'SPIRIT AIRLINES': 'NK',
    'JETBLUE AIRWAYS': 'B6',
    'ALLEGIANT AIR': 'G4',
    # Add more common ones at LAS
    'HAWAIIAN AIRLINES': 'HA',
    'VIRGIN AMERICA': 'VX',
    'WESTJET': 'WS',
    'AIR CANADA': 'AC',
    'VOLARIS': 'Y4',
    'INTERJET': '4O',
    'BRITISH AIRWAYS': 'BA',
    'LUFTHANSA': 'LH',
    'EDELWEISS AIR': 'WK'
}

# Ensure directories exist
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(LOGO_DIR, exist_ok=True)

# Initialize Pygame
pygame.init()
screen = pygame.display.set_mode(RES, pygame.FULLSCREEN)
pygame.mouse.set_visible(False)
clock = pygame.time.Clock()  # Added for better timing control

current_view = "BOARD"
selected_flight = None
detail_start_time = 0

# --- FONT LOADER ---
try:
    font_bold = pygame.font.Font(FONT_PATH, 14)
    font_large = pygame.font.Font(FONT_PATH, 32)
    font_small = pygame.font.Font(FONT_PATH_REGULAR, 11)
except:
    print("Custom fonts not found. Falling back to system fonts.")
    font_bold = pygame.font.SysFont("Arial", 14, bold=True)
    font_large = pygame.font.SysFont("Arial", 32, bold=True)
    font_small = pygame.font.SysFont("Arial", 11)

# Global caches
aircraft_db = {}
route_cache = {}
last_route_fetch = 0
ROUTE_REFRESH_SECONDS = 300  # 5 minutes

def super_clean(val):
    if not val:
        return ""
    return str(val).replace('"', '').replace("'", "").strip().upper()

def load_databases():
    if os.path.exists(AIRCRAFT_CSV):
        try:
            with open(AIRCRAFT_CSV, mode='r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                reader.fieldnames = [super_clean(h) for h in reader.fieldnames]
                for row in reader:
                    icao = super_clean(row.get('ICAO24') or row.get('ADDRESS'))
                    model = super_clean(row.get('TYPECODE') or row.get('MODEL'))
                    if icao and model:
                        aircraft_db[icao.lower()] = model
            print(f"Loaded {len(aircraft_db)} aircraft from DB.")
        except Exception as e:
            print(f"Aircraft DB Error: {e}")
    else:
        print(f"Aircraft CSV not found at {AIRCRAFT_CSV}")

load_databases()

def parse_time(time_str, current_date):
    try:
        # FlightAware format like "4:20p" or "4:20p *"
        time_str = time_str.replace('*', '').strip()
        # Convert "p" to " PM", "a" to " AM"
        if time_str.endswith('p'):
            time_str = time_str.replace('p', ' PM')
        elif time_str.endswith('a'):
            time_str = time_str.replace('a', ' AM')
        scheduled_dt = datetime.strptime(f"{current_date.date()} {time_str}", "%Y-%m-%d %I:%M %p")
        return scheduled_dt, None  # No estimated in FlightAware; use scheduled
    except ValueError:
        return None, None

def parse_status(status_str):
    if "Departed at" in status_str:
        try:
            time_str = status_str.split("at")[-1].strip()
            current_date = datetime.now()
            actual_dt = datetime.strptime(f"{current_date.date()} {time_str}", "%Y-%m-%d %I:%M %p")
            return actual_dt
        except ValueError:
            return None
    return None

def fetch_airport_routes():
    global route_cache
    urls = {
        'arrival': 'https://www.flightaware.com/live/airport/KLAS/arrivals',
        'departure': 'https://www.flightaware.com/live/airport/KLAS/departures'
    }
    # Set up headless Firefox
    options = Options()
    options.add_argument("--headless")
    service = Service(executable_path='/usr/local/bin/geckodriver')
    driver = webdriver.Firefox(service=service, options=options)
    
    current_time = datetime.now()
    new_routes = {}  # Temp dict for new scraped routes
    
    for scope, url in urls.items():
        try:
            driver.get(url)
            # Wait for initial table to load
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CLASS_NAME, "prettyTable")))
            
            html = driver.page_source
            soup = BeautifulSoup(html, 'html.parser')
            table = soup.find('table', class_='prettyTable')
            if not table:
                print(f"No table found for {scope} after rendering")
                continue
            print(f"Table found for {scope}")
            rows = table.find_all('tr')[1:]  # Skip header
            print(f"Found {len(rows)} rows for {scope}")
            for row in rows:
                tds = row.find_all('td')
                if len(tds) < 7:
                    continue
                # Columns on FlightAware: 0: Ident, 1: Type, 2: From/Origin, 3: Depart, 4: Arrive, 5: Speed, 6: Altitude, 7: Status (approximate)
                ident = tds[0].text.strip()
                from_to = tds[2].text.strip()
                depart_time = tds[3].text.strip()
                arrive_time = tds[4].text.strip()
                status_text = tds[7].text.strip() if len(tds) > 7 else ""
                # Parse callsign from Ident (e.g., "AAL1871" is ICAO-like)
                callsign = ident.upper()
                # Parse airline from callsign
                airline_iata = callsign[:2]
                airline_icao = IATA_TO_ICAO.get(airline_iata, airline_iata[:3])
                # Parse iata from from_to (e.g., "Dallas-Fort Worth Intl (DFW)" -> "DFW")
                iata = "---"
                if '(' in from_to and ')' in from_to:
                    iata = from_to.split(' (')[-1].strip(')')
                # Time text depends on scope
                time_text = arrive_time if scope == 'arrival' else depart_time
                scheduled_dt, estimated_dt = parse_time(time_text, current_time)
                if not scheduled_dt:
                    print(f"Time parsing failed for {callsign} in {scope}: {time_text}")
                    continue
                
                actual_dt = parse_status(status_text)
                
                use_time = estimated_dt if estimated_dt else scheduled_dt
                
                # For arrivals: Cache if within next 60 min
                if scope == 'arrival':
                    if current_time <= use_time <= current_time + timedelta(minutes=60):
                        new_routes[callsign] = {
                            "origin": iata,
                            "dest": 'LAS',
                            "airline_iata": airline_iata,
                            "airline_icao": airline_icao,
                            "scheduled_time": scheduled_dt,
                            "actual_time": estimated_dt
                        }
                        print(f"Cached flight {callsign} (arrival) with origin={iata}, use_time={use_time}")
                    else:
                        print(f"Flight {callsign} not cached (arrival): use_time={use_time} not within range (now={current_time}, +60min={current_time + timedelta(minutes=60)})")
                
                # For departures: Cache if within next 60 min
                if scope == 'departure':
                    if current_time <= use_time <= current_time + timedelta(minutes=60):
                        new_routes[callsign] = {
                            "origin": 'LAS',
                            "dest": iata,
                            "airline_iata": airline_iata,
                            "airline_icao": airline_icao,
                            "scheduled_time": scheduled_dt,
                            "actual_time": actual_dt if "Departed" in status_text else None
                        }
                        print(f"Cached flight {callsign} (departure) with dest={iata}, use_time={use_time}")
                    else:
                        print(f"Flight {callsign} not cached (departure): use_time={use_time} not within range (now={current_time}, +60min={current_time + timedelta(minutes=60)})")
            print(f"Scraped {len(rows)} {scope}s, added {len(new_routes)} new routes")
        except Exception as e:
            print(f"Scrape error for {scope}: {e}")
    driver.quit()
    
    # Update cache: Add new, remove expired
    route_cache.update(new_routes)
    to_remove = []
    for callsign, info in route_cache.items():
        if 'scheduled_time' not in info:
            to_remove.append(callsign)
            continue
        use_time = info.get('actual_time') or info['scheduled_time']
        if use_time < current_time:
            to_remove.append(callsign)
        elif info.get('actual_time'):
            if info['actual_time'] + timedelta(minutes=30) < current_time:
                to_remove.append(callsign)
    for cs in to_remove:
        route_cache.pop(cs, None)

def get_live_route(callsign):
    callsign = callsign.strip().upper()
    if not callsign:
        return {"origin": "---", "dest": "---", "airline_iata": "---", "airline_icao": "---"}
    if callsign in route_cache:
        return route_cache[callsign]
    # Fallback if not in cache
    print(f"No scraped route for {callsign}, using fallback")
    return {"origin": "---", "dest": "---", "airline_iata": callsign[:3], "airline_icao": callsign[:3]}

def get_aircraft_type(icao, callsign):
    atype = aircraft_db.get(icao.lower())
    if atype:
        return atype
    if callsign.startswith("SWA"):
        return "B737"
    return "JET"  # Default

def is_ignored_type(atype):
    atype = atype.upper()
    ignored_types = {
        # Helicopters
        'A109', 'A119', 'A129', 'A139', 'A169', 'A189', 'AS32', 'AS35', 'AS55', 'AS65', 'B105', 'B407', 'B412', 'B427', 'B429', 'B430', 'EC20', 'EC25', 'EC30', 'EC35', 'EC45', 'EC55', 'H47', 'H60', 'MD52', 'MD60', 'MD90', 'R22', 'R44', 'R66', 'S76', 'S92', 'B505', 'H125', 'H130', 'H135', 'H145', 'H160', 'H175', 'H225',
        # Light aircraft
        'C150', 'C152', 'C172', 'C182', 'C206', 'C208', 'C210', 'C310', 'C337', 'P28A', 'P28R', 'P32R', 'P46T', 'BE33', 'BE35', 'BE36', 'BE55', 'BE58', 'M20P', 'M20T', 'SR20', 'SR22', 'DA40', 'DA42', 'DA62', 'PC6T', 'PC12', 'TBM7', 'TBM8', 'TBM9', 'SF50', 'PA46', 'PA34', 'PA31', 'BN2P', 'GA8', 'DHC2', 'DHC3', 'DHC6',
        # Added for user-reported small aircraft
        'C421', 'T206'
    }
    if atype in ignored_types:
        return True
    # Additional prefixes for helicopters and light aircraft
    heli_prefixes = ('A1', 'AS', 'B4', 'EC', 'H1', 'H2', 'MD', 'R2', 'R4', 'R6', 'S7', 'S9', 'H')
    light_prefixes = ('C1', 'C2', 'P2', 'SR', 'BE', 'M20', 'DA', 'PC', 'TBM', 'PA', 'BN2', 'GA', 'DHC', 'C4', 'T2')
    if atype.startswith(heli_prefixes) or atype.startswith(light_prefixes):
        return True
    return False

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in km
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c  # Return in km

def calculate_bearing(lat1, lon1, lat2, lon2):
    dlon = radians(lon2 - lon1)
    y = sin(dlon) * cos(radians(lat2))
    x = cos(radians(lat1)) * sin(radians(lat2)) - sin(radians(lat1)) * cos(radians(lat2)) * cos(dlon)
    return (degrees(atan2(y, x)) + 360) % 360

def is_approach_to_las(lat, lon, heading, v_rate, alt_ft):
    distance = calculate_distance(lat, lon, LAS_LAT, LAS_LON)
    bearing_to_las = calculate_bearing(lat, lon, LAS_LAT, LAS_LON)
    heading_diff = abs((heading - bearing_to_las + 180) % 360 - 180)
    # Simplified: descending and somewhat towards LAS or close in
    if v_rate < 0 and alt_ft < 15000 and (distance < 60 or heading_diff < 120):
        return True
    return False

def is_departure_from_las(lat, lon, heading, v_rate, alt_ft):
    distance = calculate_distance(lat, lon, LAS_LAT, LAS_LON)
    if distance < 30 and v_rate > 0 and alt_ft < 10000:  # Within 30 NM, climbing, below 10k ft
        return True
    return False

def get_runway(lat, lon, hdg, alt_ft):
    try:
        lat, lon, hdg, alt_ft = float(lat), float(lon), float(hdg) % 360, float(alt_ft)
        distance = calculate_distance(lat, lon, LAS_LAT, LAS_LON)
        if distance > 40 or alt_ft >= 7000:
            return ""
        bearing_to_las = calculate_bearing(lat, lon, LAS_LAT, LAS_LON)
        heading_diff = abs((hdg - bearing_to_las + 180) % 360 - 180)
        if heading_diff >= 90:
            return ""
        # Find closest runway threshold
        min_diff = float('inf')
        predicted = ""
        for rwy, (r_lat, r_lon) in RUNWAY_THRESHOLDS.items():
            r_bearing = calculate_bearing(lat, lon, r_lat, r_lon)
            r_diff = abs((hdg - r_bearing + 180) % 360 - 180)
            if r_diff < min_diff:
                min_diff = r_diff
                predicted = rwy
        return predicted
    except ValueError:
        pass
    return ""

def get_airhex_logo(callsign, route_info):
    airline_icao = route_info.get("airline_icao")
    airline_iata = route_info.get("airline_iata")
    if airline_icao and airline_icao != "---":
        prefix = airline_icao.upper()
    elif callsign[:3]:
        prefix = callsign[:3].upper()
    else:
        return None
    if len(prefix) < 3:
        return None
    path = f"{LOGO_DIR}/{prefix}.png"
    if os.path.exists(path):
        return path
    return None

def fetch_opensky():
    try:
        # Extended bounding box for LAS area, farther east to cover Lake Mead
        params = {'lamin': 35.8, 'lamax': 36.3, 'lomin': -115.4, 'lomax': -114.0}
        r = requests.get("https://opensky-network.org/api/states/all", params=params, timeout=5)
        r.raise_for_status()
        return r.json().get('states', [])
    except requests.RequestException as e:
        print(f"OpenSky API error: {e}")
        return []

def draw_detail_view(flight_data, ac_type, route_info, is_arrival):
    screen.fill(BG_COLOR)
    call = flight_data[1].strip()
    alt = int((flight_data[7] or 0) * 3.2808)
    spd = int((flight_data[9] or 0) * 1.9438)
    hdg = flight_data[10] or 0
    try:
        hdg = int(float(hdg))
    except ValueError:
        hdg = 0
    rwy = get_runway(flight_data[6], flight_data[5], hdg, flight_data[7])
    
    # Title
    screen.blit(font_large.render(call, True, WHITE), (20, 20))
    pygame.draw.line(screen, GRAY, (20, 70), (460, 70), 2)
    
    # Logo
    logo_path = get_airhex_logo(call, route_info)
    if logo_path:
        try:
            logo_img = pygame.image.load(logo_path)
            logo_img = pygame.transform.scale(logo_img, (120, 120))
            screen.blit(logo_img, (20, 100))
        except pygame.error as e:
            pass
    
    # Details
    screen.blit(font_bold.render(f"AIRCRAFT: {ac_type}", True, CYAN), (160, 100))
    if is_arrival:
        screen.blit(font_bold.render(f"FROM: {route_info['origin']}", True, YELLOW), (160, 130))
    else:
        screen.blit(font_bold.render(f"TO: {route_info['dest']}", True, YELLOW), (160, 130))
    screen.blit(font_bold.render(f"ALTITUDE: {alt} FT", True, WHITE), (160, 160))
    screen.blit(font_bold.render(f"SPEED: {spd} KTS", True, WHITE), (160, 190))
    screen.blit(font_bold.render(f"HEADING: {hdg}°", True, WHITE), (160, 220))
    if rwy:
        screen.blit(font_bold.render(f"PREDICTED: RWY {rwy}", True, GREEN), (160, 260))
    
    # Close button
    btn_rect = pygame.Rect(350, 260, 110, 45)
    pygame.draw.rect(screen, RED, btn_rect, border_radius=8)
    screen.blit(font_bold.render("CLOSE", True, WHITE), (375, 272))
    return btn_rect

# --- MAIN LOOP ---
known_arrivals = set()
known_departures = set()
exit_rect = pygame.Rect(465, 0, 15, 15)  # Tiny exit button
last_fetch_time = 0
raw_data = []
current_arrivals = []
current_departures = []

while True:
    # Dynamic refresh based on time of day
    now = datetime.now()
    h = now.hour
    if (6 <= h < 10) or (16 <= h < 21):
        refresh_seconds, dot_color = 20, GREEN  # Peak
    elif (10 <= h < 16) or (21 <= h < 24):
        refresh_seconds, dot_color = 20, YELLOW  # Mid-Day
    else:
        refresh_seconds, dot_color = 90, GRAY  # Night
    # Fetch routes if needed
    if time.time() - last_route_fetch >= ROUTE_REFRESH_SECONDS:
        fetch_airport_routes()
        last_route_fetch = time.time()
    # Fetch data if needed
    if time.time() - last_fetch_time >= refresh_seconds:
        raw_data = fetch_opensky()
        active_ids = {f[0] for f in raw_data if f}
        known_arrivals &= active_ids
        known_departures &= active_ids
        
        current_arrivals.clear()
        current_departures.clear()
        for f in raw_data:
            if not f or len(f) < 13:
                print("Skipped invalid data (length < 13)")
                continue
            icao = f[0]
            call = f[1].strip().upper()
            if not call:
                print("Skipped no callsign")
                continue
            
            # Filters
            spd_kt = (f[9] or 0) * 1.9438
            alt_ft = (f[7] or 0) * 3.2808
            v_rate = f[11] or 0
            lat = float(f[6] or 0)
            lon = float(f[5] or 0)
            heading = float(f[10] or 0)
            
            if spd_kt < 130 or alt_ft > 15000:
                print(f"Skipped {call} due to speed {spd_kt:.1f} < 130 or alt {alt_ft:.0f} > 15000")
                continue
            
            atype = get_aircraft_type(icao, call)
            
            if is_ignored_type(atype):
                print(f"Skipped {call} due to ignored type {atype}")
                continue
            
            # Get route from scrape
            route_info = get_live_route(call)
            origin = route_info['origin']
            dest = route_info['dest']
            
            # Primary classification: Prefer route if available, else OpenSky heuristics
            if dest in ["LAS", "KLAS"]:
                is_arrival = True
            else:
                is_arrival = is_approach_to_las(lat, lon, heading, v_rate, alt_ft) or (v_rate < -0.5 and alt_ft < 12000 and dest == "---")
            
            if origin in ["LAS", "KLAS"]:
                is_departure = True
            else:
                is_departure = is_departure_from_las(lat, lon, heading, v_rate, alt_ft) or (v_rate > 0.5 and alt_ft < 10000 and origin == "---")
            
            # Exclude likely VGT arrivals: low altitude north of LAS
            if is_arrival and lat > 36.15 and alt_ft < 6000:
                print(f"Skipped {call} due to likely VGT: lat {lat} > 36.15 and alt {alt_ft:.0f} < 6000")
                continue
            
            # Exclude flights likely over/approaching KHND or KVGT (low descending near them, not LAS route)
            distance_to_khnd = calculate_distance(lat, lon, KHND_LAT, KHND_LON)
            distance_to_kvgt = calculate_distance(lat, lon, KVGT_LAT, KVGT_LON)
            if v_rate < 0 and alt_ft < 5000 and (distance_to_khnd < 5 or distance_to_kvgt < 5) and dest not in ["LAS", "KLAS"]:
                print(f"Skipped {call} due to likely KHND/KVGT: dist_khnd {distance_to_khnd:.1f} NM, dist_kvgt {distance_to_kvgt:.1f} NM, v_rate {v_rate:.2f}, alt {alt_ft:.0f}")
                continue
            
            if is_arrival:
                current_arrivals.append((f, atype, route_info, alt_ft))
                known_arrivals.add(icao)
            elif is_departure:
                current_departures.append((f, atype, route_info, alt_ft))
                known_departures.add(icao)
            else:
                print(f"Skipped {call} - not classified (v_rate: {v_rate:.2f}, alt: {alt_ft:.0f}, distance: {calculate_distance(lat, lon, LAS_LAT, LAS_LON):.1f} NM, heading_diff: {abs((heading - calculate_bearing(lat, lon, LAS_LAT, LAS_LON) + 180) % 360 - 180):.1f} deg)")
        
        # Sort arrivals by altitude ascending (closest to landing first)
        current_arrivals.sort(key=lambda x: x[3])
        
        # Sort departures by altitude ascending (most recent takeoff first)
        current_departures.sort(key=lambda x: x[3])
        
        last_fetch_time = time.time()

    # Drawing
    screen.fill(BG_COLOR)
    if current_view == "BOARD":
        flight_rects = []
        screen.blit(font_bold.render("LAS ARRIVALS", True, CYAN), (10, 10))
        screen.blit(font_bold.render("LAS DEPARTURES", True, YELLOW), (250, 10))
        pygame.draw.circle(screen, dot_color, (470, 310), 4)
        
        def draw_sec(flights, x_start, color, is_arr):
            for i, pkg in enumerate(flights[:4]):  # Limit to 4 per side
                f_data, t_code, route_info, _ = pkg
                y = 45 + (i * 68)
                call = f_data[1].strip()
                alt = int((f_data[7] or 0) * 3.2808)
                spd = int((f_data[9] or 0) * 1.9438)
                route_label = route_info['origin'] if is_arr else route_info['dest']
                rwy = get_runway(f_data[6], f_data[5], f_data[10], f_data[7]) if is_arr else ""
                
                rect = pygame.Rect(x_start, y, 230, 65)
                flight_rects.append((rect, f_data, t_code, route_info, is_arr))
                
                # Logo
                logo_path = get_airhex_logo(call, route_info)
                if logo_path:
                    try:
                        logo_img = pygame.image.load(logo_path)
                        logo_img = pygame.transform.scale(logo_img, (40, 40))
                        screen.blit(logo_img, (x_start, y + 5))
                    except pygame.error as e:
                        pass
                
                # Text
                screen.blit(font_bold.render(f"{call} | {t_code}", True, color), (x_start + 45, y))
                label = "FRM: " if is_arr else "TO: "
                screen.blit(font_small.render(f"{label}{route_label}", True, WHITE), (x_start + 45, y + 20))
                if rwy:
                    screen.blit(font_small.render(f"RWY {rwy}", True, GREEN), (x_start + 115, y + 20))
                screen.blit(font_small.render(f"{alt}ft {spd}kt", True, GRAY), (x_start + 45, y + 36))
        
        draw_sec(current_arrivals, 5, CYAN, True)
        draw_sec(current_departures, 245, YELLOW, False)
    
    elif current_view == "DETAIL":
        active_close_btn = draw_detail_view(selected_flight[0], selected_flight[1], selected_flight[2], selected_flight[3])
        if time.time() - detail_start_time > 30:
            current_view = "BOARD"
            selected_flight = None
    
    pygame.display.flip()
    
    # Event handling
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            pygame.quit()
            sys.exit()
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:  # Added ESC to quit
                pygame.quit()
                sys.exit()
        if event.type == pygame.MOUSEBUTTONDOWN:
            pos = pygame.mouse.get_pos()
            if exit_rect.collidepoint(pos):
                pygame.quit()
                sys.exit()
            if current_view == "BOARD":
                for rect, data, typ, rte, is_arr in flight_rects:
                    if rect.collidepoint(pos):
                        selected_flight = (data, typ, rte, is_arr)
                        current_view = "DETAIL"
                        detail_start_time = time.time()
                        break
            elif current_view == "DETAIL" and active_close_btn.collidepoint(pos):
                current_view = "BOARD"
                selected_flight = None
    
    clock.tick(10)  # Limit to 10 FPS to save CPU