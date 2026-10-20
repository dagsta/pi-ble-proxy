"""Minimaler Ersatz für ``org.bluez`` auf einem privaten D-Bus (nur für Tests)."""

import asyncio

from dbus_fast import DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

LOCK_MAC = "AA_BB_CC_DD_EE_FF"
DEV = f"/org/bluez/hci0/dev_{LOCK_MAC}"
SVC = f"{DEV}/service000a"
CHR_WRITE = f"{SVC}/char000b"
CHR_NOTIFY = f"{SVC}/char000d"
DESC_CCC = f"{CHR_NOTIFY}/desc000f"


AGENT_PATH = "/org/pi_ble_proxy/agent"


class AgentManager(ServiceInterface):
    def __init__(self, fake):
        super().__init__("org.bluez.AgentManager1")
        self.fake = fake

    @method()
    def RegisterAgent(self, agent: "o", capability: "s"):
        self.fake.calls.append(f"RegisterAgent {agent} {capability}")

    @method()
    def RequestDefaultAgent(self, agent: "o"):
        self.fake.calls.append("RequestDefaultAgent")

    @method()
    def UnregisterAgent(self, agent: "o"):
        self.fake.calls.append("UnregisterAgent")


class MonitorManager(ServiceInterface):
    def __init__(self, fake):
        super().__init__("org.bluez.AdvertisementMonitorManager1")
        self.fake = fake

    @method()
    def RegisterMonitor(self, application: "o"):
        self.fake.calls.append(f"RegisterMonitor {application}")
        if self.fake.fail_monitor:
            raise DBusError("org.bluez.Error.NotSupported", "Advertisement Monitor nicht unterstützt")

    @method()
    def UnregisterMonitor(self, application: "o"):
        self.fake.calls.append("UnregisterMonitor")


class Adapter(ServiceInterface):
    def __init__(self, fake):
        super().__init__("org.bluez.Adapter1")
        self.fake = fake
        self._powered = True

    @dbus_property()
    def Powered(self) -> "b":
        return self._powered

    @Powered.setter
    def Powered(self, value: "b"):
        self.fake.calls.append(f"SetPowered {value}")
        self._powered = value

    @dbus_property(access=PropertyAccess.READ)
    def Discovering(self) -> "b":
        return self.fake.discovering

    @method()
    def SetDiscoveryFilter(self, properties: "a{sv}"):
        self.fake.filter = {k: v.value for k, v in properties.items()}

    @method()
    def StartDiscovery(self):
        self.fake.calls.append("StartDiscovery")
        if self.fake.discovering:  # wie BlueZ: zweiter Aufruf desselben Clients
            raise DBusError("org.bluez.Error.InProgress", "Operation already in progress")
        self.fake.discovering = True

    @method()
    def StopDiscovery(self):
        self.fake.calls.append("StopDiscovery")
        self.fake.discovering = False

    @method()
    def RemoveDevice(self, device: "o"):
        self.fake.calls.append(f"RemoveDevice {device}")


class Device(ServiceInterface):
    def __init__(self, fake):
        super().__init__("org.bluez.Device1")
        self.fake, self._connected, self._resolved, self._paired = fake, False, False, False
        self._trusted = False

    @dbus_property(access=PropertyAccess.READ)
    def Connected(self) -> "b":
        return self._connected

    @dbus_property(access=PropertyAccess.READ)
    def ServicesResolved(self) -> "b":
        return self._resolved

    @dbus_property(access=PropertyAccess.READ)
    def Paired(self) -> "b":
        return self._paired

    @dbus_property()
    def Trusted(self) -> "b":
        return self._trusted

    @Trusted.setter
    def Trusted(self, value: "b"):
        self._trusted = value

    @method()
    async def Connect(self):
        self.fake.calls.append("Connect")
        if self.fake.fail_connect or self.fake.abort_first > 0:
            self.fake.abort_first -= 1
            raise DBusError("org.bluez.Error.Failed", "le-connection-abort-by-local")
        await asyncio.sleep(self.fake.connect_delay)
        self._connected = True
        self.emit_properties_changed({"Connected": True})
        await asyncio.sleep(0.02)  # Service-Erkennung dauert etwas
        self._resolved = True
        self.emit_properties_changed({"ServicesResolved": True})

    @method()
    def Disconnect(self):
        self.fake.calls.append("Disconnect")
        self.drop()

    @method()
    async def Pair(self):
        self.fake.calls.append("Pair")
        if self.fake.agent_dest:  # BlueZ fragt den Agenten, wie bei echtem Just-Works-Pairing
            reply = await self.fake.bus.call(Message(
                destination=self.fake.agent_dest, path=AGENT_PATH, interface="org.bluez.Agent1",
                member="RequestConfirmation", signature="ou", body=[DEV, 123456]))
            self.fake.agent_answers.append(reply.message_type != MessageType.ERROR)
            if reply.message_type == MessageType.ERROR:
                raise DBusError("org.bluez.Error.AuthenticationFailed", "Agent hat abgelehnt")
        self._paired = True

    def drop(self):
        """Verbindung geht verloren (auch vom Test aufrufbar)."""
        if self._connected:
            self._connected = self._resolved = False
            self.emit_properties_changed({"Connected": False, "ServicesResolved": False})

    def props(self):
        return {"Connected": Variant("b", self._connected),
                "ServicesResolved": Variant("b", self._resolved)}


class Service(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.GattService1")

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return "cba20d00-224d-11e6-9fb8-0002a5d5c51b"

    def props(self):
        return {"UUID": Variant("s", self.UUID)}


class Characteristic(ServiceInterface):
    def __init__(self, fake, uuid, flags):
        super().__init__("org.bluez.GattCharacteristic1")
        self.fake, self._uuid, self._flags = fake, uuid, flags
        self.value, self.written, self.notifying = b"\x01\x02", [], False

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return SVC

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def MTU(self) -> "q":
        return 247

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self.value

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return self.value

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        self.written.append((bytes(value), options["type"].value))
        if self.fake.fail_write:
            raise DBusError("org.bluez.Error.NotPermitted", "Write not permitted")

    @method()
    def StartNotify(self):
        self.notifying = True

    @method()
    def StopNotify(self):
        self.notifying = False

    def notify(self, data: bytes):
        self.value = data
        self.emit_properties_changed({"Value": data})

    def props(self):
        return {"UUID": Variant("s", self._uuid), "Service": Variant("o", SVC),
                "Flags": Variant("as", self._flags), "MTU": Variant("q", 247)}


class Descriptor(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.GattDescriptor1")
        self.written = []

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return "00002902-0000-1000-8000-00805f9b34fb"

    @dbus_property(access=PropertyAccess.READ)
    def Characteristic(self) -> "o":
        return CHR_NOTIFY

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return b"\x00\x00"

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        # wie BlueZ: der CCCD darf nicht direkt beschrieben werden
        raise DBusError("org.bluez.Error.NotPermitted", "Write not permitted")

    def props(self):
        return {"UUID": Variant("s", self.UUID), "Characteristic": Variant("o", CHR_NOTIFY)}


class FakeBluez:
    def __init__(self, device_known=True):
        self.calls, self.fail_connect, self.fail_write = [], False, False
        self.discovering, self.filter, self.abort_first = False, {}, 0
        self.fail_monitor, self.agent_dest, self.agent_answers = False, None, []
        self.connect_delay, self.device_known = 0.0, device_known
        self.device, self.service, self.desc = Device(self), Service(), Descriptor()
        self.write_char = Characteristic(self, "cba20002-224d-11e6-9fb8-0002a5d5c51b", ["write", "write-without-response"])
        self.notify_char = Characteristic(self, "cba20003-224d-11e6-9fb8-0002a5d5c51b", ["read", "notify"])

    async def start(self, bus_address: str):
        self.bus = await MessageBus(bus_address=bus_address).connect()
        self.adapter = Adapter(self)
        self.bus.export("/org/bluez/hci0", self.adapter)
        self.bus.export("/org/bluez/hci0", MonitorManager(self))
        self.bus.export("/org/bluez", AgentManager(self))
        if self.device_known:
            self.appear()
        await self.bus.request_name("org.bluez")

    def appear(self):
        """Gerät wird von BlueZ erst nach dem Scan bekannt (Test für Discovery-Fallback)."""
        self.device_known = True
        for path, iface in [(DEV, self.device), (SVC, self.service), (CHR_WRITE, self.write_char),
                            (CHR_NOTIFY, self.notify_char), (DESC_CCC, self.desc)]:
            self.bus.export(path, iface)

    def stop(self):
        self.bus.disconnect()
