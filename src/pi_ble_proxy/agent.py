"""D-Bus-Objekte, die BlueZ aufruft: Pairing-Agent und Advertisement-Monitor (passiver Scan)."""

from __future__ import annotations

import logging

from dbus_fast import DBusError
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

log = logging.getLogger(__name__)

AGENT_PATH = "/org/pi_ble_proxy/agent"
AGENT_CAPABILITY = "NoInputNoOutput"  # "Just Works", wie ein ESP32-Proxy

MONITOR_APP = "/org/pi_ble_proxy/monitor"
MONITOR_PATH = f"{MONITOR_APP}/monitor0"
# Passiver Scan filtert im Controller: nur Advertisements mit diesen Flags-Werten (AD-Typ 0x01),
# dieselbe Auswahl wie bei Home Assistants lokalem Bluetooth-Adapter.
AD_TYPE_FLAGS = 0x01
PASSIVE_PATTERNS = [(0, AD_TYPE_FLAGS, bytes([flags])) for flags in (0x06, 0x1A, 0x1B, 0x1E, 0x1F)]


def _rejected(reason: str) -> DBusError:
    return DBusError("org.bluez.Error.Rejected", reason)


class PairingAgent(ServiceInterface):
    """Beantwortet Pairing-Anfragen nur für das Gerät, das wir gerade selbst koppeln.

    Ohne diese Einschränkung könnte jedes Gerät in Funkreichweite sich mit dem Pi koppeln.
    PIN- und Passkey-Eingabe sind nicht möglich (Capability NoInputNoOutput).
    """

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")
        self.allowed: str | None = None  # D-Bus-Pfad des Geräts, das gerade gekoppelt wird

    def _check(self, device: str) -> None:
        if device != self.allowed:
            log.warning("Pairing-Anfrage von %s abgelehnt (nicht von uns angefordert)", device)
            raise _rejected("nicht angefordert")

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        raise _rejected("PIN-Eingabe wird nicht unterstützt")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        pass

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        raise _rejected("Passkey-Eingabe wird nicht unterstützt")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        self._check(device)

    @method()
    def RequestAuthorization(self, device: "o"):
        self._check(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        self._check(device)

    @method()
    def Cancel(self):
        pass


class AdvertisementMonitor(ServiceInterface):
    """Meldet BlueZ die Muster für den passiven Scan (org.bluez.AdvertisementMonitor1)."""

    def __init__(self) -> None:
        super().__init__("org.bluez.AdvertisementMonitor1")
        self.active = False

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "or_patterns"

    @dbus_property(access=PropertyAccess.READ)
    def Patterns(self) -> "a(yyay)":
        return [list(p) for p in PASSIVE_PATTERNS]

    @method()
    def Release(self):
        self.active = False

    @method()
    def Activate(self):
        self.active = True

    @method()
    def DeviceFound(self, device: "o"):
        pass

    @method()
    def DeviceLost(self, device: "o"):
        pass
