"""Gegenprobe: unser Codec muss byte-kompatibel zur offiziellen protobuf-Implementierung sein."""

import pytest

from pi_ble_proxy import messages as m
from pi_ble_proxy.protobuf import DecodeError, decode_varint, encode_varint

pb = pytest.importorskip("aioesphomeapi.api_pb2")


@pytest.mark.parametrize("n", [0, 1, 127, 128, 300, 2**32, 2**48 - 1, 2**64 - 1])
def test_varint_roundtrip(n):
    assert decode_varint(encode_varint(n), 0) == (n, len(encode_varint(n)))


def test_varint_truncated():
    with pytest.raises(DecodeError):
        decode_varint(b"\x80", 0)


def test_hello_matches_reference():
    ours = m.HelloRequest(client_info="Home Assistant", api_version_major=1, api_version_minor=13)
    ref = pb.HelloRequest(client_info="Home Assistant", api_version_major=1, api_version_minor=13)
    assert ours.encode() == ref.SerializeToString()
    assert m.HelloRequest.decode(ref.SerializeToString()) == ours


def test_raw_advertisements_match_reference():
    advs = [
        (0xAABBCCDDEEFF, -71, 1, b"\x02\x01\x06\x03\x03\x0f\x18"),
        (0x112233445566, 5, 0, b""),
    ]
    ours = m.BluetoothLERawAdvertisementsResponse(
        advertisements=[
            m.BluetoothLERawAdvertisement(address=a, rssi=r, address_type=t, data=d)
            for a, r, t, d in advs
        ]
    )
    ref = pb.BluetoothLERawAdvertisementsResponse()
    for a, r, t, d in advs:
        ref.advertisements.add(address=a, rssi=r, address_type=t, data=d)
    assert ours.encode() == ref.SerializeToString()
    back = m.BluetoothLERawAdvertisementsResponse.decode(ref.SerializeToString())
    assert back == ours


def test_device_info_matches_reference():
    kw = dict(
        name="pi-ble-proxy",
        mac_address="AA:BB:CC:DD:EE:FF",
        esphome_version="2025.10.0",
        bluetooth_proxy_feature_flags=35,
        bluetooth_mac_address="11:22:33:44:55:66",
        friendly_name="Pi",
    )
    assert m.DeviceInfoResponse(**kw).encode() == pb.DeviceInfoResponse(**kw).SerializeToString()
    assert m.DeviceInfoResponse.decode(pb.DeviceInfoResponse(**kw).SerializeToString()).name == kw["name"]


def test_unknown_fields_are_skipped():
    data = pb.HelloRequest(client_info="x").SerializeToString() + b"\x78\x05\x82\x07\x02ab"
    assert m.HelloRequest.decode(data).client_info == "x"


def test_ids_match_reference():
    for cls in m.REGISTRY.values():
        ref = getattr(pb, cls.__name__)
        ref_id = next(v for f, v in ref.DESCRIPTOR.GetOptions().ListFields() if f.name == "id")
        assert ref_id == cls.ID, cls.__name__


def test_sensor_messages_match_reference():
    ours = m.ListEntitiesSensorResponse(object_id="cpu_temperature", key=0xDEADBEEF, name="CPU Temperature",
                                        unit_of_measurement="°C", accuracy_decimals=1, device_class="temperature",
                                        state_class=1, entity_category=2)
    ref = pb.ListEntitiesSensorResponse(object_id="cpu_temperature", key=0xDEADBEEF, name="CPU Temperature",
                                        unit_of_measurement="°C", accuracy_decimals=1, device_class="temperature",
                                        state_class=1, entity_category=2)
    assert ours.encode() == ref.SerializeToString()
    state = m.SensorStateResponse(key=0xDEADBEEF, state=47.5)
    assert state.encode() == pb.SensorStateResponse(key=0xDEADBEEF, state=47.5).SerializeToString()
    assert m.SensorStateResponse.decode(state.encode()) == state
    missing = m.SensorStateResponse(key=1, missing_state=True)
    assert missing.encode() == pb.SensorStateResponse(key=1, missing_state=True).SerializeToString()


def test_scanner_messages_match_reference():
    ours = m.BluetoothScannerStateResponse(state=2, mode=1, configured_mode=0)
    assert ours.encode() == pb.BluetoothScannerStateResponse(state=2, mode=1, configured_mode=0).SerializeToString()
    assert m.BluetoothScannerSetModeRequest.decode(pb.BluetoothScannerSetModeRequest(mode=1).SerializeToString()).mode == 1
