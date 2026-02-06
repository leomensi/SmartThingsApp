import { Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';

interface DeviceSummary {
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

interface DeviceHourlyPoint {
  hour: string;
  kWh: number;
}

interface DeviceHistoryPoint {
  timestamp: string;
  deltaKWh: number | null;
  cumulativeWh: number | null;
  humidity?: number | null;
  temperatureC?: number | null;
  airConditionerMode?: string | null;
  fanMode?: string | null;
}

interface DeviceHourlyResponse {
  deviceName: string;
  location: string;
  hourly: DeviceHourlyPoint[];
}

interface HourlyLookup {
  [key: string]: DeviceHourlyPoint[];
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule],
  styleUrl: './app.css',
  template: `
    <main class="page">
      <header class="page-header">
        <div>
          <h1>Power Usage Dashboard</h1>
          <p>Monitor power and climate data for each AC device.</p>
        </div>
      </header>

      <section class="page-content" *ngIf="!loading(); else loadingTpl">
        <ng-container *ngIf="!error(); else errorTpl">
          <div class="layout">
            <aside class="sidebar">
              <h2 class="sidebar-title">Devices</h2>
              <div class="device-list" *ngIf="devices().length; else noDevicesTpl">
                <button
                  class="device-card"
                  *ngFor="let device of devices()"
                  [class.device-card--active]="isSelected(device)"
                  (click)="selectDevice(device)"
                >
                  <div class="device-card__header">
                    <div class="device-card__name">{{ device.deviceName }}</div>
                    <div class="device-card__location">{{ device.location }}</div>
                  </div>
                  <div class="device-card__metrics">
                    <div class="metric">
                      <span class="metric__label">Today</span>
                      <span class="metric__value">
                        {{ device.todayKWh | number : '1.2-2' }} kWh
                      </span>
                    </div>
                    <div class="metric">
                      <span class="metric__label">Last 24h</span>
                      <span class="metric__value">
                        {{ device.rolling24hKWh | number : '1.2-2' }} kWh
                      </span>
                    </div>
                    <div class="metric" *ngIf="device.temperatureC != null">
                      <span class="metric__label">Temp</span>
                      <span class="metric__value">
                        {{ device.temperatureC | number : '1.0-1' }} °C
                      </span>
                    </div>
                    <div class="metric" *ngIf="device.humidity != null">
                      <span class="metric__label">Humidity</span>
                      <span class="metric__value">
                        {{ device.humidity | number : '1.0-0' }} %
                      </span>
                    </div>
                  </div>
                  <div class="device-card__footer">
                    <span class="device-card__updated">
                      Updated {{ formatLastUpdate(device.lastUpdate) }}
                    </span>
                  </div>
                </button>
              </div>
            </aside>

            <section class="main-panel" *ngIf="selectedDevice() as current">
              <div class="summary-grid">
                <div class="summary-card">
                  <div class="summary-card__label">Device</div>
                  <div class="summary-card__value">{{ current.deviceName }}</div>
                  <div class="summary-card__subvalue">{{ current.location }}</div>
                </div>
                <div class="summary-card">
                  <div class="summary-card__label">Today</div>
                  <div class="summary-card__value">
                    {{ current.todayKWh | number : '1.2-2' }} kWh
                  </div>
                  <div class="summary-card__subvalue">Energy used since midnight</div>
                </div>
                <div class="summary-card">
                  <div class="summary-card__label">Last 24 hours</div>
                  <div class="summary-card__value">
                    {{ current.rolling24hKWh | number : '1.2-2' }} kWh
                  </div>
                  <div class="summary-card__subvalue">Rolling window</div>
                </div>
                <div class="summary-card">
                  <div class="summary-card__label">Environment</div>
                  <div class="summary-card__value">
                    <ng-container *ngIf="current.temperatureC != null; else noTemp">
                      {{ current.temperatureC | number : '1.0-1' }} °C
                    </ng-container>
                    <ng-template #noTemp>—</ng-template>
                  </div>
                  <div class="summary-card__subvalue">
                    <ng-container *ngIf="current.humidity != null">
                      Humidity: {{ current.humidity | number : '1.0-0' }} %
                    </ng-container>
                    <ng-container *ngIf="current.humidity == null">
                      No humidity data
                    </ng-container>
                  </div>
                </div>
                <div class="summary-card" *ngIf="current.airConditionerMode || current.fanMode">
                  <div class="summary-card__label">Modes</div>
                  <div class="summary-card__value">
                    <ng-container *ngIf="current.airConditionerMode; else noAcMode">
                      AC: {{ current.airConditionerMode }}
                    </ng-container>
                    <ng-template #noAcMode>AC: —</ng-template>
                  </div>
                  <div class="summary-card__subvalue">
                    Fan:
                    <ng-container *ngIf="current.fanMode; else noFanMode">
                      {{ current.fanMode }}
                    </ng-container>
                    <ng-template #noFanMode>—</ng-template>
                  </div>
                </div>
              </div>

              <div class="chart-card">
                <div class="chart-card__header">
                  <div>
                    <h2>Hourly usage (last 24h)</h2>
                    <p>Each bar shows total kWh in that hour.</p>
                  </div>
                  <button class="history-button" (click)="loadHistoryForCurrent()">
                    View 24h history
                  </button>
                </div>

                <div class="chart" *ngIf="selectedHourly().length; else noHourlyTpl">
                  <div class="chart-bars">
                    <div
                      class="chart-bar"
                      *ngFor="let point of selectedHourly()"
                      [style.height]="hourlyBarHeight(point.kWh)"
                    >
                      <span class="chart-bar__value">
                        {{ point.kWh | number : '1.1-2' }}
                      </span>
                      <span class="chart-bar__label">
                        {{ point.hour | date : 'HH:mm' }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <div class="history-card" *ngIf="history().length">
                <div class="history-header">
                  <h3>Last 24h history</h3>
                  <span *ngIf="historyLoading()">Loading…</span>
                </div>
                <table class="history-table">
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>Δ kWh</th>
                      <th>Temp (°C)</th>
                      <th>Humidity (%)</th>
                      <th>AC Mode</th>
                      <th>Fan Mode</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr *ngFor="let row of history()">
                      <td>{{ row.timestamp | date : 'yyyy-MM-dd HH:mm' }}</td>
                      <td>{{ row.deltaKWh !== null ? (row.deltaKWh | number : '1.3-3') : '—' }}</td>
                      <td>
                        {{ row.temperatureC != null ? (row.temperatureC | number : '1.1-1') : '—' }}
                      </td>
                      <td>
                        {{ row.humidity != null ? (row.humidity | number : '1.0-0') : '—' }}
                      </td>
                      <td>{{ row.airConditionerMode || '—' }}</td>
                      <td>{{ row.fanMode || '—' }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </section>
          </div>
        </ng-container>
      </section>

      <ng-template #loadingTpl>
        <div class="state state--center">
          <div class="spinner"></div>
          <p>Loading power data…</p>
        </div>
      </ng-template>

      <ng-template #errorTpl>
        <div class="state state--center state--error">
          <h2>Unable to load data</h2>
          <p>{{ error() }}</p>
          <p>Make sure the backend is running at http://localhost:8000 and try again.</p>
        </div>
      </ng-template>

      <ng-template #noDevicesTpl>
        <div class="state">
          <p>No devices found yet. Once your SmartThings sync runs, devices will appear here.</p>
        </div>
      </ng-template>

      <ng-template #noHourlyTpl>
        <div class="state">
          <p>No hourly data available for the last 24 hours for this device.</p>
        </div>
      </ng-template>
    </main>
  `
})
export class App implements OnInit, OnDestroy {
  private readonly apiBase = 'http://localhost:8000';

  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);

  protected readonly devices = signal<DeviceSummary[]>([]);
  protected readonly hourlyByDevice = signal<HourlyLookup>({});
  protected readonly selectedDeviceKey = signal<string | null>(null);
  protected readonly history = signal<DeviceHistoryPoint[]>([]);
  protected readonly historyLoading = signal(false);

  protected readonly selectedDevice = computed(() => {
    const key = this.selectedDeviceKey();
    if (!key) {
      return null;
    }
    const [deviceName, location] = key.split('||');
    return (
      this.devices().find(
        (d) => d.deviceName === deviceName && d.location === location
      ) ?? null
    );
  });

  protected readonly selectedHourly = computed<DeviceHourlyPoint[]>(() => {
    const key = this.selectedDeviceKey();
    if (!key) {
      return [];
    }
    return this.hourlyByDevice()[key] ?? [];
  });

  protected readonly maxHourlyKWh = computed(() => {
    return this.selectedHourly().reduce((max, p) => (p.kWh > max ? p.kWh : max), 0);
  });

  private refreshTimerId: any | null = null;

  constructor(private readonly http: HttpClient) {}

  ngOnInit(): void {
    this.loadData();
    // Auto-refresh summary data every 15 minutes
    this.refreshTimerId = setInterval(() => this.loadData(), 15 * 60 * 1000);
  }

  ngOnDestroy(): void {
    if (this.refreshTimerId) {
      clearInterval(this.refreshTimerId);
    }
  }

  protected selectDevice(device: DeviceSummary): void {
    const key = this.deviceKey(device.deviceName, device.location);
    this.selectedDeviceKey.set(key);
  }

  protected isSelected(device: DeviceSummary): boolean {
    return this.selectedDeviceKey() === this.deviceKey(device.deviceName, device.location);
  }

  protected formatLastUpdate(iso: string): string {
    const d = new Date(iso);
    return d.toLocaleString();
  }

  protected hourlyBarHeight(kWh: number): string {
    const max = this.maxHourlyKWh();
    if (!max) {
      return '0%';
    }
    const pct = (kWh / max) * 100;
    return `${pct}%`;
  }

  private deviceKey(deviceName: string, location: string): string {
    return `${deviceName}||${location}`;
  }

  private loadData(): void {
    this.loading.set(true);
    this.error.set(null);

    const summary$ = this.http.get<DeviceSummary[]>(`${this.apiBase}/dashboard`);
    const hourly$ = this.http.get<DeviceHourlyResponse[]>(`${this.apiBase}/dashboard/hourly`);

    summary$.subscribe({
      next: (summary) => {
        this.devices.set(summary);

        // Auto-select first device if none selected yet
        if (summary.length && !this.selectedDeviceKey()) {
          const first = summary[0];
          this.selectedDeviceKey.set(this.deviceKey(first.deviceName, first.location));
        }
      },
      error: (err) => {
        this.error.set('Failed to load summary data from backend.');
        console.error(err);
        this.loading.set(false);
      }
    });

    hourly$.subscribe({
      next: (hourlyDevices) => {
        const lookup: { [key: string]: DeviceHourlyPoint[] } = {};
        for (const d of hourlyDevices) {
          const key = this.deviceKey(d.deviceName, d.location);
          // Sort hourly buckets by time ascending
          const sorted = [...d.hourly].sort(
            (a, b) => new Date(a.hour).getTime() - new Date(b.hour).getTime()
          );
          lookup[key] = sorted;
        }
        this.hourlyByDevice.set(lookup);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load hourly data from backend.');
        console.error(err);
        this.loading.set(false);
      }
    });
  }

  protected loadHistoryForCurrent(hours = 24): void {
    const current = this.selectedDevice();
    if (!current) {
      return;
    }

    this.historyLoading.set(true);
    this.history.set([]);

    this.http
      .get<DeviceHistoryPoint[]>(`${this.apiBase}/history`, {
        params: {
          deviceName: current.deviceName,
          location: current.location ?? '',
          hours: `${hours}`,
        },
      })
      .subscribe({
        next: (rows) => {
          this.history.set(rows);
          this.historyLoading.set(false);
        },
        error: (err) => {
          console.error('Failed to load history', err);
          this.historyLoading.set(false);
        }
      });
  }
}
