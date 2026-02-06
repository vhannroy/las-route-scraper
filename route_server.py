import threading
import time
import os
from datetime import datetime, timedelta
from flask import Flask, jsonify
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import re
import geckodriver_autoinstaller

# Auto-install geckodriver (for cloud)
geckodriver_autoinstaller.install()

app = Flask(__name__)

# Configurations (copied from your script for consistency)
IATA_TO_ICAO = {
    'WN': 'SWA', 'DL': 'DAL', 'AA': 'AAL', 'UA': 'UAL', 'AS': 'ASA',
    'F9': 'FFT', 'NK': 'NKS', 'B6': 'JBU', 'G4': 'AAY',
    # Add more if needed
}
FULL_NAME_TO_IATA = {
    'AMERICAN AIRLINES': 'AA', 'SOUTHWEST AIRLINES': 'WN', 'DELTA AIR LINES': 'DL',
    'UNITED AIRLINES': 'UA', 'ALASKA AIRLINES': 'AS', 'FRONTIER AIRLINES': 'F9',
    'SPIRIT AIRLINES': 'NK', 'JETBLUE AIRWAYS': 'B6', 'ALLEGIANT AIR': 'G4',
    # Add more common ones at LAS
    'HAWAIIAN AIRLINES': 'HA', 'VIRGIN AMERICA': 'VX', 'WESTJET': 'WS',
    'AIR CANADA': 'AC', 'VOLARIS': 'Y4', 'INTERJET': '4O',
    'BRITISH AIRWAYS': 'BA', 'LUFTHANSA': 'LH', 'EDELWEISS AIR': 'WK'
}

route_cache = {}
ROUTE_REFRESH_SECONDS = 300  # 5 minutes

def parse_time(time_str, current_date):
    try:
        time_parts = time_str.split(' / ')
        scheduled_str = time_parts[0].strip()
        estimated_str = time_parts[1].strip() if len(time_parts) > 1 else None
        scheduled_dt = datetime.strptime(f"{current_date.date()} {scheduled_str}", "%Y-%m-%d %I:%M %p")
        estimated_dt = None
        if estimated_str:
            estimated_dt = datetime.strptime(f"{current_date.date()} {estimated_str}", "%Y-%m-%d %I:%M %p")
        return scheduled_dt, estimated_dt
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

def scrape_airport_routes():
    global route_cache
    urls = {
        'arrival': 'https://www.harryreidairport.com/flights?scope=arrival',
        'departure': 'https://www.harryreidairport.com/flights?scope=departure'
    }
    options = Options()
    options.add_argument("--headless")  # Run headless for cloud
    service = Service()
    driver = webdriver.Firefox(service=service, options=options)

    current_time = datetime.now()
    new_routes = {}

    for scope, url in urls.items():
        try:
            driver.get(url)

            # Handle cookies prompt if present
            try:
                accept_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, '//button[contains(text(), "Allow all") or contains(text(), "Accept all") or contains(text(), "Allow All") or contains(text(), "Accept") or contains(text(), "Agree") or contains(text(), "Accept All Cookies")]'))
                )
                accept_button.click()
                print(f"Clicked cookies accept button for {scope}")
                time.sleep(2)  # Let page settle
                driver.execute_script("window.scrollTo(0, 0);")  # Reset to top
            except Exception as e:
                print(f"No cookies prompt or click failed for {scope}: {e}")

            # Wait for data row to appear
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CLASS_NAME, 'FlightsListTable_row__f7EKZ'))
                )
                time.sleep(7)  # Additional time for data to fill
            except Exception as wait_e:
                print(f"Data row wait failed for {scope}: {wait_e}")
                print("Debug: Page source snippet after wait failure:")
                print(driver.page_source[:1000])
                continue

            # Parse the initial/current page (no scroll to bottom)
            html = driver.page_source
            with open(f'{scope}.html', 'w', encoding='utf-8') as f:
                f.write(html)
            print(f"Saved full HTML to {scope}.html for inspection")
            soup = BeautifulSoup(html, 'html.parser')

            table = soup.find('div', class_='FlightsListTable_table__OXs2M')
            if not table:
                print(f"No table found for {scope}")
                continue

            rows = table.find_all('a', class_=lambda c: c and 'FlightsListTable_row__' in c)
            print(f"Scraped {len(rows)} {scope}s from initial page")

            for row in rows:
                cells = row.find_all('div', class_=lambda c: c and 'FlightsListTable_' in c and '-cell__' in c)
                data = []
                for cell in cells:
                    img = cell.find('img')
                    if img and 'alt' in img.attrs and img['alt']:
                        data.append(img['alt'])
                    else:
                        text = cell.text.strip().replace('\n', ' / ')
                        data.append(text)

                if len(data) < 7:
                    continue

                time_text = data[0]
                city_text = data[1] if scope == 'arrival' else data[1]  # For both, first is origin/dest string
                airline_name = data[2]
                status_text = data[6]

                match = re.match(r'(.*)\ \((.*)\)(.*)', city_text)
                if not match:
                    continue

                city = match.group(1).strip()
                iata = match.group(2).strip()
                flight_num = match.group(3).strip()

                airline_iata = FULL_NAME_TO_IATA.get(airline_name.upper(), flight_num[:2].upper())
                airline_icao = IATA_TO_ICAO.get(airline_iata, airline_iata[:3])

                num = flight_num[len(airline_iata):]
                callsign = airline_icao + num

                scheduled_dt, estimated_dt = parse_time(time_text, current_time)
                if not scheduled_dt:
                    continue

                actual_dt = parse_status(status_text)

                use_time = estimated_dt if estimated_dt else scheduled_dt

                if current_time <= use_time <= current_time + timedelta(minutes=60):
                    new_routes[callsign] = {
                        "origin": iata if scope == 'arrival' else 'LAS',
                        "dest": 'LAS' if scope == 'arrival' else iata,
                        "airline_iata": airline_iata,
                        "airline_icao": airline_icao,
                        "scheduled_time": scheduled_dt.isoformat(),
                        "actual_time": estimated_dt.isoformat() if estimated_dt else actual_dt.isoformat() if actual_dt else None
                    }
                else:
                    print(f"Flight {callsign} time not within range for {scope}")

            # Try to load and scrape "Later Flights"
            try:
                later_button = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable((By.XPATH, '//button[div[contains(text(), "Show Later Flights")]]'))
                )
                later_button.location_once_scrolled_into_view
                time.sleep(2)  # Stabilize after scroll
                later_button.click()
                time.sleep(3)

                html = driver.page_source
                with open(f'{scope}_later.html', 'w', encoding='utf-8') as f:
                    f.write(html)
                print(f"Saved full HTML after later flights to {scope}_later.html")
                soup = BeautifulSoup(html, 'html.parser')

                table = soup.find('div', class_='FlightsListTable_table__OXs2M')
                if table:
                    rows = table.find_all('a', class_=lambda c: c and 'FlightsListTable_row__' in c)
                    print(f"Scraped additional {len(rows)} {scope}s after Later Flights")
                    for row in rows:
                        cells = row.find_all('div', class_=lambda c: c and 'FlightsListTable_' in c and '-cell__' in c)
                        data = []
                        for cell in cells:
                            img = cell.find('img')
                            if img and 'alt' in img.attrs and img['alt']:
                                data.append(img['alt'])
                            else:
                                text = cell.text.strip().replace('\n', ' / ')
                                data.append(text)

                        if len(data) < 7:
                            continue

                        time_text = data[0]
                        city_text = data[1] if scope == 'arrival' else data[1]  # For both, first is origin/dest string
                        airline_name = data[2]
                        status_text = data[6]

                        match = re.match(r'(.*)\ \((.*)\)(.*)', city_text)
                        if not match:
                            continue

                        city = match.group(1).strip()
                        iata = match.group(2).strip()
                        flight_num = match.group(3).strip()

                        airline_iata = FULL_NAME_TO_IATA.get(airline_name.upper(), flight_num[:2].upper())
                        airline_icao = IATA_TO_ICAO.get(airline_iata, airline_iata[:3])

                        num = flight_num[len(airline_iata):]
                        callsign = airline_icao + num

                        scheduled_dt, estimated_dt = parse_time(time_text, current_time)
                        if not scheduled_dt:
                            continue

                        actual_dt = parse_status(status_text)

                        use_time = estimated_dt if estimated_dt else scheduled_dt

                        if current_time <= use_time <= current_time + timedelta(minutes=60):
                            new_routes[callsign] = {
                                "origin": iata if scope == 'arrival' else 'LAS',
                                "dest": 'LAS' if scope == 'arrival' else iata,
                                "airline_iata": airline_iata,
                                "airline_icao": airline_icao,
                                "scheduled_time": scheduled_dt.isoformat(),
                                "actual_time": estimated_dt.isoformat() if estimated_dt else actual_dt.isoformat() if actual_dt else None
                            }
                        else:
                            print(f"Flight {callsign} time not within range for {scope}")
            except Exception as e:
                print(f"No 'Show Later Flights' button or failed for {scope}: {e}")

            # Try to load and scrape "Earlier Flights"
            try:
                earlier_button = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, '//button[div[contains(text(), "Show Earlier Flights")]]'))
                )
                earlier_button.location_once_scrolled_into_view
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", earlier_button)
                time.sleep(3)  # Stabilize after scroll
                earlier_button.click()
                time.sleep(3)

                html = driver.page_source
                with open(f'{scope}_earlier.html', 'w', encoding='utf-8') as f:
                    f.write(html)
                print(f"Saved full HTML after earlier flights to {scope}_earlier.html")
                soup = BeautifulSoup(html, 'html.parser')

                table = soup.find('div', class_='FlightsListTable_table__OXs2M')
                if table:
                    rows = table.find_all('a', class_=lambda c: c and 'FlightsListTable_row__' in c)
                    print(f"Scraped additional {len(rows)} {scope}s after Earlier Flights")
                    for row in rows:
                        cells = row.find_all('div', class_=lambda c: c and 'FlightsListTable_' in c and '-cell__' in c)
                        data = []
                        for cell in cells:
                            img = cell.find('img')
                            if img and 'alt' in img.attrs and img['alt']:
                                data.append(img['alt'])
                            else:
                                text = cell.text.strip().replace('\n', ' / ')
                                data.append(text)

                        if len(data) < 7:
                            continue

                        time_text = data[0]
                        city_text = data[1] if scope == 'arrival' else data[1]  # For both, first is origin/dest string
                        airline_name = data[2]
                        status_text = data[6]

                        match = re.match(r'(.*)\ \((.*)\)(.*)', city_text)
                        if not match:
                            continue

                        city = match.group(1).strip()
                        iata = match.group(2).strip()
                        flight_num = match.group(3).strip()

                        airline_iata = FULL_NAME_TO_IATA.get(airline_name.upper(), flight_num[:2].upper())
                        airline_icao = IATA_TO_ICAO.get(airline_iata, airline_iata[:3])

                        num = flight_num[len(airline_iata):]
                        callsign = airline_icao + num

                        scheduled_dt, estimated_dt = parse_time(time_text, current_time)
                        if not scheduled_dt:
                            continue

                        actual_dt = parse_status(status_text)

                        use_time = estimated_dt if estimated_dt else scheduled_dt

                        if current_time <= use_time <= current_time + timedelta(minutes=60):
                            new_routes[callsign] = {
                                "origin": iata if scope == 'arrival' else 'LAS',
                                "dest": 'LAS' if scope == 'arrival' else iata,
                                "airline_iata": airline_iata,
                                "airline_icao": airline_icao,
                                "scheduled_time": scheduled_dt.isoformat(),
                                "actual_time": estimated_dt.isoformat() if estimated_dt else actual_dt.isoformat() if actual_dt else None
                            }
                        else:
                            print(f"Flight {callsign} time not within range for {scope}")
            except Exception as e:
                print(f"No 'Show Earlier Flights' button or failed for {scope}: {e}")

        except Exception as e:
            print(f"Scrape error for {scope}: {e}")

    driver.quit()

    route_cache.update(new_routes)

    # Cleanup old flights
    to_remove = []
    for callsign, info in route_cache.items():
        scheduled_time = datetime.fromisoformat(info['scheduled_time'])
        actual_time = datetime.fromisoformat(info['actual_time']) if info['actual_time'] else None
        use_time = actual_time or scheduled_time
        if use_time < current_time:
            to_remove.append(callsign)
        elif actual_time and actual_time + timedelta(minutes=30) < current_time:
            to_remove.append(callsign)
    for cs in to_remove:
        route_cache.pop(cs, None)

def background_scraper():
    while True:
        scrape_airport_routes()
        time.sleep(ROUTE_REFRESH_SECONDS)

threading.Thread(target=background_scraper, daemon=True).start()

@app.route('/routes', methods=['GET'])
def get_routes():
    return jsonify(route_cache)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))