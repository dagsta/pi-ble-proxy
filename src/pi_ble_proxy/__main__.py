"""Kommandozeile: ``python -m pi_ble_proxy {run,genkey,avahi}``."""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import secrets
import signal
import sys

from . import config as cfgmod
from .server import ApiServer


def _scanner(cfg: cfgmod.Config, simulate: bool):
    if simulate:
        from .scanner import SimulatedScanner

        return SimulatedScanner()
    from .mgmt import MgmtScanner

    return MgmtScanner(cfg.adapter_index)


def _gatt_backend(cfg: cfgmod.Config):
    try:
        from .bluez import BluezBackend
    except ImportError as exc:
        logging.getLogger("pi_ble_proxy").warning(
            "Aktive Verbindungen deaktiviert (%s) – apt install python3-dbus-fast", exc)
        return None
    return BluezBackend(cfg.adapter_index, pause_scan=cfg.pause_scan_on_connect, scan_mode=cfg.scan_mode)


async def _run(cfg: cfgmod.Config, simulate: bool) -> int:
    server = ApiServer(cfg, _scanner(cfg, simulate), None if simulate else _gatt_backend(cfg))
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stopper = asyncio.ensure_future(stop.wait())
    await asyncio.wait([stopper, server.failed], return_when=asyncio.FIRST_COMPLETED)
    stopper.cancel()
    await server.stop()
    return 1 if server.failed.done() and server.failed.exception() else 0  # 1: systemd startet neu


AVAHI_TEMPLATE = """<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>{name}</name>
  <service>
    <type>_esphomelib._tcp</type>
    <port>{port}</port>
    <txt-record>friendly_name={friendly_name}</txt-record>
    <txt-record>version={version}</txt-record>
    <txt-record>mac={mac}</txt-record>
    <txt-record>platform=ESP32</txt-record>
    <txt-record>board=pi-ble-proxy</txt-record>
    <txt-record>network=ethernet</txt-record>
    <txt-record>api_encryption=Noise_NNpsk0_25519_ChaChaPoly_SHA256</txt-record>
  </service>
</service-group>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pi_ble_proxy", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    for name, help_ in (("run", "Proxy starten"), ("avahi", "Avahi-Service-Datei ausgeben")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("-c", "--config", default="/etc/pi-ble-proxy/config.toml")
        if name == "run":
            p.add_argument("--simulate", action="store_true", help="künstliche Advertisements statt Hardware")
    sub.add_parser("genkey", help="neuen Verschlüsselungsschlüssel erzeugen")
    args = parser.parse_args(argv)

    if args.cmd == "genkey":
        print(base64.b64encode(secrets.token_bytes(32)).decode())
        return 0
    if args.cmd is None:
        parser.print_help()
        return 2
    try:
        cfg = cfgmod.load(args.config)
    except cfgmod.ConfigError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    if args.cmd == "avahi":
        from .server import ESPHOME_VERSION

        print(AVAHI_TEMPLATE.format(
            name=cfg.name, port=cfg.port, friendly_name=cfg.friendly_name,
            version=ESPHOME_VERSION, mac=cfg.mac.replace(":", "").lower(),
        ), end="")
        return 0

    logging.basicConfig(level=cfg.log_level, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_run(cfg, args.simulate))
    except Exception as exc:  # klare Meldung statt Traceback bei Hardware-/Rechteproblemen
        logging.getLogger("pi_ble_proxy").error("%s", exc)
        logging.debug("Details", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
