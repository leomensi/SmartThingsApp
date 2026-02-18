import { Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule, DecimalPipe, DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
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
  imports: [CommonModule, DecimalPipe, DatePipe, FormsModule], // Explicitly import Pipes and Forms
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

  // Token UI
  protected readonly tokenValue = signal('');
  protected readonly tokenError = signal<string | null>(null);
  protected readonly tokenUpdatedAt = signal<string | null>(null);
  protected readonly tokenSecondsLeft = signal<number | null>(null);

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
  private tokenTimer: any;
  private tokenRefetchTimer: any;

  constructor(private http: HttpClient) { }

  ngOnInit() {
    this.loadData();
    this.loadTokenInfo();
    this.startTokenTimers();
    // Refresh every minute
    this.refreshTimer = setInterval(() => this.loadData(), 60000);
  }

  ngOnDestroy() {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    this.stopTokenTimers();
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

  // Token API
  loadTokenInfo() {
    this.http.get<any>(`${this.apiBase}/token`).subscribe({
      next: (res) => {
        if (res && res.updatedAt) {
          this.tokenUpdatedAt.set(res.updatedAt);
          this.tokenSecondsLeft.set(res.secondsLeft || null);
        }
      },
      error: () => { /* ignore */ }
    });
  }

  saveToken() {
    const val = this.tokenValue();
    // client-side validation to match backend rules
    if (!val || val.trim() === '') {
      this.tokenError.set('Token must not be empty');
      return;
    }
    if (/^[A-Za-z]+$/.test(val)) {
      this.tokenError.set('Token must not be only letters');
      return;
    }
    if (/^[0-9]+$/.test(val)) {
      this.tokenError.set('Token must not be only numbers');
      return;
    }
    if (!val.includes('-')) {
      this.tokenError.set("Token must contain a dash (-)");
      return;
    }

    this.tokenError.set(null);
    this.http.post<any>(`${this.apiBase}/token`, { token: val }).subscribe({
      next: (res) => {
        // If server reset the timer (new token), show 24h; if same token, keep remaining time.
        if (res && res.reset) {
          this.tokenUpdatedAt.set(res.updatedAt || new Date().toISOString());
          this.tokenSecondsLeft.set(res.secondsLeft || 24 * 3600);
        } else {
          if (res && res.secondsLeft != null) this.tokenSecondsLeft.set(res.secondsLeft);
          if (res && res.updatedAt) this.tokenUpdatedAt.set(res.updatedAt);
        }

        this.tokenError.set(null);
        // Re-sync authoritative state from server
        this.loadTokenInfo();
      },
      error: (err) => {
        this.tokenError.set(err?.error?.error || 'Failed to save token');
      }
    });
  }

  tokenCountdownText(): string {
    const s = this.tokenSecondsLeft();
    if (s == null) return 'No token set';
    if (s <= 0) return 'Expired';
    const hrs = Math.floor(s / 3600);
    const mins = Math.floor((s % 3600) / 60);
    return `${hrs}h ${mins}m left`;
  }

  private startTokenTimers() {
    if (this.tokenTimer) return;
    // Decrement local counter every second for instant feedback
    this.tokenTimer = setInterval(() => {
      const s = this.tokenSecondsLeft();
      if (s == null) return;
      if (s > 0) this.tokenSecondsLeft.set(s - 1);
    }, 1000);

    // Periodically re-sync with server to avoid drift or missed resets
    this.tokenRefetchTimer = setInterval(() => this.loadTokenInfo(), 60000);
  }

  private stopTokenTimers() {
    if (this.tokenTimer) { clearInterval(this.tokenTimer); this.tokenTimer = null; }
    if (this.tokenRefetchTimer) { clearInterval(this.tokenRefetchTimer); this.tokenRefetchTimer = null; }
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
