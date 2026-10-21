# pi-ble-proxy

[Deutsch](README.de.md) | English

A Raspberry Pi as a Bluetooth proxy for Home Assistant. The software presents itself as an ESPHome device with
`bluetooth_proxy` (Native API, Noise-encrypted), so Home Assistant treats the Pi like an ESP32 proxy.

> **Note on how this was made:** The code in this project was written **primarily by Claude** (an AI model by
> Anthropic, used through Claude Code). The author set the requirements, tested the software on real hardware and
> reported bugs; the fixes were again produced with Claude. Expect bugs, and review the code before you rely on it for
> anything security-relevant.

## Features

- **Passive proxy:** raw advertisements, batched every 100 ms.
- **Active proxy:** connect, services, read/write/notify, connection slot management (switching locks, lights, etc.).
  Pauses scanning while connecting and retries automatically.
- **Pairing** ("Just Works", like an ESP32 proxy) through a BlueZ agent that only accepts the device we are currently
  pairing. PIN/passkey entry is not possible.
- **Scan mode** active/passive, switchable from Home Assistant. Passive needs BlueZ 5.56 or newer with the
  Advertisement Monitor API (sometimes `bluetoothd --experimental`); if it is missing, the proxy falls back to active
  scanning and does not try again.
- **Back-off after connection failures:** from the second consecutive failure per device, 5 s, 10 s, … (at most 60 s)
  so an unresponsive device is not hammered continuously (`connect_backoff_step`, 0 = off).
- **Hints in the log** for blocked Bluetooth (rfkill) and for under-voltage on the Pi.
- **Supervision:** detects a `bluetoothd` restart, a powered-off or removed adapter, and missing advertisements;
  repairs what it can and otherwise exits with code 1 so systemd restarts it.
- **Diagnostics:** a statistics line in the log and three sensors in Home Assistant (CPU temperature, advertisements,
  active connections).

## Notes

- **No warranty.** This project can control door locks and other devices. Do not use it as the only way to get in, and
  test it yourself before any security-relevant use. The license (MIT) excludes all liability.
- **No affiliation.** There is no connection to ESPHome, Home Assistant, Nabu Casa or SwitchBot; the names are
  trademarks of their owners and appear here only to describe compatibility. Towards Home Assistant the proxy
  identifies itself as an ESPHome-compatible device (mDNS type `_esphomelib._tcp`, reported version `2025.10.0`) so
  that its ESPHome integration accepts it.

## Verification status

Verified on a Raspberry Pi 4 (Raspberry Pi OS based on Debian 12, BlueZ 5.66) with a SwitchBot Lock in Home
Assistant: forwarding of advertisements, connecting, writing and notifications, a permanently stable connection to
Home Assistant, the diagnostic sensors in Home Assistant, and installing and updating through the Debian package. The
fallback to active scanning when BlueZ offers no Advertisement Monitor has also been observed in operation.

Tested only against a simulated BlueZ (not on real hardware): pairing, the passive scan mode, the back-off after
connection failures, recovery after a `bluetoothd` restart, and the rfkill / under-voltage hints (detected via sysfs).

## Installation (Debian package)

```
packaging/build-deb.sh                      # creates dist/pi-ble-proxy_<version>_all.deb
scp dist/*.deb USER@PI-HOSTNAME:
sudo apt install ./pi-ble-proxy_*_all.deb   # pulls in python3-cryptography, python3-dbus-fast, avahi-daemon
```

The package creates the user `bleproxy`, creates `/etc/pi-ble-proxy/config.toml` with a fresh key (only if the file is
missing), writes the mDNS entry for Avahi and starts the service. You enter the key in Home Assistant when adding the
ESPHome device. After changing name or port: `pi-ble-proxy avahi | sudo tee /etc/avahi/services/pi-ble-proxy.service`.

**Switching from a manual installation:** first `sudo systemctl disable --now pi-ble-proxy`, then
`sudo rm /etc/systemd/system/pi-ble-proxy.service` and `sudo rm -r /opt/pi-ble-proxy`. Your existing configuration is
kept.

## Configuration

See `config.example.toml`. Important for permanent operation:

- `[logging] level = "INFO"` – DEBUG writes one line per notification.
- `/etc/pi-ble-proxy/config.toml` contains the key: mode 640, owner `root:bleproxy`.
- Turn Wi-Fi off on the Pi and use Ethernet (Wi-Fi and Bluetooth share the chip and the antenna).
- `[watchdog]` controls the supervision, `pause_scan_on_connect` the scan pause while connecting.

## Dependencies

Runtime: `python3-cryptography` (X25519, ChaCha20-Poly1305) and `python3-dbus-fast` (BlueZ over D-Bus).
Everything else is the standard library. Without `python3-dbus-fast` the proxy runs in passive-only mode.
Tests only: `pytest`, `aioesphomeapi`, `noiseprotocol`, `dbus-fast`.

## Tool for testing

`tools/proxy_client.py` talks to the API directly (scan, pairing, services), without Home Assistant. It runs on any
machine on the network with `pip install aioesphomeapi`; see the header of the file for details.

## License

MIT, see [LICENSE](LICENSE).

## Tests

```
python -m venv --system-site-packages .venv
.venv/bin/pip install pytest aioesphomeapi noiseprotocol dbus-fast
.venv/bin/python -m pytest
```

The tests run against the real `aioesphomeapi` client (the same one Home Assistant uses) and a simulated BlueZ on a
private D-Bus (`dbus-daemon` must be installed).
