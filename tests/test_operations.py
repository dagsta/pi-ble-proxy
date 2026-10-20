"""Betrieb: Sensoren, Scanner-Modus, Pairing-Agent, Überwachung und Wiederherstellung."""

import asyncio
import base64
import logging
import os
import shutil
import subprocess

import pytest

aioesphomeapi = pytest.importorskip("aioesphomeapi")
pytest.importorskip("dbus_fast")

from dbus_fast import Message, MessageType  # noqa: E402
from fake_bluez import AGENT_PATH, DEV, FakeBluez  # noqa: E402
from test_e2e import FakeScanner, make_config  # noqa: E402

from pi_ble_proxy import stats  # noqa: E402
from pi_ble_proxy.bluez import BluezBackend  # noqa: E402
from pi_ble_proxy.scanner import Advertisement  # noqa: E402
from pi_ble_proxy.server import ApiServer  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("dbus-daemon"), reason="dbus-daemon fehlt")
KEY = os.urandom(32)
LOCK = 0xAABBCCDDEEFF


@pytest.fixture
def bus_address():
    proc = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                            stdout=subprocess.PIPE, text=True)
    yield proc.stdout.readline().strip()
    proc.terminate()
    proc.wait()


def scenario(bus_address, test, *, scanner=None, with_bluez=True, backend_args=None, **cfg):
    async def main():
        fake = None
        if with_bluez:
            fake = FakeBluez()
            await fake.start(bus_address)
        server = ApiServer(make_config(encryption_key=KEY, **cfg), scanner or FakeScanner(),
                           BluezBackend(0, bus_address=bus_address, **(backend_args or {})) if with_bluez else None)
        await server.start()
        client = aioesphomeapi.APIClient("127.0.0.1", server.port, None,
                                         noise_psk=base64.b64encode(KEY).decode())
        await client.connect(login=True)
        try:
            await asyncio.wait_for(test(client, fake, server), 20)
        finally:
            await client.disconnect()
            await server.stop()
            if fake:
                fake.stop()

    asyncio.run(main())


# --- Sensoren ------------------------------------------------------------------------

def test_sensors_report_values(bus_address, tmp_path, monkeypatch):
    thermal = tmp_path / "temp"
    thermal.write_text("47500\n")
    monkeypatch.setattr(stats, "THERMAL", thermal)

    async def test(client, fake, server):
        entities, _ = await client.list_entities_services()
        by_key = {e.key: e for e in entities}
        assert {e.name for e in by_key.values()} == {"CPU Temperature", "Advertisements Received", "Active Connections"}
        cpu = next(e for e in entities if e.object_id == "cpu_temperature")
        assert (cpu.unit_of_measurement, cpu.device_class, cpu.entity_category.name) == ("°C", "temperature", "DIAGNOSTIC")

        states = {}
        client.subscribe_states(lambda s: states.__setitem__(s.key, s))
        await asyncio.sleep(0.2)
        assert states[cpu.key].state == pytest.approx(47.5)
        server.scanner.emit(Advertisement(1, 0, -50, b""))
        server.scanner.emit(Advertisement(2, 0, -50, b""))
        for session in server.sessions:  # statt auf den 30-s-Takt zu warten
            session.send_states()
        await asyncio.sleep(0.2)
        adv = next(e for e in entities if e.object_id == "advertisements_received")
        assert states[adv.key].state == 2

    scenario(bus_address, test, with_bluez=False)


def test_missing_cpu_temperature_is_reported_as_missing(bus_address, tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "THERMAL", tmp_path / "gibt-es-nicht")

    async def test(client, fake, server):
        entities, _ = await client.list_entities_services()
        cpu = next(e for e in entities if e.object_id == "cpu_temperature")
        states = {}
        client.subscribe_states(lambda s: states.__setitem__(s.key, s))
        await asyncio.sleep(0.2)
        assert states[cpu.key].missing_state is True

    scenario(bus_address, test, with_bluez=False)


# --- Scanner-Modus ----------------------------------------------------------------------

def collect_scanner_states(client):
    states = []
    client.subscribe_bluetooth_scanner_state(lambda s: states.append((s.state.name, s.mode.name, s.configured_mode.name)))
    return states


def test_scanner_state_is_sent_on_subscribe(bus_address):
    async def test(client, fake, server):
        states = collect_scanner_states(client)
        client.subscribe_bluetooth_le_raw_advertisements(lambda r: None)
        await asyncio.sleep(0.2)
        assert states == [("RUNNING", "ACTIVE", "ACTIVE")]

    scenario(bus_address, test)


def test_switch_to_passive_and_back(bus_address):
    async def test(client, fake, server):
        states = collect_scanner_states(client)
        client.bluetooth_scanner_set_mode(aioesphomeapi.BluetoothScannerMode.PASSIVE)
        await asyncio.sleep(0.3)
        assert states[-1] == ("RUNNING", "PASSIVE", "ACTIVE")
        assert any(c.startswith("RegisterMonitor /org/pi_ble_proxy/monitor") for c in fake.calls)
        assert not fake.discovering  # passiv: kein aktiver Scan mehr

        client.bluetooth_scanner_set_mode(aioesphomeapi.BluetoothScannerMode.ACTIVE)
        await asyncio.sleep(0.3)
        assert states[-1] == ("RUNNING", "ACTIVE", "ACTIVE")
        assert "UnregisterMonitor" in fake.calls and fake.discovering

    scenario(bus_address, test)


def test_failed_mode_switch_keeps_old_mode(bus_address):
    async def test(client, fake, server):
        states = collect_scanner_states(client)
        fake.fail_monitor = True
        client.bluetooth_scanner_set_mode(aioesphomeapi.BluetoothScannerMode.PASSIVE)
        await asyncio.sleep(0.3)
        assert states[-1][1] == "ACTIVE"  # HA sieht den tatsächlichen Modus
        assert fake.discovering and server.gatt.scan_mode == "active"

    scenario(bus_address, test)


def test_configured_passive_mode_starts_passive(bus_address):
    async def test(client, fake, server):
        assert any(c.startswith("RegisterMonitor") for c in fake.calls) and not fake.discovering
        assert server.scanner_state().configured_mode == 0  # PASSIVE

    scenario(bus_address, test, backend_args={"scan_mode": "passive"}, scan_mode="passive")


def test_monitor_object_serves_patterns_to_bluez(bus_address):
    async def test(client, fake, server):
        client.bluetooth_scanner_set_mode(aioesphomeapi.BluetoothScannerMode.PASSIVE)
        await asyncio.sleep(0.3)
        reply = await fake.bus.call(Message(destination=server.gatt._bus.unique_name, path="/org/pi_ble_proxy/monitor",
                                            interface="org.freedesktop.DBus.ObjectManager", member="GetManagedObjects"))
        (objects,) = reply.body
        monitor = objects["/org/pi_ble_proxy/monitor/monitor0"]["org.bluez.AdvertisementMonitor1"]
        assert monitor["Type"].value == "or_patterns"
        assert [bytes(p[2]) for p in monitor["Patterns"].value] == [b"\x06", b"\x1a", b"\x1b", b"\x1e", b"\x1f"]

    scenario(bus_address, test)


# --- Pairing-Agent ----------------------------------------------------------------------

def test_agent_registered_as_no_input_no_output(bus_address):
    async def test(client, fake, server):
        assert f"RegisterAgent {AGENT_PATH} NoInputNoOutput" in fake.calls
        assert "RequestDefaultAgent" in fake.calls

    scenario(bus_address, test)


def test_agent_accepts_only_the_device_being_paired(bus_address):
    async def test(client, fake, server):
        fake.agent_dest = server.gatt._bus.unique_name
        assert (await client.bluetooth_device_pair(LOCK)).paired is True
        assert fake.agent_answers == [True]

        # Fremdes Gerät fragt ohne unser Zutun: abgelehnt
        reply = await fake.bus.call(Message(destination=fake.agent_dest, path=AGENT_PATH,
                                            interface="org.bluez.Agent1", member="RequestConfirmation",
                                            signature="ou", body=["/org/bluez/hci0/dev_11_22_33_44_55_66", 1]))
        assert reply.message_type == MessageType.ERROR and reply.error_name == "org.bluez.Error.Rejected"
        assert server.gatt._agent.allowed is None

    scenario(bus_address, test)


def test_agent_rejects_pin_entry(bus_address):
    async def test(client, fake, server):
        for member, sig, body in (("RequestPinCode", "o", [DEV]), ("RequestPasskey", "o", [DEV])):
            reply = await fake.bus.call(Message(destination=server.gatt._bus.unique_name, path=AGENT_PATH,
                                                interface="org.bluez.Agent1", member=member, signature=sig, body=body))
            assert reply.message_type == MessageType.ERROR, member

    scenario(bus_address, test)


# --- Überwachung --------------------------------------------------------------------------

def test_check_restarts_stopped_discovery_and_powers_adapter(bus_address):
    async def test(client, fake, server):
        assert await server.gatt.check() is None
        fake.discovering = False
        assert await server.gatt.check() is None
        assert fake.discovering and fake.calls.count("StartDiscovery") == 2

        fake.adapter._powered = False
        assert await server.gatt.check() is None
        assert "SetPowered True" in fake.calls

    scenario(bus_address, test)


def test_bluetoothd_restart_is_detected_and_recovered(bus_address):
    async def test(client, fake, server):
        loop = asyncio.get_running_loop()
        state, done = [], loop.create_future()
        await client.bluetooth_device_connect(
            LOCK, lambda c, mtu, err: (state.append(c), done.set_result(None) if c and not done.done() else None),
            timeout=10, address_type=1, feature_flags=127)
        await done

        fake.stop()  # bluetoothd beendet
        await asyncio.sleep(0.5)
        assert server.gatt.available is False
        assert state[-1] is False  # HA erfährt, dass die Verbindung weg ist
        assert server.free_slots == 3

        fake2 = FakeBluez()  # bluetoothd kommt zurück
        await fake2.start(bus_address)
        try:
            for _ in range(40):
                if server.gatt.available:
                    break
                await asyncio.sleep(0.25)
            assert server.gatt.available
            assert fake2.discovering and any(c.startswith("RegisterAgent") for c in fake2.calls)
        finally:
            fake2.stop()
        fake.bus = fake2.bus  # scenario räumt hinterher auf

    scenario(bus_address, test)


class StubScanner(FakeScanner):
    def __init__(self, problems):
        self.problems = list(problems)

    async def check(self):
        return self.problems.pop(0) if self.problems else None


def test_persistent_problem_makes_the_service_exit(bus_address):
    async def test(client, fake, server):
        with pytest.raises(RuntimeError, match="dauerhaft gestört: Adapter weg"):
            await asyncio.wait_for(server.failed, 3)

    scenario(bus_address, test, with_bluez=False, scanner=StubScanner(["Adapter weg"] * 100),
             watchdog_interval=0.05, watchdog_failures=3)


def test_transient_problem_does_not_exit(bus_address):
    async def test(client, fake, server):
        await asyncio.sleep(0.8)
        assert not server.failed.done()
        assert server.scanner_problem is None

    scenario(bus_address, test, with_bluez=False, scanner=StubScanner(["kurz weg", "kurz weg", None]),
             watchdog_interval=0.05, watchdog_failures=3)


def test_silence_counts_as_problem(bus_address):
    async def test(client, fake, server):
        with pytest.raises(RuntimeError, match="keine Advertisements"):
            await asyncio.wait_for(server.failed, 3)

    scenario(bus_address, test, with_bluez=False, watchdog_interval=0.05, watchdog_failures=3, silence_timeout=0.1)


class BrokenScanner(FakeScanner):
    async def check(self):
        raise OSError("Socket tot")


def test_crashing_check_counts_as_problem(bus_address):
    async def test(client, fake, server):
        with pytest.raises(RuntimeError, match="Prüfung fehlgeschlagen"):
            await asyncio.wait_for(server.failed, 3)

    scenario(bus_address, test, with_bluez=False, scanner=BrokenScanner(), watchdog_interval=0.05, watchdog_failures=3)


def test_problem_is_reported_to_ha_as_scanner_state_failed(bus_address):
    async def test(client, fake, server):
        states = collect_scanner_states(client)
        await asyncio.sleep(0.4)
        assert states and states[-1][0] == "FAILED"

    scenario(bus_address, test, scanner=StubScanner(["kaputt"] * 100), watchdog_interval=0.05, watchdog_failures=1000)


def test_statistics_line_in_log(bus_address, caplog):
    async def test(client, fake, server):
        server.scanner.emit(Advertisement(1, 0, -50, b""))
        server.stats.connects += 2
        server._supervisor.log_stats()

    with caplog.at_level(logging.INFO, logger="pi_ble_proxy.supervisor"):
        scenario(bus_address, test, with_bluez=False)
    assert any("Statistik: 1 Advertisements empfangen" in r.message and "2 Verbindungen" in r.message
               for r in caplog.records)
