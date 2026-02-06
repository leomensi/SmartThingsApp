import { Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule, DecimalPipe, DatePipe } from '@angular/common';
import { HttpClient } from '@angular/common/http';

/* Interfaces */
export interface DeviceSummary {
  deviceName: string;
  location: string;
  todayKWh: number;
  rolling24hKWh: number;
  lastUpdate: string;
  humidity?: number | null;
  temperatureC?: number | null;
  airConditionerMode?: string | null;
  fanMode?: string | null;
}

export interface DeviceHourlyPoint {
  hour: string;
  kWh: number;
}

export interface DeviceHistoryPoint {
  timestamp: string;
  deltaKWh: number | null;
  cumulativeWh: number | null;
  humidity?: number | null;
  temperatureC?: number | null;
  airConditionerMode?: string | null;
  fanMode?: string | null;
}

export interface DeviceHourlyResponse {
  deviceName: string;
  location: string;
  hourly: DeviceHourlyPoint[];
}

export interface HourlyLookup {
  [key: string]: DeviceHourlyPoint[];
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, DecimalPipe, DatePipe], // Explicitly import Pipes
  styleUrl: './app.css',
  templateUrl: './app.html' // Use external template for cleanliness
})
export class App implements OnInit, OnDestroy {
  private readonly apiBase = 'http://localhost:8000';

  // Signals
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);

  protected readonly devices = signal<DeviceSummary[]>([]);
  protected readonly hourlyByDevice = signal<HourlyLookup>({});
  protected readonly selectedDeviceKey = signal<string | null>(null); // Key: "Name|Location"

  protected readonly history = signal<DeviceHistoryPoint[]>([]);
  protected readonly historyLoading = signal(false);

  // Computed
  protected readonly selectedDevice = computed(() => {
    const key = this.selectedDeviceKey();
    if (!key) return null;

    // Key format: "Name|Location"
    const [name, loc] = key.split('|');
    return this.devices().find(d => d.deviceName === name && d.location === loc) || null;
  });

  protected readonly selectedHourly = computed(() => {
    const key = this.selectedDeviceKey();
    if (!key) return [];
    return this.hourlyByDevice()[key] || [];
  });

  protected readonly maxHourlyKWh = computed(() => {
    return this.selectedHourly().reduce((max, p) => Math.max(max, p.kWh), 0);
  });

  private refreshTimer: any;

  constructor(private http: HttpClient) { }

  ngOnInit() {
    this.loadData();
    // Refresh every minute
    this.refreshTimer = setInterval(() => this.loadData(), 60000);
  }

  ngOnDestroy() {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
  }

  // Actions
  selectDevice(device: DeviceSummary) {
    this.selectedDeviceKey.set(this.getDeviceKey(device));
  }

  isSelected(device: DeviceSummary): boolean {
    return this.selectedDeviceKey() === this.getDeviceKey(device);
  }

  getDeviceKey(device: DeviceSummary): string {
    return `${device.deviceName}|${device.location}`;
  }

  loadData() {
    // Only set loading true on first load to avoid flickering
    if (this.devices().length === 0) {
      this.loading.set(true);
    }
    this.error.set(null);

    this.http.get<DeviceSummary[]>(`${this.apiBase}/dashboard`).subscribe({
      next: (data) => {
        this.devices.set(data);

        // Auto-select first if none selected
        if (!this.selectedDeviceKey() && data.length > 0) {
          this.selectedDeviceKey.set(this.getDeviceKey(data[0]));
        }

        // Now load hourly data
        this.loadHourly();
      },
      error: (err) => {
        console.error('API Error:', err);
        this.error.set('Could not connect to backend.');
        this.loading.set(false);
      }
    });
  }

  loadHourly() {
    this.http.get<DeviceHourlyResponse[]>(`${this.apiBase}/dashboard/hourly`).subscribe({
      next: (data) => {
        const lookup: HourlyLookup = {};
        data.forEach(item => {
          const key = `${item.deviceName}|${item.location}`;
          // Sort by hour
          lookup[key] = item.hourly.sort((a, b) => a.hour.localeCompare(b.hour));
        });
        this.hourlyByDevice.set(lookup);
        this.loading.set(false);
      },
      error: (err) => {
        console.error('Hourly API Error:', err);
        // Don't block UI, just stop loading
        this.loading.set(false);
      }
    });
  }

  loadHistoryForCurrent() {
    const current = this.selectedDevice();
    if (!current) return;

    this.historyLoading.set(true);
    this.http.get<DeviceHistoryPoint[]>(`${this.apiBase}/history`, {
      params: {
        deviceName: current.deviceName,
        location: current.location,
        hours: 24
      }
    }).subscribe({
      next: (data) => {
        this.history.set(data);
        this.historyLoading.set(false);
      },
      error: (err) => {
        console.error('History API Error:', err);
        this.historyLoading.set(false);
      }
    });
  }

  exportCsv() {
    window.open(`${this.apiBase}/export`, '_blank');
  }

  // Helpers
  formatLastUpdate(isoDate: string): string {
    if (!isoDate) return 'Never';
    return new Date(isoDate).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  hourlyBarHeight(kWh: number): string {
    const max = this.maxHourlyKWh();
    return max > 0 ? `${(kWh / max) * 100}%` : '0%';
  }
}
