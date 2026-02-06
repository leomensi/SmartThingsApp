import psycopg2
import time
import os
import requests
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app)

# Config
SMARTTHINGS_TOKEN = os.getenv("SMARTTHINGS_TOKEN", "54ccf244-948b-4fef-9401-870230445961")
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
    conn.commit()
    cur.close()
    conn.close()

def fetch_and_store():
    """Talks to SmartThings and stores both Delta and Lifetime Energy"""
    headers = {"Authorization": f"Bearer {SMARTTHINGS_TOKEN}"}
    try:
        r = requests.get("https://api.smartthings.com/v1/devices", headers=headers)
        devices = r.json().get('items', [])
        
        conn = get_db_connection()
        cur = conn.cursor()

        for d in devices:
            d_id = d['deviceId']
            d_name = d.get('label') or d.get('name') or "Unknown AC"
            loc = d.get('locationId', 'Main Home')

            s_res = requests.get(f"https://api.smartthings.com/v1/devices/{d_id}/status", headers=headers)
            if s_res.status_code == 200:
                data = s_res.json()
                main = data.get('components', {}).get('main', {})
                report = main.get('powerConsumptionReport', {}).get('powerConsumption', {}).get('value', {})

                delta_wh = report.get('deltaEnergy')
                cum_wh = report.get('energy') # The large lifetime value

                # New telemetry fields (best-effort, depends on device capabilities)
                humidity = None
                temperature_c = None
                ac_mode = None
                fan_mode = None

                try:
                    humidity_comp = main.get('relativeHumidityMeasurement', {}).get('humidity', {})
                    humidity = float(humidity_comp.get('value')) if humidity_comp.get('value') is not None else None
                except Exception:
                    pass

                try:
                    temp_comp = main.get('temperatureMeasurement', {}).get('temperature', {})
                    temperature_c = float(temp_comp.get('value')) if temp_comp.get('value') is not None else None
                except Exception:
                    pass

                try:
                    ac_mode_comp = main.get('airConditionerMode', {}).get('airConditionerMode', {})
                    ac_mode = ac_mode_comp.get('value')
                except Exception:
                    pass

                try:
                    fan_mode_comp = main.get('fanMode', {}).get('fanMode', {})
                    fan_mode = fan_mode_comp.get('value')
                except Exception:
                    pass

                if delta_wh is not None:
                    kwh_delta = float(delta_wh) / 1000.0
                    cur.execute('''
                        INSERT INTO ac_monitoring (
                            device_name,
                            location,
                            delta_kwh,
                            cumulative_wh,
                            humidity,
                            temperature_c,
                            air_conditioner_mode,
                            fan_mode
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (d_name, loc, kwh_delta, cum_wh, humidity, temperature_c, ac_mode, fan_mode))
        
        conn.commit()
        cur.close()
        conn.close()
        print(f"[{datetime.now()}] Data synchronized. Saved energy totals.")
    except Exception as e:
        print(f"Error during fetch: {e}")

def background_loop():
    """Runs every 15 minutes"""
    while True:
        fetch_and_store()
        time.sleep(900) 

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


if __name__ == '__main__':
    init_db()
    # Start background sync
    threading.Thread(target=background_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=8000)