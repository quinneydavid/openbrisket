# Monitoring

OpenBrisket serves the cook as Prometheus metrics at `/metrics`. This folder has what goes around it: a
Prometheus config, alert rules and a Grafana dashboard.

The Cook page already draws the curve and the app already sends its own notes, so none of this is needed to
run a cook. It is here because a fourteen-hour deployment with an SLO deserves a dashboard, and because cooks
kept in Prometheus can be laid over each other.

## With Docker Compose

1. Start the app, open Settings, and under **Metrics** press **Mint a token for Prometheus**.
2. Save that token as the only line of `monitoring/prometheus/token`.
3. `docker compose --profile monitoring up -d`
4. Open Grafana on port 3000 (first sign-in is `admin` / `admin`; it asks for a new password). The dashboard is
   under Dashboards > OpenBrisket.

Prometheus listens on `127.0.0.1:9090` only, and keeps two years of data.

## With a Prometheus and Grafana you already run

The **Mint a token for Prometheus** button prints a scrape job with the address and token filled in. It is this:

```yaml
scrape_configs:
  - job_name: openbrisket
    metrics_path: /metrics
    authorization:
      credentials: <a token with read rights>
    static_configs:
      - targets: ["openbrisket.example:8321"]
```

Import `grafana/dashboards/openbrisket.json` in Grafana (Dashboards > New > Import) and pick your Prometheus
data source. Add `prometheus/alerts.yml` to your `rule_files` if you want the alerts.

On Kubernetes with the Prometheus operator, the same thing is a `ServiceMonitor` with `authorization` pointing at
a Secret that holds the token.

## The token

`/metrics` needs a token like every other address in the app. A token with `read` rights is enough and is the
right one to use: it can look at the cook and nothing else. Revoke it on the Settings page.

## Alerts

| Alert | Fires when |
|---|---|
| `PitFeedDown` | A cook is open and no reading has arrived for five minutes |
| `PitDrift` | The pit has been outside the recipe's range for 15 minutes |
| `FireDying` | The fan has been at 90% or more for ten minutes and the pit is still low |
| `ErrorBudgetBurn` | Less than an hour is left between the estimated finish and serving |
| `DinnerSLOBreach` | The estimated finish is after the serving time |
| `CookCheckFailing` | One of the status checks has been failing for ten minutes |
| `HoldNearFloor` | The held meat will reach its floor temperature within the hour |

## Metrics

Temperatures are in degrees Celsius. A value that is not known is left out, so a probe that is unplugged has no
series, and the `openbrisket_cook_*` and `openbrisket_hold_*` metrics exist only while a cook is open.

| Metric | Meaning |
|---|---|
| `openbrisket_info` | Always 1; the running version is in the label. |
| `openbrisket_feed_up` | 1 when a fresh reading has arrived and the reader says it is connected. |
| `openbrisket_last_reading_timestamp_seconds` | When the last reading was taken. |
| `openbrisket_pit_temperature_celsius` | Pit temperature. |
| `openbrisket_pit_setpoint_celsius` | What the fan controller is set to hold. |
| `openbrisket_fan_output_percent` | Fan output, 0 to 100. |
| `openbrisket_fan_on` | 1 when the fan is switched on. |
| `openbrisket_probe_temperature_celsius` | Food probe temperature, one series per probe that is reading. |
| `openbrisket_ambient_temperature_celsius` | Outdoor temperature, when a sensor is set. |
| `openbrisket_ambient_humidity_percent` | Outdoor relative humidity, when a sensor is set. |
| `openbrisket_rain` | The rain sensor's own number (mm, mm/h), or 1/0 for a wet/dry sensor. |
| `openbrisket_cooks` | Cooks in the log, by how they ended (open, done, abandoned). |
| `openbrisket_cook_open` | 1 while a cook is open. |
| `openbrisket_cook_info` | Always 1 while a cook is open; what is cooking is in the labels. |
| `openbrisket_cook_phase` | 1 for the phase the cook is in, 0 for the others. |
| `openbrisket_cook_wrapped` | 1 once the meat has been wrapped. |
| `openbrisket_cook_weight_kilograms` | Raw weight of the meat. |
| `openbrisket_cook_opened_timestamp_seconds` | When the cook was opened. |
| `openbrisket_cook_meat_on_timestamp_seconds` | When the meat went on. |
| `openbrisket_cook_serve_timestamp_seconds` | The serving time: the SLO. |
| `openbrisket_cook_estimated_off_timestamp_seconds` | When the meat is expected to come off. |
| `openbrisket_cook_error_budget_seconds` | Time between the estimated finish and serving. The rest is paid out of this; negative means dinner is late. |
| `openbrisket_cook_meat_temperature_celsius` | The coldest probe still in the meat, which decides everything. |
| `openbrisket_cook_probe_in_meat` | 1 for a probe of this cook that is in the meat, 0 once it is pulled. |
| `openbrisket_cook_pit_target_celsius` | Pit temperature the recipe declares. |
| `openbrisket_cook_pit_tolerance_celsius` | How far the pit may sit from the recipe's target before it counts as drift. |
| `openbrisket_cook_meat_target_celsius` | Meat temperature the recipe finishes at. |
| `openbrisket_cook_wrap_celsius` | Meat temperature at which the recipe wraps. |
| `openbrisket_cook_check_status` | Status checks on the open cook: 1 pass, 0 pending, -1 fail. |
| `openbrisket_cook_events_total` | Events logged in the open cook, by kind. Starts again with each cook. |
| `openbrisket_hold_seconds` | How long the meat has been resting or held. |
| `openbrisket_hold_floor_celsius` | The temperature the meat must stay above during the hold. |
| `openbrisket_hold_rate_celsius_per_hour` | How fast the held meat is cooling (negative) or warming. |
| `openbrisket_hold_until_floor_seconds` | At the current rate, how long until the meat reaches the floor. |

Labels: `openbrisket_probe_temperature_celsius` has `probe` (its number) and `name` (the name given in Settings).
`openbrisket_cook_info` has `cook`, `recipe`, `version`, `cut`, `style` and `stack`.
`openbrisket_cook_phase` has `phase`: `preheating`, `cooking`, `stall` or `resting`.
`openbrisket_cook_check_status` has `check`, `openbrisket_cook_events_total` has `event`, and
`openbrisket_cooks` has `status`.
