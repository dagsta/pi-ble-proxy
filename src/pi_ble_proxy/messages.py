"""ESPHome-Native-API-Nachrichten (Feldnummern und IDs aus ``api.proto``).

Nur der Teil, den ein Bluetooth-Proxy braucht. ``REGISTRY`` bildet die
Nachrichten-ID auf die Klasse ab.
"""

from __future__ import annotations

from dataclasses import dataclass

from .protobuf import Message, field

REGISTRY: dict[int, type[Message]] = {}


def api_message(msg_id: int):
    def deco(cls):
        cls = dataclass(cls)
        cls.ID = msg_id
        REGISTRY[msg_id] = cls
        return cls

    return deco


# --- Verbindungsaufbau -------------------------------------------------------


@api_message(1)
class HelloRequest(Message):
    client_info: str = field(1, "string")
    api_version_major: int = field(2, "uint32")
    api_version_minor: int = field(3, "uint32")


@api_message(2)
class HelloResponse(Message):
    api_version_major: int = field(1, "uint32")
    api_version_minor: int = field(2, "uint32")
    server_info: str = field(3, "string")
    name: str = field(4, "string")


@api_message(3)
class AuthenticationRequest(Message):
    password: str = field(1, "string")


@api_message(4)
class AuthenticationResponse(Message):
    invalid_password: bool = field(1, "bool")


@api_message(5)
class DisconnectRequest(Message):
    pass


@api_message(6)
class DisconnectResponse(Message):
    pass


@api_message(7)
class PingRequest(Message):
    pass


@api_message(8)
class PingResponse(Message):
    pass


@api_message(9)
class DeviceInfoRequest(Message):
    pass


@api_message(10)
class DeviceInfoResponse(Message):
    uses_password: bool = field(1, "bool")
    name: str = field(2, "string")
    mac_address: str = field(3, "string")
    esphome_version: str = field(4, "string")
    compilation_time: str = field(5, "string")
    model: str = field(6, "string")
    bluetooth_proxy_feature_flags: int = field(15, "uint32")
    manufacturer: str = field(12, "string")
    friendly_name: str = field(13, "string")
    bluetooth_mac_address: str = field(18, "string")


@api_message(11)
class ListEntitiesRequest(Message):
    pass


@api_message(19)
class ListEntitiesDoneResponse(Message):
    pass


@api_message(20)
class SubscribeStatesRequest(Message):
    pass


@api_message(34)
class SubscribeHomeassistantServicesRequest(Message):
    pass


@api_message(38)
class SubscribeHomeAssistantStatesRequest(Message):
    pass


# --- Bluetooth: Advertisements ----------------------------------------------


@api_message(66)
class SubscribeBluetoothLEAdvertisementsRequest(Message):
    flags: int = field(1, "uint32")  # Bit 0: Rohdaten


@dataclass
class BluetoothLERawAdvertisement(Message):
    address: int = field(1, "uint64")
    rssi: int = field(2, "sint32")
    address_type: int = field(3, "uint32")
    data: bytes = field(4, "bytes")


@api_message(93)
class BluetoothLERawAdvertisementsResponse(Message):
    advertisements: list = field(1, BluetoothLERawAdvertisement, repeated=True)


@api_message(87)
class UnsubscribeBluetoothLEAdvertisementsRequest(Message):
    pass


# --- Bluetooth: aktive Verbindungen (GATT) -----------------------------------

REQ_CONNECT, REQ_DISCONNECT, REQ_PAIR, REQ_UNPAIR = 0, 1, 2, 3
REQ_CONNECT_WITH_CACHE, REQ_CONNECT_WITHOUT_CACHE, REQ_CLEAR_CACHE = 4, 5, 6


@api_message(68)
class BluetoothDeviceRequest(Message):
    address: int = field(1, "uint64")
    request_type: int = field(2, "enum")
    has_address_type: bool = field(3, "bool")
    address_type: int = field(4, "uint32")


@api_message(69)
class BluetoothDeviceConnectionResponse(Message):
    address: int = field(1, "uint64")
    connected: bool = field(2, "bool")
    mtu: int = field(3, "uint32")
    error: int = field(4, "int32")


@api_message(70)
class BluetoothGATTGetServicesRequest(Message):
    address: int = field(1, "uint64")


@dataclass
class BluetoothGATTDescriptor(Message):
    uuid: list = field(1, "uint64", repeated=True)  # [high64, low64]
    handle: int = field(2, "uint32")


@dataclass
class BluetoothGATTCharacteristic(Message):
    uuid: list = field(1, "uint64", repeated=True)
    handle: int = field(2, "uint32")
    properties: int = field(3, "uint32")
    descriptors: list = field(4, BluetoothGATTDescriptor, repeated=True)


@dataclass
class BluetoothGATTService(Message):
    uuid: list = field(1, "uint64", repeated=True)
    handle: int = field(2, "uint32")
    characteristics: list = field(3, BluetoothGATTCharacteristic, repeated=True)


@api_message(71)
class BluetoothGATTGetServicesResponse(Message):
    address: int = field(1, "uint64")
    services: list = field(2, BluetoothGATTService, repeated=True)


@api_message(72)
class BluetoothGATTGetServicesDoneResponse(Message):
    address: int = field(1, "uint64")


@api_message(73)
class BluetoothGATTReadRequest(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")


@api_message(74)
class BluetoothGATTReadResponse(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")
    data: bytes = field(3, "bytes")


@api_message(75)
class BluetoothGATTWriteRequest(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")
    response: bool = field(3, "bool")
    data: bytes = field(4, "bytes")


@api_message(76)
class BluetoothGATTReadDescriptorRequest(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")


@api_message(77)
class BluetoothGATTWriteDescriptorRequest(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")
    data: bytes = field(3, "bytes")


@api_message(78)
class BluetoothGATTNotifyRequest(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")
    enable: bool = field(3, "bool")


@api_message(79)
class BluetoothGATTNotifyDataResponse(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")
    data: bytes = field(3, "bytes")


@api_message(80)
class SubscribeBluetoothConnectionsFreeRequest(Message):
    pass


@api_message(81)
class BluetoothConnectionsFreeResponse(Message):
    free: int = field(1, "uint32")
    limit: int = field(2, "uint32")
    allocated: list = field(3, "uint64", repeated=True)


@api_message(82)
class BluetoothGATTErrorResponse(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")
    error: int = field(3, "int32")


@api_message(83)
class BluetoothGATTWriteResponse(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")


@api_message(84)
class BluetoothGATTNotifyResponse(Message):
    address: int = field(1, "uint64")
    handle: int = field(2, "uint32")


@api_message(85)
class BluetoothDevicePairingResponse(Message):
    address: int = field(1, "uint64")
    paired: bool = field(2, "bool")
    error: int = field(3, "int32")


@api_message(86)
class BluetoothDeviceUnpairingResponse(Message):
    address: int = field(1, "uint64")
    success: bool = field(2, "bool")
    error: int = field(3, "int32")


@api_message(88)
class BluetoothDeviceClearCacheResponse(Message):
    address: int = field(1, "uint64")
    success: bool = field(2, "bool")
    error: int = field(3, "int32")


# --- Scanner-Status und -Modus -----------------------------------------------

SCANNER_IDLE, SCANNER_STARTING, SCANNER_RUNNING, SCANNER_FAILED, SCANNER_STOPPING, SCANNER_STOPPED = range(6)
MODE_PASSIVE, MODE_ACTIVE = 0, 1


@api_message(126)
class BluetoothScannerStateResponse(Message):
    state: int = field(1, "enum")
    mode: int = field(2, "enum")
    configured_mode: int = field(3, "enum")


@api_message(127)
class BluetoothScannerSetModeRequest(Message):
    mode: int = field(1, "enum")


# --- Sensor-Entitäten (Diagnosewerte) ---------------------------------------

STATE_CLASS_MEASUREMENT, STATE_CLASS_TOTAL_INCREASING = 1, 2
ENTITY_CATEGORY_DIAGNOSTIC = 2


@api_message(16)
class ListEntitiesSensorResponse(Message):
    object_id: str = field(1, "string")
    key: int = field(2, "fixed32")
    name: str = field(3, "string")
    icon: str = field(5, "string")
    unit_of_measurement: str = field(6, "string")
    accuracy_decimals: int = field(7, "int32")
    device_class: str = field(9, "string")
    state_class: int = field(10, "enum")
    entity_category: int = field(13, "enum")


@api_message(25)
class SensorStateResponse(Message):
    key: int = field(1, "fixed32")
    state: float = field(2, "float")
    missing_state: bool = field(3, "bool")
