import asyncio
import logging
import socket
import struct

import pytest

from pi_ble_proxy import mgmt
from pi_ble_proxy.mgmt import MgmtError, MgmtScanner, parse_device_found

H = struct.Struct("<HHH")


def pkt(event, index, params=b""):
    return H.pack(event, index, len(params)) + params


def device_found(mac: str, addr_type: int, rssi: int, eir: bytes, flags: int = 0) -> bytes:
    bdaddr = bytes(int(x, 16) for x in reversed(mac.split(":")))
    return struct.pack("<6sBbIH", bdaddr, addr_type, rssi, flags, len(eir)) + eir


def read_info_reply(mac="DC:A6:32:01:02:03", settings=mgmt.SETTING_POWERED | mgmt.SETTING_LE):
    bdaddr = bytes(int(x, 16) for x in reversed(mac.split(":")))
    body = struct.pack("<6sBHII", bdaddr, 9, 15, 0xFFFF, settings) + b"\0" * (3 + 249 + 11)
    return pkt(mgmt.EV_CMD_COMPLETE, 0, struct.pack("<HB", mgmt.OP_READ_INFO, 0) + body)


def complete(opcode, status=0, index=0):
    return pkt(mgmt.EV_CMD_COMPLETE, index, struct.pack("<HB", opcode, status))


class FakeKernel:
    """Das andere Ende des Sockets: beantwortet Kommandos wie der Kernel."""

    def __init__(self, loop, sock, read_info=None, discovery_status=0):
        self.sock, self.commands = sock, []
        self.read_info = read_info or read_info_reply()
        self.discovery_status = discovery_status
        loop.add_reader(sock.fileno(), self._on_cmd)

    def _on_cmd(self):
        opcode, index, length = H.unpack(self.sock.recv(65535)[:6])
        self.commands.append(opcode)
        if opcode == mgmt.OP_READ_INFO:
            self.sock.send(self.read_info)
        elif opcode == mgmt.OP_START_DISCOVERY:
            self.sock.send(complete(opcode, self.discovery_status))
        else:
            self.sock.send(complete(opcode))


def run(coro):
    return asyncio.run(coro)


def test_parse_device_found_le_random():
    eir = b"\x02\x01\x06\x03\x03\x0f\x18"
    adv = parse_device_found(device_found("AA:BB:CC:DD:EE:FF", 2, -71, eir, flags=0x20))
    assert adv.address == 0xAABBCCDDEEFF
    assert (adv.address_type, adv.rssi, adv.data) == (1, -71, eir)


def test_parse_ignores_classic_and_garbage():
    assert parse_device_found(device_found("AA:BB:CC:DD:EE:FF", 0, -50, b"")) is None  # BR/EDR
    assert parse_device_found(b"\x00" * 5) is None


def test_scanner_reads_info_starts_discovery_and_delivers():
    async def main():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False), theirs.setblocking(False)
        kernel = FakeKernel(loop, theirs)
        got = []
        scanner = MgmtScanner(0, sock=ours)
        await scanner.start(got.append)
        assert scanner.adapter_address == "DC:A6:32:01:02:03"
        assert kernel.commands == [mgmt.OP_READ_INFO, mgmt.OP_START_DISCOVERY]

        theirs.send(pkt(mgmt.EV_DEVICE_FOUND, 0, device_found("11:22:33:44:55:66", 1, -60, b"\x02\x01\x06")))
        theirs.send(pkt(mgmt.EV_DEVICE_FOUND, 1, device_found("77:77:77:77:77:77", 1, -60, b"")))  # anderer Adapter
        await asyncio.sleep(0.05)
        assert [(a.address, a.rssi) for a in got] == [(0x112233445566, -60)]

        await scanner.stop()
        assert mgmt.OP_STOP_DISCOVERY in kernel.commands

    run(main())


def test_discovery_is_restarted_when_kernel_stops_it():
    async def main():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False), theirs.setblocking(False)
        kernel = FakeKernel(loop, theirs)
        scanner = MgmtScanner(0, sock=ours)
        await scanner.start(lambda a: None)
        theirs.send(pkt(mgmt.EV_DISCOVERING, 0, bytes([6, 0])))
        await asyncio.sleep(0.3)
        assert kernel.commands.count(mgmt.OP_START_DISCOVERY) == 2
        await scanner.stop()

    run(main())


def test_busy_discovery_is_accepted():
    async def main():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False), theirs.setblocking(False)
        FakeKernel(loop, theirs, discovery_status=mgmt.STATUS_BUSY)
        scanner = MgmtScanner(0, sock=ours)
        await scanner.start(lambda a: None)  # darf nicht werfen
        await scanner.stop()

    run(main())


@pytest.mark.parametrize("settings, msg", [(mgmt.SETTING_LE, "nicht eingeschaltet"), (mgmt.SETTING_POWERED, "Low Energy")])
def test_unusable_adapter_gives_clear_error(settings, msg):
    async def main():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False), theirs.setblocking(False)
        FakeKernel(loop, theirs, read_info=read_info_reply(settings=settings))
        with pytest.raises(MgmtError, match=msg):
            await MgmtScanner(0, sock=ours).start(lambda a: None)

    run(main())


def test_real_socket_binds_and_reports_missing_adapter():
    """Echter Kernel-Socket (ohne Hardware): Bind klappt, hci0 existiert nur ggf. nicht."""
    try:
        sock = mgmt.open_mgmt_socket()
    except (OSError, MgmtError) as exc:  # z. B. CI-Rechner: Python ohne Bluetooth oder ohne Rechte
        pytest.skip(f"kein mgmt-Socket in dieser Umgebung: {exc}")

    async def main():
        try:
            await MgmtScanner(0, sock=sock).start(lambda a: None)
        except MgmtError as exc:
            return str(exc)
        return "ok"

    result = run(main())
    assert result == "ok" or "nicht vorhanden" in result or "nicht eingeschaltet" in result


def test_listen_only_mode_never_touches_discovery():
    async def main():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False), theirs.setblocking(False)
        kernel = FakeKernel(loop, theirs)
        got = []
        scanner = MgmtScanner(0, sock=ours)
        await scanner.start(got.append, discover=False)
        theirs.send(pkt(mgmt.EV_DEVICE_FOUND, 0, device_found("11:22:33:44:55:66", 1, -60, b"")))
        theirs.send(pkt(mgmt.EV_DISCOVERING, 0, bytes([6, 0])))  # BlueZ beendet/startet selbst neu
        await asyncio.sleep(0.3)
        assert len(got) == 1
        assert kernel.commands == [mgmt.OP_READ_INFO]  # kein START/STOP_DISCOVERY
        await scanner.stop()
        assert kernel.commands == [mgmt.OP_READ_INFO]

    run(main())


def test_passive_monitor_events_are_delivered():
    async def main():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False), theirs.setblocking(False)
        FakeKernel(loop, theirs)
        got = []
        scanner = MgmtScanner(0, sock=ours)
        await scanner.start(got.append, discover=False)
        eir = b"\x02\x01\x06\x03\x03\x0f\x18"
        monitor_handle = struct.pack("<H", 1)
        theirs.send(pkt(mgmt.EV_ADV_MONITOR_DEVICE_FOUND, 0, monitor_handle + device_found("AA:BB:CC:DD:EE:FF", 2, -70, eir)))
        await asyncio.sleep(0.1)
        assert [(a.address, a.address_type, a.rssi, a.data) for a in got] == [(0xAABBCCDDEEFF, 1, -70, eir)]
        await scanner.stop()

    run(main())


def test_check_reports_adapter_problems():
    async def main():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False), theirs.setblocking(False)
        kernel = FakeKernel(loop, theirs)
        scanner = MgmtScanner(0, sock=ours)
        await scanner.start(lambda a: None, discover=False)
        assert await scanner.check() is None

        kernel.read_info = read_info_reply(settings=mgmt.SETTING_LE)  # ausgeschaltet
        assert "nicht eingeschaltet" in await scanner.check()

        theirs.send(pkt(mgmt.EV_INDEX_REMOVED, 0))
        await asyncio.sleep(0.05)
        assert scanner.present is False
        theirs.send(pkt(mgmt.EV_INDEX_ADDED, 0))
        await asyncio.sleep(0.05)
        assert scanner.present is True

        kernel.sock.close()  # Kernel-Seite weg: check() darf nicht hängen
        assert await scanner.check() is not None
        await scanner.stop()

    run(main())


def test_python_without_bluetooth_support_gives_a_clear_error(monkeypatch):
    """Z. B. ein selbst gebautes Python oder der GitHub-Rechner: keine kryptische AttributeError-Meldung."""
    monkeypatch.delattr(socket, "AF_BLUETOOTH", raising=False)
    with pytest.raises(MgmtError, match="ohne Bluetooth-Unterstützung"):
        mgmt.open_mgmt_socket()


def test_closed_kernel_socket_does_not_spin_the_event_loop(caplog):
    """Regression: Eine leere Antwort (Gegenstelle zu) ließ _on_readable endlos laufen und blockierte alles."""
    async def main():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False), theirs.setblocking(False)
        kernel = FakeKernel(loop, theirs)
        scanner = MgmtScanner(0, sock=ours)
        await scanner.start(lambda a: None, discover=False)

        loop.remove_reader(theirs.fileno())
        theirs.close()  # Kernel-Seite weg
        start = loop.time()
        await asyncio.sleep(0.2)  # früher: Endlosschleife, man kam hier nie an
        assert loop.time() - start < 1

        problem = await scanner.check()
        assert problem and "geschlossen" in problem
        assert scanner.dead
        await scanner.stop()
        return kernel

    with caplog.at_level(logging.ERROR, logger="pi_ble_proxy.mgmt"):
        run(main())
    assert sum("unbrauchbar" in r.message for r in caplog.records) == 1  # einmal melden, nicht fluten


def test_pending_command_fails_immediately_when_socket_dies():
    async def main():
        loop = asyncio.get_running_loop()
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False), theirs.setblocking(False)
        scanner = MgmtScanner(0, sock=ours)
        scanner._loop = loop
        loop.add_reader(ours.fileno(), scanner._on_readable)
        command = asyncio.ensure_future(scanner._command(mgmt.OP_READ_INFO))
        await asyncio.sleep(0.05)  # Kommando ist raus, keine Antwort
        theirs.close()
        start = loop.time()
        with pytest.raises(MgmtError, match="geschlossen"):
            await command
        assert loop.time() - start < 1  # nicht erst nach dem 5-s-Timeout

    run(main())
