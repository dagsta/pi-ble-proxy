#!/bin/sh
# Baut das Debian-Paket: packaging/build-deb.sh [Zielordner]   (benötigt dpkg-deb)
set -e
cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/pi_ble_proxy/__init__.py)
OUT=${1:-dist}
ROOT=$(mktemp -d)
PKG=$ROOT/pi-ble-proxy_${VERSION}_all
trap 'rm -rf "$ROOT"' EXIT

install -d "$PKG/DEBIAN" "$PKG/usr/lib/python3/dist-packages" "$PKG/usr/bin" \
    "$PKG/lib/systemd/system" "$PKG/usr/share/pi-ble-proxy" "$PKG/usr/share/doc/pi-ble-proxy"
cp -r src/pi_ble_proxy "$PKG/usr/lib/python3/dist-packages/"
find "$PKG" -name __pycache__ -prune -exec rm -rf {} +
printf '#!/bin/sh\nexec python3 -m pi_ble_proxy "$@"\n' > "$PKG/usr/bin/pi-ble-proxy"
chmod 755 "$PKG/usr/bin/pi-ble-proxy"
install -m 644 packaging/pi-ble-proxy.service "$PKG/lib/systemd/system/"
install -m 644 config.example.toml "$PKG/usr/share/pi-ble-proxy/"
install -m 644 README.md "$PKG/usr/share/doc/pi-ble-proxy/"
install -m 644 packaging/copyright "$PKG/usr/share/doc/pi-ble-proxy/copyright"
install -m 755 packaging/postinst packaging/prerm packaging/postrm "$PKG/DEBIAN/"

SIZE=$(du -sk "$PKG" | cut -f1)
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: pi-ble-proxy
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), python3-cryptography, python3-dbus-fast, bluez, avahi-daemon
Installed-Size: $SIZE
Maintainer: Daniel <2291483+dagsta@users.noreply.github.com>
Description: Bluetooth-Proxy für Home Assistant (ESPHome Native API)
 Macht einen Raspberry Pi mit eingebautem Bluetooth zu einem Bluetooth-Proxy,
 den Home Assistant wie einen ESP32-Proxy behandelt: passive Advertisements,
 aktive GATT-Verbindungen zum Schalten, Pairing, verschlüsselt per Noise.
CONTROL

mkdir -p "$OUT"
dpkg-deb --build --root-owner-group "$PKG" "$OUT" >/dev/null
echo "$OUT/pi-ble-proxy_${VERSION}_all.deb"
