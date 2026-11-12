# Devices

OpenBrisket does not talk to thermometers directly. It reads a standard set of values, and a device
is supported once something publishes them.

## The standard values

| Value | Unit | Notes |
|---|---|---|
| Pit temperature | °C | The probe at the grate |
| Food probes 1..n | °C | No reading when unplugged |
| Fan output | % | Optional |
| Fan switched on | on/off | Optional |
| Pit target | °C | What the controller is set to hold. Optional |
| Connected | on/off | Whether the reader currently has the device |

In the settings file, each value is mapped to a Home Assistant entity.

## Inkbird ISC-027BW and IBT-26S

`esphome/` holds an [ESPHome](https://esphome.io) config for an ESP32 placed near the smoker:

- `inkbird-isc-027bw.yaml`: the fan controller. Pit, three food probes, fan output, fan on/off, target.
- `inkbird-ibt-26s.yaml`: the six-probe thermometer, as extra probes.
- `bbq-reader.example.yaml`: a complete device that includes both.

Things worth knowing:

- Both are read-only. The firmware never writes to either device.
- The fan controller accepts one Bluetooth connection. If the Inkbird app on your phone is using
  Bluetooth, the reader is locked out, and the other way round. Turn off the app's Bluetooth
  permission and it works over Wi-Fi, alongside the reader.
- If the fan controller stops showing up on Bluetooth with no phone attached, switch it off and on.
- The thermometer hangs up about 31 seconds after each connection. The reader reconnects, so its
  readings arrive in bursts.
- The readings in these files were checked against the Inkbird app. The files were split out of one
  working config and have not been compiled in this split form yet.

## Adding a device

Write one ESPHome file that connects to the device and publishes the standard values under the same
names, add a note here on anything odd about it, and open a pull request. Anything already in Home
Assistant works without firmware: map its entities in the settings file.
