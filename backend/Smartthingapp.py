import psycopg2
import time
import os
import requests
import threading
import csv
import io
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

# Config
SMARTTHINGS_CLIENT_ID = os.getenv("SMARTTHINGS_CLIENT_ID")
SMARTTHINGS_CLIENT_SECRET = os.getenv("SMARTTHINGS_CLIENT_SECRET")
# We expect REFRESH_TOKEN_1 ... REFRESH_TOKEN_5

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "user": os.getenv("DB_USER", "myuser"),
    "password": os.getenv("DB_PASSWORD", "mypassword"),
    "database": os.getenv("DB_NAME", "mydatabase")
}

def get_db_connection():
    while True:
        try:
            return psycopg2.connect(**DB_CONFIG)
        except psycopg2.OperationalError:
            print("Database not ready, retrying in 2s...")
            time.sleep(2)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Base table to store energy and device telemetry
    cur.execute('''
        CREATE TABLE IF NOT EXISTS ac_monitoring (
            id SERIAL PRIMARY KEY,
            device_name TEXT,
            location TEXT,
            delta_kwh FLOAT,
            cumulative_wh FLOAT,
            reading_day DATE DEFAULT CURRENT_DATE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    # Ensure new telemetry columns exist (safe to run multiple times)
    cur.execute("ALTER TABLE ac_monitoring ADD COLUMN IF NOT EXISTS humidity FLOAT;")
    cur.execute("ALTER TABLE ac_monitoring ADD COLUMN IF NOT EXISTS temperature_c FLOAT;")
    cur.execute("ALTER TABLE ac_monitoring ADD COLUMN IF NOT EXISTS air_conditioner_mode TEXT;")
    cur.execute("ALTER TABLE ac_monitoring ADD COLUMN IF NOT EXISTS fan_mode TEXT;")

    # Table for storing rotating OAuth tokens
    cur.execute('''
        CREATE TABLE IF NOT EXISTS auth_tokens (
            location_index INTEGER PRIMARY KEY,
            refresh_token TEXT,
            access_token TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    
    # Initialize tokens from env if table is empty
    for i in range(1, 6):
        cur.execute("SELECT 1 FROM auth_tokens WHERE location_index = %s", (i,))
        if cur.fetchone() is None:
            initial_refresh = os.getenv(f"REFRESH_TOKEN_{i}")
            if initial_refresh:
                print(f"Seeding initial token for Location {i}")
                cur.execute('''
                    INSERT INTO auth_tokens (location_index, refresh_token)
                    VALUES (%s, %s)
                ''', (i, initial_refresh))
    
    conn.commit()
    cur.close()
    conn.close()

def get_valid_access_token(location_index):
    """
    Retrieves a valid access token for the given location index.
    If the current one is missing or we force a refresh, it calls the SmartThings API.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT refresh_token FROM auth_tokens WHERE location_index = %s", (location_index,))
    row = cur.fetchone()
    
    if not row or not row[0]:
        print(f"No refresh token found for Location {location_index}")
        cur.close()
        conn.close()
        return None
        
    current_refresh_token = row[0]
    
    # Exchange refresh token for a new access token
    # https://auth-global.api.smartthings.com/oauth/token
    try:
        data = {
            "grant_type": "refresh_token",
            "client_id": SMARTTHINGS_CLIENT_ID,
            "client_secret": SMARTTHINGS_CLIENT_SECRET,
            "refresh_token": current_refresh_token
        }
        r = requests.post("https://auth-global.api.smartthings.com/oauth/token", data=data, auth=(SMARTTHINGS_CLIENT_ID, SMARTTHINGS_CLIENT_SECRET))
        
        if r.status_code != 200:
            print(f"Error refreshing token for Loc {location_index}: {r.text}")
            cur.close()
            conn.close()
            return None
            
        resp_json = r.json()
        new_access_token = resp_json.get("access_token")
        new_refresh_token = resp_json.get("refresh_token")
        
        if new_access_token and new_refresh_token:
            print(f"Successfully refreshed token for Loc {location_index}")
            cur.execute("""
                UPDATE auth_tokens 
                SET access_token = %s, refresh_token = %s, updated_at = NOW() 
                WHERE location_index = %s
            """, (new_access_token, new_refresh_token, location_index))
            conn.commit()
            cur.close()
            conn.close()
            return new_access_token
            
    except Exception as e:
        print(f"Exception refreshing token for Loc {location_index}: {e}")
        
    cur.close()
    conn.close()
    return None

def fetch_and_store():
    """Talks to SmartThings and stores data for all 5 locations"""
    
    for loc_idx in range(1, 6):
        access_token = get_valid_access_token(loc_idx)
        if not access_token:
            continue
            
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            r = requests.get("https://api.smartthings.com/v1/devices", headers=headers)
            if r.status_code == 401:
                print(f"Loc {loc_idx}: 401 Unauthorized. Access token might have expired despite refresh attempt.")
                continue
                
            devices = r.json().get('items', [])
            print(f"Loc {loc_idx}: Found {len(devices)} raw devices via API.")
            
            conn = get_db_connection()
            cur = conn.cursor()

            for d in devices:
                d_id = d['deviceId']
                d_name = d.get('label') or d.get('name') or "Unknown AC"
                
                # Fetch detailed status
                s_res = requests.get(f"https://api.smartthings.com/v1/devices/{d_id}/status", headers=headers)
                if s_res.status_code == 200:
                    data = s_res.json()
                    main = data.get('components', {}).get('main', {})
                    
                    # We need location info - typically stored on the device object but often we just use 'Main Home'
                    # With OAuth per location, we can assume all devices here belong to this "Location X" context
                    # But the API returns locationId.
                    real_loc_id = d.get('locationId', f'Location-{loc_idx}')
                    
                    report = main.get('powerConsumptionReport', {}).get('powerConsumption', {}).get('value', {})
                    delta_wh = report.get('deltaEnergy')
                    cum_wh = report.get('energy') 
                    if delta_wh is None:
                        print(f"NOTICE: Device '{d_name}' (ID: {d_id}) has no 'deltaEnergy'. Syncing anyway with 0.")
                        delta_wh = 0
                        cum_wh = cum_wh or 0

                    # New telemetry fields
                    humidity = None
                    temperature_c = None
                    ac_mode = None
                    fan_mode = None

                    try:
                        humidity_comp = main.get('relativeHumidityMeasurement', {}).get('humidity', {})
                        humidity = float(humidity_comp.get('value')) if humidity_comp.get('value') is not None else None
                    except Exception: pass

                    try:
                        temp_comp = main.get('temperatureMeasurement', {}).get('temperature', {})
                        temperature_c = float(temp_comp.get('value')) if temp_comp.get('value') is not None else None
                    except Exception: pass

                    try:
                        ac_mode_comp = main.get('airConditionerMode', {}).get('airConditionerMode', {})
                        ac_mode = ac_mode_comp.get('value')
                    except Exception: pass

                    try:
                        fan_mode_comp = main.get('fanMode', {}).get('fanMode', {})
                        fan_mode = fan_mode_comp.get('value')
                    except Exception: pass

                    if delta_wh is not None:
                        kwh_delta = float(delta_wh) / 1000.0
                        cur.execute('''
                            INSERT INTO ac_monitoring (
                                device_name, location, delta_kwh, cumulative_wh, 
                                humidity, temperature_c, air_conditioner_mode, fan_mode
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (d_name, real_loc_id, kwh_delta, cum_wh, humidity, temperature_c, ac_mode, fan_mode))
                else:
                    print(f"WARNING: Loc {loc_idx} Device {d_name} - status call failed: {s_res.status_code}")
            
            conn.commit()
            cur.close()
            conn.close()
            print(f"[{datetime.now()}] Data synchronized for Location {loc_idx}.")
            
        except Exception as e:
            print(f"Error during fetch for Loc {loc_idx}: {e}")

def background_loop():
    """Runs every 5 minutes, aligned to the clock (00, 05, 10...)"""
    print("Background sync loop started.")
    
    # First run immediately or wait? User asked for 55, 60, 05. 
    # Let's run once immediately to populate data, then wait for next mark.
    fetch_and_store()

    while True:
        now = time.time()
        # Calculate time to next 5-minute mark
        # 300 seconds = 5 mins
        # next_ts = current_ts + (300 - (current_ts % 300))
        sleep_seconds = 300 - (now % 300)
        
        print(f"Sleeping for {sleep_seconds:.1f}s until next 5-minute mark...")
        time.sleep(sleep_seconds)
        
        # Add a small buffer to ensure we are seemingly 'past' the mark
        time.sleep(1) 
        
        fetch_and_store() 

@app.route('/dashboard')
def get_stats():
    """
    Returns a list of devices with:
    1. totalKWh: Sum of deltas for the current calendar day.
    2. rolling24hKWh: Difference between current energy and energy 24h ago.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    # This query calculates:
    # - Daily Sum: All deltas for today
    # - Rolling 24h: Latest cumulative_wh minus the one from 24h ago
    # - Latest telemetry: humidity, temperature, AC mode, fan mode
    query = '''
    WITH latest_data AS (
        SELECT DISTINCT ON (device_name) 
            device_name,
            cumulative_wh,
            location,
            timestamp,
            humidity,
            temperature_c,
            air_conditioner_mode,
            fan_mode
        FROM ac_monitoring
        ORDER BY device_name, timestamp DESC
    ),
    past_data AS (
        SELECT DISTINCT ON (device_name) 
            device_name, cumulative_wh
        FROM ac_monitoring
        WHERE timestamp <= NOW() - INTERVAL '24 hours'
        ORDER BY device_name, timestamp DESC
    ),
    today_sum AS (
        SELECT device_name, SUM(delta_kwh) as daily_total
        FROM ac_monitoring
        WHERE reading_day = CURRENT_DATE
        GROUP BY device_name
    )
    SELECT 
        l.device_name,
        l.location,
        COALESCE(t.daily_total, 0) as today_kwh,
        (l.cumulative_wh - COALESCE(p.cumulative_wh, l.cumulative_wh)) / 1000.0 as rolling_24h_kwh,
        l.timestamp,
        l.humidity,
        l.temperature_c,
        l.air_conditioner_mode,
        l.fan_mode
    FROM latest_data l
    LEFT JOIN past_data p ON l.device_name = p.device_name
    LEFT JOIN today_sum t ON l.device_name = t.device_name;
    '''
    
    cur.execute(query)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    return jsonify([{
        "deviceName": r[0],
        "location": r[1],
        "todayKWh": round(r[2], 3),
        "rolling24hKWh": round(r[3], 3),
        "lastUpdate": r[4].isoformat(),
        "humidity": round(r[5], 1) if r[5] is not None else None,
        "temperatureC": round(r[6], 1) if r[6] is not None else None,
        "airConditionerMode": r[7],
        "fanMode": r[8]
    } for r in rows])


@app.route('/dashboard/hourly')
def get_hourly_stats():
    """
    Returns per-device energy usage aggregated by hour for the last 24 hours.
    Response shape:
    [
        {
            "deviceName": "...",
            "location": "...",
            "hourly": [
                { "hour": "2025-02-04T10:00:00", "kWh": 0.25 },
                ...
            ]
        },
        ...
    ]
    """
    conn = get_db_connection()
    cur = conn.cursor()

    query = '''
    SELECT 
        device_name,
        location,
        date_trunc('hour', timestamp) AS hour_bucket,
        SUM(delta_kwh) AS kwh
    FROM ac_monitoring
    WHERE timestamp >= NOW() - INTERVAL '24 hours'
    GROUP BY device_name, location, hour_bucket
    ORDER BY device_name, hour_bucket;
    '''

    cur.execute(query)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    devices = {}
    for device_name, location, hour_bucket, kwh in rows:
        key = (device_name, location)
        if key not in devices:
            devices[key] = {
                "deviceName": device_name,
                "location": location,
                "hourly": []
            }
        devices[key]["hourly"].append({
            "hour": hour_bucket.isoformat(),
            "kWh": round(kwh or 0, 3)
        })

    return jsonify(list(devices.values()))


@app.route('/history')
def get_history():
    """
    Per-device history endpoint.
    Query params:
      - deviceName (required)
      - location (required, can be empty string for default)
      - hours (optional, default 24, max 168)
    Returns latest rows (up to 500) ordered by timestamp DESC.
    """
    device_name = request.args.get('deviceName')
    location = request.args.get('location', '')
    hours_str = request.args.get('hours', '24')

    if not device_name:
        return jsonify({"error": "deviceName is required"}), 400

    try:
        hours = int(hours_str)
    except ValueError:
        hours = 24

    if hours <= 0:
        hours = 24
    if hours > 168:
        hours = 168  # clamp to 7 days

    conn = get_db_connection()
    cur = conn.cursor()

    query = f"""
        SELECT
            timestamp,
            delta_kwh,
            cumulative_wh,
            humidity,
            temperature_c,
            air_conditioner_mode,
            fan_mode
        FROM ac_monitoring
        WHERE device_name = %s
          AND (location = %s OR %s = '')
          AND timestamp >= NOW() - INTERVAL '{hours} hours'
        ORDER BY timestamp DESC
        LIMIT 500;
    """

    cur.execute(query, (device_name, location, location))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    result = []
    for ts, delta_kwh, cumulative_wh, hum, temp_c, ac_mode, fan_mode in rows:
        result.append({
            "timestamp": ts.isoformat(),
            "deltaKWh": float(delta_kwh) if delta_kwh is not None else None,
            "cumulativeWh": float(cumulative_wh) if cumulative_wh is not None else None,
            "humidity": float(hum) if hum is not None else None,
            "temperatureC": float(temp_c) if temp_c is not None else None,
            "airConditionerMode": ac_mode,
            "fanMode": fan_mode,
        })

    return jsonify(result)


@app.route('/export')
def export_csv():
    """
    Exports all AC data to CSV.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Export last 30 days by default to keep it manageable, or all if needed.
    # User asked for "medianet ac data", implying a dump.
    query = '''
        SELECT 
            t1.timestamp,
            t1.device_name,
            t1.location,
            t1.delta_kwh,
            t1.cumulative_wh,
            t1.humidity,
            t1.temperature_c,
            t1.air_conditioner_mode,
            t1.fan_mode,
            (
                t1.cumulative_wh - COALESCE(
                    (
                        SELECT t2.cumulative_wh 
                        FROM ac_monitoring t2 
                        WHERE t2.device_name = t1.device_name 
                          AND t2.timestamp <= t1.timestamp - INTERVAL '24 hours' 
                        ORDER BY t2.timestamp DESC 
                        LIMIT 1
                    ), 
                    t1.cumulative_wh
                )
            ) / 1000.0 as rolling_24h_kwh
        FROM ac_monitoring t1
        ORDER BY t1.device_name ASC, t1.timestamp DESC
        LIMIT 10000; -- Safety limit for now
    '''
    
    cur.execute(query)
    rows = cur.fetchall()
    
    # Create CSV in memory
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['Timestamp', 'Device Name', 'Location', 'Delta kWh', 'Cumulative Wh', 'Humidity (%)', 'Temperature (C)', 'AC Mode', 'Fan Mode', '24h Usage (kWh)'])
    
    for row in rows:
        cw.writerow(row)
        
    cur.close()
    conn.close()
    
    output = io.BytesIO()
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)
    
    return send_file(
        output,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'ac_data_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    )


if __name__ == '__main__':
    init_db()
    # Start background sync
    threading.Thread(target=background_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=8000)