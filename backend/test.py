import requests
import csv
from datetime import datetime

# --- CONFIGURATION ---
TOKEN = "YOUR_TOKEN_HERE"
BASE_URL = "https://api.smartthings.com/v1"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def get_all_energy_devices():
    """Finds all devices that support energy reporting."""
    response = requests.get(f"{BASE_URL}/devices", headers=HEADERS)
    devices = response.json().get('items', [])
    # Filter for devices that have 'powerConsumptionReport'
    energy_devices = [
        d for d in devices 
        if any(c['id'] == 'powerConsumptionReport' for c in d.get('components', [{}])[0].get('capabilities', []))
    ]
    return energy_devices

def get_device_energy(device_id):
    """Fetches energy data for a specific device."""
    url = f"{BASE_URL}/devices/{device_id}/components/main/capabilities/powerConsumptionReport/status"
    res = requests.get(url, headers=HEADERS)
    if res.status_code == 200:
        data = res.json()
        # Path: data['powerConsumption']['value']
        energy = data.get('powerConsumption', {}).get('value', {}).get('energy', 0)
        power = data.get('powerConsumption', {}).get('value', {}).get('power', 0)
        return energy, power
    return 0, 0

def main():
    print("Scanning for AC units...")
    devices = get_all_energy_devices()
    print(f"Found {len(devices)} energy-capable devices.")
    
    filename = f"energy_log_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    
    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Timestamp', 'Label', 'DeviceID', 'Energy_Wh', 'Power_W'])
        
        for dev in devices:
            label = dev['label']
            d_id = dev['deviceId']
            energy, power = get_device_energy(d_id)
            print(f"Feeding data: {label} -> {energy} Wh")
            writer.writerow([datetime.now(), label, d_id, energy, power])

    print(f"Done! Data saved to {filename}")

if __name__ == "__main__":
    main()