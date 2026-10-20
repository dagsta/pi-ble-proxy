#!/usr/bin/env python3
"""Testwerkzeug: spricht die Proxy-API direkt an (wie Home Assistant), z. B. zum Pairing.

Läuft auf jedem Rechner im Netz, braucht ``pip install aioesphomeapi`` (nicht auf dem Pi nötig).

  python proxy_client.py HOST KEY pair    AA:BB:CC:DD:EE:FF
  python proxy_client.py HOST KEY unpair  AA:BB:CC:DD:EE:FF
  python proxy_client.py HOST KEY services AA:BB:CC:DD:EE:FF
  python proxy_client.py HOST KEY scan  [Sekunden]

Pairing nur mit gestopptem Home Assistant-Eintrag für das Gerät testen, damit nichts parallel verbindet.
"""

import argparse
import asyncio
import sys

import aioesphomeapi


def mac_to_int(mac: str) -> int:
    return int(mac.replace(":", ""), 16)


async def run(args) -> int:
    client = aioesphomeapi.APIClient(args.host, args.port, None, noise_psk=args.key)
    await client.connect(login=True)
    try:
        info = await client.device_info()
        print(f"Proxy: {info.name}, Adapter {info.bluetooth_mac_address}, "
              f"Features {info.bluetooth_proxy_feature_flags} (Pairing={'ja' if info.bluetooth_proxy_feature_flags & 8 else 'nein'})")
        if args.cmd == "scan":
            seen = {}
            client.subscribe_bluetooth_le_raw_advertisements(
                lambda r: [seen.__setitem__(a.address, a.rssi) for a in r.advertisements])
            await asyncio.sleep(args.seconds)
            for addr, rssi in sorted(seen.items(), key=lambda kv: -kv[1]):
                print(f"  {':'.join(f'{b:02X}' for b in addr.to_bytes(6, 'big'))}  {rssi} dBm")
            return 0

        address = mac_to_int(args.mac)
        if args.cmd == "pair":
            result = await client.bluetooth_device_pair(address, timeout=60)
            print(f"Pairing: {'ERFOLGREICH' if result.paired else 'FEHLGESCHLAGEN'} (Fehlercode {result.error})")
            return 0 if result.paired else 1
        if args.cmd == "unpair":
            result = await client.bluetooth_device_unpair(address)
            print(f"Unpair: {'ok' if result.success else 'fehlgeschlagen'} (Fehlercode {result.error})")
            return 0 if result.success else 1
        if args.cmd == "services":
            done = asyncio.get_running_loop().create_future()
            client_flags = info.bluetooth_proxy_feature_flags
            await client.bluetooth_device_connect(
                address, lambda c, mtu, err: done.set_result((c, mtu, err)) if not done.done() else None,
                timeout=30, feature_flags=client_flags, address_type=args.address_type)
            connected, mtu, err = await done
            print(f"Verbunden: {connected}, MTU {mtu}, Fehler {err}")
            if connected:
                for svc in (await client.bluetooth_gatt_get_services(address)).services:
                    print(f"Service {svc.uuid} (Handle {svc.handle})")
                    for ch in svc.characteristics:
                        print(f"  Char {ch.uuid} Handle {ch.handle} Props 0x{ch.properties:02x}")
                await client.bluetooth_device_disconnect(address)
            return 0 if connected else 1
    finally:
        await client.disconnect()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host")
    p.add_argument("key", help="Base64-Schlüssel aus config.toml")
    p.add_argument("--port", type=int, default=6053)
    p.add_argument("--address-type", type=int, default=0, help="0 public, 1 random (Standard 0)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("pair", "unpair", "services"):
        sub.add_parser(name).add_argument("mac")
    sub.add_parser("scan").add_argument("seconds", nargs="?", type=int, default=10)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
