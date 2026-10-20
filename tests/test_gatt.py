"""Aktive Verbindungen: aioesphomeapi-Client -> Proxy -> BlueZ (simuliert auf privatem D-Bus)."""

import asyncio
import base64
import os
import shutil
import subprocess

import pytest

aioesphomeapi = pytest.importorskip("aioesphomeapi")
pytest.importorskip("dbus_fast")

from fake_bluez import CHR_NOTIFY, CHR_WRITE, FakeBluez  # noqa: E402
from test_e2e import FakeScanner, make_config  # noqa: E402

from pi_ble_proxy.bluez import BluezBackend  # noqa: E402
from pi_ble_proxy.server import ApiServer  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("dbus-daemon"), reason="dbus-daemon fehlt")
LOCK = 0xAABBCCDDEEFF
KEY = os.urandom(32)


@pytest.fixture
def bus_address():
    proc = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                            stdout=subprocess.PIPE, text=True)
    yield proc.stdout.readline().strip()
    proc.terminate()
    proc.wait()


def scenario(bus_address, test, *, device_known=True, **cfg):
    async def main():
        fake = FakeBluez(device_known)
        await fake.start(bus_address)
        server = ApiServer(make_config(encryption_key=KEY, **cfg), FakeScanner(),
                           BluezBackend(0, bus_address=bus_address))
        await server.start()
        client = aioesphomeapi.APIClient("127.0.0.1", server.port, None,
                                         noise_psk=base64.b64encode(KEY).decode())
        await client.connect(login=True)
        try:
            await asyncio.wait_for(test(client, fake, server), 20)
        finally:
            await client.disconnect()
            await server.stop()
            fake.stop()

    asyncio.run(main())


async def connect(client, address=LOCK):
    loop = asyncio.get_running_loop()
    state = loop.create_future()
    states = []

    def cb(connected, mtu, error):
        states.append((connected, mtu, error))
        if not state.done():
            state.set_result(None)

    cancel = await client.bluetooth_device_connect(address, cb, timeout=10, address_type=1, feature_flags=63)
    await state
    return cancel, states


def test_feature_flags_announce_active_connections(bus_address):
    async def test(client, fake, server):
        assert (await client.device_info()).bluetooth_proxy_feature_flags == 127
        assert fake.discovering and fake.filter == {"Transport": "le", "DuplicateData": True}
        assert server.scanner.discover is False  # BlueZ scannt, mgmt hört nur mit

    scenario(bus_address, test)


def test_connect_services_read_write_notify_disconnect(bus_address):
    async def test(client, fake, server):
        free = []
        client.subscribe_bluetooth_connections_free(lambda f, limit, alloc: free.append((f, limit, list(alloc))))
        await asyncio.sleep(0.1)
        assert free[-1] == (3, 3, [])

        cancel, states = await connect(client)
        assert states[0] == (True, 247, 0)  # MTU kommt aus BlueZ
        await asyncio.sleep(0.1)
        assert free[-1] == (2, 3, [LOCK])

        services = await client.bluetooth_gatt_get_services(LOCK)
        svc = services.services[0]
        assert svc.handle == 0x0A
        chars = {c.handle: c for c in svc.characteristics}
        assert set(chars) == {0x0B, 0x0D}
        assert chars[0x0B].properties == 0x04 | 0x08  # write + write-without-response
        assert chars[0x0D].properties == 0x02 | 0x10  # read + notify
        assert chars[0x0D].descriptors[0].handle == 0x0F
        assert (svc.uuid) == "cba20d00-224d-11e6-9fb8-0002a5d5c51b"

        assert bytes(await client.bluetooth_gatt_read(LOCK, 0x0D)) == b"\x01\x02"
        await client.bluetooth_gatt_write(LOCK, 0x0B, b"\x57\x0f\x4e", response=True)
        await client.bluetooth_gatt_write(LOCK, 0x0B, b"\x57\x01", response=False)
        await asyncio.sleep(0.1)
        assert fake.write_char.written == [(b"\x57\x0f\x4e", "request"), (b"\x57\x01", "command")]
        assert bytes(await client.bluetooth_gatt_read_descriptor(LOCK, 0x0F)) == b"\x00\x00"

        got = []
        stop, _ = await client.bluetooth_gatt_start_notify(LOCK, 0x0D, lambda h, d: got.append((h, bytes(d))))
        assert fake.notify_char.notifying
        fake.notify_char.notify(b"\x01\x80")
        await asyncio.sleep(0.1)
        assert got == [(0x0D, b"\x01\x80")]
        await stop()  # fire-and-forget im Client
        await asyncio.sleep(0.2)
        assert not fake.notify_char.notifying

        await client.bluetooth_device_disconnect(LOCK)
        await asyncio.sleep(0.3)
        assert "Disconnect" in fake.calls
        assert free[-1] == (3, 3, [])

    scenario(bus_address, test)


@pytest.mark.parametrize("flags, response, expected", [
    (["write"], False, "request"),                       # SwitchBot-Fall: nur "write", HA schreibt ohne Antwort
    (["write-without-response"], True, "command"),
    (["write", "write-without-response"], False, "command"),
    (["write", "write-without-response"], True, "request"),
])
def test_write_type_follows_characteristic_flags(bus_address, flags, response, expected):
    async def test(client, fake, server):
        await connect(client)
        await client.bluetooth_gatt_write(LOCK, 0x0B, b"\x57", response=response)
        await asyncio.sleep(0.1)
        assert fake.write_char.written == [(b"\x57", expected)]

    scenario_flags(bus_address, test, flags)


def scenario_flags(bus_address, test, flags):
    # Flags müssen vor dem Verbinden gesetzt sein (BlueZ liefert sie beim Service-Aufbau)
    original = FakeBluez.__init__

    def patched(self, *a, **kw):
        original(self, *a, **kw)
        self.write_char._flags = flags

    FakeBluez.__init__ = patched
    try:
        scenario(bus_address, test)
    finally:
        FakeBluez.__init__ = original


def test_cccd_write_by_client_is_acknowledged_not_forwarded(bus_address):
    """HA schreibt den CCCD selbst (wie bei einem ESP32); BlueZ würde das mit NotPermitted ablehnen."""
    async def test(client, fake, server):
        await connect(client)
        got = []
        await client.bluetooth_gatt_start_notify(LOCK, 0x0D, lambda h, d: got.append(bytes(d)))
        await client.bluetooth_gatt_write_descriptor(LOCK, 0x0F, b"\x01\x00", wait_for_response=True)
        fake.notify_char.notify(b"\x01")
        await asyncio.sleep(0.1)
        assert got == [b"\x01"] and fake.notify_char.notifying

    scenario(bus_address, test)


def test_errors_are_reported_to_client(bus_address):
    async def test(client, fake, server):
        # nicht verbunden
        with pytest.raises(aioesphomeapi.core.BluetoothGATTAPIError):
            await client.bluetooth_gatt_read(LOCK, 0x0D)
        cancel, _ = await connect(client)
        with pytest.raises(aioesphomeapi.core.BluetoothGATTAPIError):  # unbekanntes Handle
            await client.bluetooth_gatt_read(LOCK, 0x99)
        fake.fail_write = True
        with pytest.raises(aioesphomeapi.core.BluetoothGATTAPIError):
            await client.bluetooth_gatt_write(LOCK, 0x0B, b"x", response=True)
        cancel()

    scenario(bus_address, test)


def test_failed_connect_releases_slot_and_reports_error(bus_address):
    async def test(client, fake, server):
        fake.fail_connect = True
        _, states = await connect(client)
        assert states[0][0] is False and states[0][2] != 0
        assert server.free_slots == 3
        assert fake.calls.count("Connect") == 3  # abort-by-local wird dreimal versucht
        assert "Disconnect" in fake.calls  # Aufräumen bei Fehler

    scenario(bus_address, test)


def test_remote_disconnect_is_forwarded_and_slot_freed(bus_address):
    async def test(client, fake, server):
        _, states = await connect(client)
        fake.device.drop()
        await asyncio.sleep(0.2)
        assert states[-1][0] is False
        assert server.free_slots == 3

    scenario(bus_address, test)


def test_unknown_device_triggers_discovery_fallback(bus_address):
    async def test(client, fake, server):
        loop = asyncio.get_running_loop()
        loop.call_later(0.6, fake.appear)
        _, states = await connect(client)
        assert states[0][0] is True
        # Start + Fallback (InProgress, wird toleriert) + Wiederaufnahme nach der Scan-Pause
        assert fake.calls.count("StartDiscovery") == 3

    scenario(bus_address, test, device_known=False)


def test_scan_is_paused_during_connect_and_resumed(bus_address):
    async def test(client, fake, server):
        await connect(client)
        calls = fake.calls
        i = calls.index("Connect")
        assert calls[i - 1] == "StopDiscovery" and calls[i + 1] == "StartDiscovery"
        assert fake.discovering

    scenario(bus_address, test)


def test_abort_by_local_is_retried_internally(bus_address):
    async def test(client, fake, server):
        fake.abort_first = 2
        _, states = await connect(client)
        assert states == [(True, 247, 0)]  # HA sieht nur den erfolgreichen Versuch
        assert fake.calls.count("Connect") == 3
        assert fake.calls.count("StopDiscovery") == 1  # Scan wird nur einmal pausiert

    scenario(bus_address, test)


def test_scan_pause_can_be_disabled(bus_address):
    async def main():
        fake = FakeBluez()
        await fake.start(bus_address)
        backend = BluezBackend(0, bus_address=bus_address, pause_scan=False)
        await backend.start()
        await backend.start_discovery()
        conn = await backend.connect(LOCK, 1, lambda: None)
        assert "StopDiscovery" not in fake.calls
        await conn.disconnect()
        await backend.stop()
        fake.stop()

    asyncio.run(main())


def test_slot_limit(bus_address):
    async def test(client, fake, server):
        _, first = await connect(client)
        _, second = await connect(client, address=0x112233445566)
        assert first[0][0] is True
        assert second[0] == (False, 0, 133)

    scenario(bus_address, test, max_gatt_connections=1)


def test_pair_and_unpair(bus_address):
    async def test(client, fake, server):
        assert (await client.bluetooth_device_pair(LOCK)).paired is True
        assert "Pair" in fake.calls
        assert (await client.bluetooth_device_unpair(LOCK)).success is True
        assert any(c.startswith("RemoveDevice") for c in fake.calls)

    scenario(bus_address, test)


def test_client_disconnect_releases_connection(bus_address):
    async def test(client, fake, server):
        await connect(client)
        await client.disconnect()
        await asyncio.sleep(0.3)
        assert server.free_slots == 3 and "Disconnect" in fake.calls

    scenario(bus_address, test)
