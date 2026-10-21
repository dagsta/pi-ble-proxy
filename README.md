# pi-ble-proxy

Deutsch | [English](README.en.md)

Raspberry Pi als Bluetooth-Proxy für Home Assistant. Die Software gibt sich als ESPHome-Gerät mit
`bluetooth_proxy` aus (Native API, Noise-verschlüsselt); HA behandelt den Pi dann wie einen ESP32-Proxy.

> **Hinweis zur Entstehung:** Der Code dieses Projekts wurde **primär von Claude** geschrieben (KI-Modell von
> Anthropic, genutzt über Claude Code). Der Autor hat die Anforderungen vorgegeben, die Software auf echter Hardware
> getestet und Fehler gemeldet; auch die Korrekturen sind mit Claude entstanden. Rechne mit Fehlern und prüfe den Code,
> bevor du ihn für sicherheitsrelevante Zwecke einsetzt.

## Funktionen

- **Passiver Proxy:** Rohdaten-Advertisements, gebündelt alle 100 ms.
- **Aktiver Proxy:** Verbinden, Services, Lesen/Schreiben/Notify, Slot-Verwaltung (Schalten von Schlössern, Lampen usw.).
  Scan-Pause und automatische Wiederholung beim Verbindungsaufbau.
- **Pairing** ("Just Works", wie ein ESP32-Proxy) über einen BlueZ-Agenten, der nur das Gerät annimmt, das gerade
  von uns gekoppelt wird. PIN-/Passkey-Eingabe ist nicht möglich.
- **Scan-Modus** aktiv/passiv, in HA umschaltbar. Passiv braucht BlueZ ab 5.56 mit Advertisement-Monitor (teils
  `bluetoothd --experimental`); fehlt das, fällt der Proxy automatisch auf aktiv zurück und versucht es nicht erneut.
- **Pause nach Verbindungsfehlern:** ab dem zweiten Fehler in Folge pro Gerät 5 s, 10 s, … (höchstens 60 s), damit ein
  nicht antwortendes Gerät nicht ununterbrochen angefragt wird (`connect_backoff_step`, 0 = aus).
- **Hinweise im Log** bei blockiertem Bluetooth (rfkill) und bei Unterspannung am Pi.
- **Überwachung:** erkennt Neustart von `bluetoothd`, ausgeschalteten oder entfernten Adapter und ausbleibende
  Advertisements; repariert, was möglich ist, und beendet sich sonst mit Exit-Code 1, damit systemd neu startet.
- **Diagnose:** Statistik-Zeile im Log und drei Sensoren in HA (CPU-Temperatur, Advertisements, aktive Verbindungen).

## Hinweise

- **Ohne Gewähr.** Das Projekt kann Türschlösser und andere Geräte steuern. Setze es nicht als einzige Zugangsmöglichkeit
  ein und teste es vor sicherheitsrelevanter Nutzung selbst. Die Lizenz (MIT) schließt jede Haftung aus.
- **Keine Verbindung zu anderen Projekten.** Es besteht keine Verbindung zu ESPHome, Home Assistant, Nabu Casa oder
  SwitchBot; die Namen sind Marken ihrer Inhaber und stehen hier nur zur Beschreibung der Kompatibilität. Gegenüber
  Home Assistant meldet sich der Proxy als ESPHome-kompatibles Gerät (mDNS-Typ `_esphomelib._tcp`, Versionsangabe
  `2025.10.0`), damit dessen ESPHome-Integration ihn akzeptiert.

## Stand der Prüfung

Auf einem Raspberry Pi 4 (Raspberry Pi OS auf Debian 12, BlueZ 5.66) mit einem SwitchBot Lock in Home Assistant geprüft:
Weiterleiten von Advertisements, Verbinden, Schreiben und Notifications, dauerhaft stabile Verbindung zu HA, die
Diagnose-Sensoren in HA sowie Installation und Update über das Debian-Paket. Der Rückfall auf den aktiven Scan, wenn
BlueZ keinen Advertisement-Monitor anbietet, ist ebenfalls im Betrieb beobachtet.

Nur gegen ein simuliertes BlueZ getestet (nicht auf echter Hardware): Pairing, der passive Scan-Modus, die Pause nach
Verbindungsfehlern, die Wiederherstellung nach einem `bluetoothd`-Neustart und die rfkill-/Unterspannungs-Hinweise
(Erkennung über sysfs).

## Installation (Debian-Paket)

```
packaging/build-deb.sh                      # erzeugt dist/pi-ble-proxy_<Version>_all.deb
scp dist/*.deb BENUTZER@PI-HOSTNAME:
sudo apt install ./pi-ble-proxy_*_all.deb   # zieht python3-cryptography, python3-dbus-fast, avahi-daemon nach
```

Das Paket legt den Benutzer `bleproxy` an, erzeugt `/etc/pi-ble-proxy/config.toml` mit neuem Schlüssel (nur wenn die
Datei fehlt), schreibt den mDNS-Eintrag für Avahi und startet den Dienst. Den Schlüssel trägst du in HA beim Hinzufügen
des ESPHome-Geräts ein. Nach Änderungen an Name oder Port: `pi-ble-proxy avahi | sudo tee /etc/avahi/services/pi-ble-proxy.service`.

**Umstieg von einer manuellen Installation:** vorher `sudo systemctl disable --now pi-ble-proxy`, dann
`sudo rm /etc/systemd/system/pi-ble-proxy.service` und `sudo rm -r /opt/pi-ble-proxy`. Die vorhandene Konfiguration
bleibt erhalten.

## Konfiguration

Siehe `config.example.toml`. Wichtig im Dauerbetrieb:

- `[logging] level = "INFO"` – DEBUG schreibt eine Zeile pro Notification.
- `/etc/pi-ble-proxy/config.toml` enthält den Schlüssel: Rechte 640, Besitzer `root:bleproxy`.
- WLAN am Pi abschalten und per Ethernet anbinden (WLAN und Bluetooth teilen sich Chip und Antenne).
- `[watchdog]` steuert die Überwachung, `pause_scan_on_connect` die Scan-Pause beim Verbinden.

## Abhängigkeiten

Laufzeit: `python3-cryptography` (X25519, ChaCha20-Poly1305) und `python3-dbus-fast` (BlueZ per D-Bus).
Alles andere ist Standardbibliothek. Ohne `python3-dbus-fast` läuft der Proxy nur passiv.
Nur für Tests: `pytest`, `aioesphomeapi`, `noiseprotocol`, `dbus-fast`.

## Werkzeug zum Testen

`tools/proxy_client.py` spricht die API direkt an (Scan, Pairing, Services), ohne Home Assistant.
Es läuft auf jedem Rechner im Netz mit `pip install aioesphomeapi`; Details in der Kopfzeile der Datei.

## Lizenz

MIT, siehe [LICENSE](LICENSE).

## Tests

```
python -m venv --system-site-packages .venv
.venv/bin/pip install pytest aioesphomeapi noiseprotocol dbus-fast
.venv/bin/python -m pytest
```

Die Tests laufen gegen den echten `aioesphomeapi`-Client (denselben wie HA) und ein simuliertes BlueZ auf einem
privaten D-Bus (`dbus-daemon` muss installiert sein).
