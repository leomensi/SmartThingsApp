import psycopg2
import time
import os
import requests
import threading
import csv
import io
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from datetime import datetime, timedelta
import json
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

# ========== 24-HOUR TOKEN - REPLACE THIS ===========
# Token is persisted to ../token.py by the update endpoint.
TOKEN_24H = "b50de4ac-6120-4cc5-a3f5-118cd40ffa16"
# ISO timestamp when token was last set (UTC)
TOKEN_24H_UPDATED_AT = None
TOKEN_FILE_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'token.py'))

def load_token_from_file():
    global TOKEN_24H, TOKEN_24H_UPDATED_AT
    try:
        if os.path.exists(TOKEN_FILE_PATH):
            with open(TOKEN_FILE_PATH, 'r', encoding='utf-8') as f:
                data = f.read()
            # Very small parser: look for TOKEN_24H and TOKEN_24H_UPDATED_AT assignments
            import re
            m = re.search(r"TOKEN_24H\s*=\s*['\"](.+?)['\"]", data)
            if m:
                TOKEN_24H = m.group(1)
            m2 = re.search(r"TOKEN_24H_UPDATED_AT\s*=\s*['\"](.+?)['\"]", data)
            if m2:
                TOKEN_24H_UPDATED_AT = m2.group(1)
    except Exception as e:
        print(f"Could not load token file: {e}")

def write_token_to_file(token_value, updated_at_iso):
    # Writes a simple token.py that other tools may read.
    content = f"TOKEN_24H = \"{token_value}\"\nTOKEN_24H_UPDATED_AT = \"{updated_at_iso}\"\n"
    with open(TOKEN_FILE_PATH, 'w', encoding='utf-8') as f:
        f.write(content)

# Try load persisted token on startup
load_token_from_file()

# Track last background fetch status for UI consumption
LAST_FETCH_STATUS = {
    'ok': True,
    'code': None,
    'message': 'Not yet fetched',
    'timestamp': None
}

# Config

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
    # Store the original API timestamp when available to detect repeated API samples
    cur.execute("ALTER TABLE ac_monitoring ADD COLUMN IF NOT EXISTS api_timestamp TIMESTAMP;")
    
    conn.commit()
    cur.close()
    conn.close()


def _parse_report_timestamp(val):
    if not val:
        return None
    try:
        if isinstance(val, (int, float)):
            # Handle epoch timestamps
            if val > 1e12:
                dt = datetime.fromtimestamp(val / 1000.0)
            else:
                dt = datetime.fromtimestamp(val)
        else:
            # Handle ISO strings (e.g., 2026-02-20T10:16:14Z)
            s = str(val).replace('Z', '+00:00')
            dt = datetime.fromisoformat(s)
        
        # CRITICAL FIX: Strip timezone info to match Postgres naive timestamps
        return dt.replace(tzinfo=None)
    except Exception as e:
        print(f"Timestamp parse error: {e}")
        return None

def compute_adjusted_delta(cur, device_name: str, api_ts, new_delta_wh: float):
    if api_ts is None:
        return new_delta_wh

    try:
        cur.execute(
            "SELECT api_timestamp FROM ac_monitoring WHERE device_name = %s ORDER BY id DESC LIMIT 1;",
            (device_name,)
        )
        row = cur.fetchone()
        
        if row and row[0] is not None:
            prev_api_ts = row[0]
            
            # If DB timestamp is aware, make it naive
            if hasattr(prev_api_ts, 'tzinfo') and prev_api_ts.tzinfo is not None:
                prev_api_ts = prev_api_ts.replace(tzinfo=None)
            
            # Now both are naive, subtraction will work
            if abs((prev_api_ts - api_ts).total_seconds()) < 1:
                return 0.0

    except Exception as e:
        # This will catch the error you saw and print details if it fails again
        print(f"Error comparing timestamps: {e}")
        
    return new_delta_wh
def fetch_and_store():
    """Talks to SmartThings and stores data - single API call for all devices"""
    
    # Use the 24-hour token
    headers = {"Authorization": f"Bearer {TOKEN_24H}"}
    try:
        r = requests.get("https://api.smartthings.com/v1/devices", headers=headers)
        if r.status_code == 401:
            print(f"Loc(X): 401 Unauthorized. Access token might have expired.")
            LAST_FETCH_STATUS.update({
                'ok': False,
                'code': 401,
                'message': 'Unauthorized - token may be expired',
                'timestamp': datetime.utcnow().isoformat()
            })
            return
        elif r.status_code != 200:
            LAST_FETCH_STATUS.update({
                'ok': False,
                'code': r.status_code,
                'message': f'HTTP {r.status_code}',
                'timestamp': datetime.utcnow().isoformat()
            })
            print(f"Loc(X): Devices API returned {r.status_code}")
            return
            
        devices = r.json().get('items', [])
        print(f"Loc(X): Found {len(devices)} raw devices API")
        
        conn = get_db_connection()
        cur = conn.cursor()

        for d in devices:
            d_id = d['deviceId']
            d_name = d.get('label') or d.get('name') or "Unknown AC"
            real_loc_id = d.get('locationId') or "unknown_location"
            
            # Fetch detailed status
        # Inside the loop: for d in devices:
            s_res = requests.get(f"https://api.smartthings.com/v1/devices/{d_id}/status", headers=headers)
            if s_res.status_code == 200:
                data = s_res.json()
                main = data.get('components', {}).get('main', {})
                
                # Define pc_root here
                pc_root = main.get('powerConsumptionReport', {}).get('powerConsumption', {})
                
                report_values = pc_root.get('value', {})
                cum_wh = report_values.get('energy', 0)
                delta_wh = report_values.get('deltaEnergy', 0)
                
                # Use pc_root for the timestamp
                report_ts_raw = pc_root.get('timestamp')
                api_ts = _parse_report_timestamp(report_ts_raw)
                
                # Calculate
                adjusted_delta_wh = compute_adjusted_delta(cur, d_name, api_ts, float(delta_wh))
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
                    # Adjust delta if API returned the same sample timestamp as previous
                    try:
                        adjusted_delta_wh = compute_adjusted_delta(cur, d_name, api_ts, float(delta_wh))
                    except Exception:
                        adjusted_delta_wh = float(delta_wh)

                    kwh_delta = float(adjusted_delta_wh) / 1000.0
                    # Insert api_timestamp when available for future comparisons
                    if api_ts is not None:
                        cur.execute('''
                            INSERT INTO ac_monitoring (
                                device_name, location, delta_kwh, cumulative_wh, 
                                humidity, temperature_c, air_conditioner_mode, fan_mode, api_timestamp
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (d_name, real_loc_id, kwh_delta, cum_wh, humidity, temperature_c, ac_mode, fan_mode, api_ts))
                    else:
                        cur.execute('''
                            INSERT INTO ac_monitoring (
                                device_name, location, delta_kwh, cumulative_wh, 
                                humidity, temperature_c, air_conditioner_mode, fan_mode
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (d_name, real_loc_id, kwh_delta, cum_wh, humidity, temperature_c, ac_mode, fan_mode))
        
        conn.commit()
        cur.close()
        conn.close()
        LAST_FETCH_STATUS.update({
            'ok': True,
            'code': 200,
            'message': f'Last fetch OK ({len(devices)} devices)',
            'timestamp': datetime.utcnow().isoformat()
        })
    except Exception as e:
        print(f"Error during fetch: {e}")
        LAST_FETCH_STATUS.update({
            'ok': False,
            'code': None,
            'message': f'Exception: {e}',
            'timestamp': datetime.utcnow().isoformat()
        })

def background_loop():
    """Runs every 5 minutes, aligned to the clock (00, 05, 10...)"""
    print("Background sync loop started.")
    
    # First run immediately or wait? User asked for 55, 60, 05. 
    # Let's run once immediately to populate data, then wait for next mark.
    fetch_and_store()

    last_run_slot = None

    while True:
        now = time.time()
        current_slot = int(now) // 300

        if current_slot != last_run_slot:
            # Wait until just AFTER the exact 5-minute boundary
            next_mark = (current_slot + 1) * 300
            sleep_time = next_mark - now
            time.sleep(sleep_time + 1)

            fetch_and_store()
            last_run_slot = current_slot + 1
        else:
            time.sleep(30)

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
        ORDER BY t1.timestamp DESC  -- This now sorts by newest data first
        LIMIT 70000;
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


@app.route('/fetch_status')
def fetch_status():
    """Returns last background fetch status for the frontend to display errors."""
    return jsonify(LAST_FETCH_STATUS)


@app.route('/token', methods=['GET'])
def get_token_info():
    """Returns token and time-left until 24h expiry."""
    updated = TOKEN_24H_UPDATED_AT
    now = datetime.utcnow()
    if updated:
        try:
            updated_dt = datetime.fromisoformat(updated)
        except Exception:
            updated_dt = None
    else:
        updated_dt = None

    seconds_left = None
    expires_at = None
    if updated_dt:
        expires_at_dt = updated_dt + timedelta(hours=24)
        expires_at = expires_at_dt.isoformat()
        delta = (expires_at_dt - now)
        seconds_left = int(max(0, delta.total_seconds()))

    # Mask token when returning
    masked = None
    if TOKEN_24H:
        t = TOKEN_24H
        masked = t[:4] + '...' + t[-4:] if len(t) > 8 else '****'

    return jsonify({
        'tokenMasked': masked,
        'updatedAt': TOKEN_24H_UPDATED_AT,
        'expiresAt': expires_at,
        'secondsLeft': seconds_left
    })


@app.route('/token', methods=['POST'])
def update_token():
    """Update the 24-hour token with validation and persist to token.py"""
    global TOKEN_24H, TOKEN_24H_UPDATED_AT
    data = request.get_json(force=True) or {}
    new_token = (data.get('token') or '').strip()

    # Validation per requirements
    if not new_token:
        return jsonify({'error': 'Token must not be empty'}), 400
    if new_token.isalpha():
        return jsonify({'error': 'Token must not be only letters'}), 400
    if new_token.isdigit():
        return jsonify({'error': 'Token must not be only numbers'}), 400
    if '-' not in new_token:
        return jsonify({'error': 'Token must contain a dash (-)'}), 400

    now_dt = datetime.utcnow()
    now_iso = now_dt.isoformat()

    # If token changed, treat as a new token and reset timer to 24h
    if new_token != TOKEN_24H:
        TOKEN_24H = new_token
        TOKEN_24H_UPDATED_AT = now_iso
        try:
            write_token_to_file(new_token, now_iso)
        except Exception as e:
            return jsonify({'error': f'Failed to write token file: {e}'}), 500

        seconds_left = 24 * 3600
        return jsonify({'ok': True, 'updatedAt': now_iso, 'secondsLeft': seconds_left, 'reset': True})

    # If token is the same, do NOT reset the timestamp; return remaining time
    if TOKEN_24H_UPDATED_AT:
        try:
            updated_dt = datetime.fromisoformat(TOKEN_24H_UPDATED_AT)
            expires_at = updated_dt + timedelta(hours=24)
            seconds_left = int(max(0, (expires_at - datetime.utcnow()).total_seconds()))
        except Exception:
            # Fallback: if parsing fails, assume expired
            seconds_left = 0
    else:
        # No recorded timestamp yet: initialize it now (first time submission)
        TOKEN_24H_UPDATED_AT = now_iso
        try:
            write_token_to_file(TOKEN_24H, TOKEN_24H_UPDATED_AT)
        except Exception as e:
            return jsonify({'error': f'Failed to write token file: {e}'}), 500
        seconds_left = 24 * 3600

    return jsonify({'ok': True, 'updatedAt': TOKEN_24H_UPDATED_AT, 'secondsLeft': seconds_left, 'reset': False})


if __name__ == '__main__':
    init_db()
    # Start background sync
    threading.Thread(target=background_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=8000)
