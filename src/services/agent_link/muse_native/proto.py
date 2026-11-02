"""Wire-compatible protobuf messages defined from the protocol memo."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.message import DecodeError, Message

from ..codes import AgentLinkFailure, LinkErrorCode

_FILE = descriptor_pb2.FileDescriptorProto()
_FILE.name = "aoi_muse_native_protocol.proto"
_FILE.package = "aoi.muse.native"
_FILE.syntax = "proto3"

_SERVICE = _FILE.enum_type.add()
_SERVICE.name = "Service"
for _name, _number in (
    ("DAEMON", 0),
    ("SENTINEL", 1),
    ("VAULT", 2),
    ("AUTHD", 3),
):
    _value = _SERVICE.value.add()
    _value.name = _name
    _value.number = _number

_RESET = _FILE.enum_type.add()
_RESET.name = "ResetCode"
for _name, _number in (
    ("RESET_UNSPECIFIED", 0),
    ("RESET_CANCELLED", 1),
    ("RESET_PROTOCOL_ERROR", 2),
):
    _value = _RESET.value.add()
    _value.name = _name
    _value.number = _number

_TYPE = descriptor_pb2.FieldDescriptorProto
_LABEL_OPTIONAL = _TYPE.LABEL_OPTIONAL
_LABEL_REPEATED = _TYPE.LABEL_REPEATED


def _message(name: str, fields: tuple[tuple[str, int, int, int, str | None], ...]):
    descriptor = _FILE.message_type.add()
    descriptor.name = name
    for field_name, number, field_type, label, type_name in fields:
        field = descriptor.field.add()
        field.name = field_name
        field.number = number
        field.type = field_type
        field.label = label
        if type_name:
            field.type_name = type_name
    return descriptor


_message(
    "NoiseTransportFrame",
    (
        ("chunk_id", 1, _TYPE.TYPE_INT64, _LABEL_OPTIONAL, None),
        ("chunk_index", 2, _TYPE.TYPE_UINT32, _LABEL_OPTIONAL, None),
        ("total_chunks", 3, _TYPE.TYPE_UINT32, _LABEL_OPTIONAL, None),
        ("payload", 4, _TYPE.TYPE_BYTES, _LABEL_OPTIONAL, None),
    ),
)
_message(
    "ServiceRequest",
    (
        ("service", 1, _TYPE.TYPE_ENUM, _LABEL_OPTIONAL, ".aoi.muse.native.Service"),
        ("payload", 2, _TYPE.TYPE_BYTES, _LABEL_OPTIONAL, None),
    ),
)
_message(
    "ServiceResponse",
    (("payload", 1, _TYPE.TYPE_BYTES, _LABEL_OPTIONAL, None),),
)
_message(
    "Header",
    (
        ("key", 1, _TYPE.TYPE_STRING, _LABEL_OPTIONAL, None),
        ("value", 2, _TYPE.TYPE_STRING, _LABEL_OPTIONAL, None),
    ),
)
_message(
    "ApplicationRequest",
    (
        ("verb", 1, _TYPE.TYPE_STRING, _LABEL_OPTIONAL, None),
        ("path", 2, _TYPE.TYPE_STRING, _LABEL_OPTIONAL, None),
        ("headers", 3, _TYPE.TYPE_MESSAGE, _LABEL_REPEATED, ".aoi.muse.native.Header"),
        ("body", 4, _TYPE.TYPE_BYTES, _LABEL_OPTIONAL, None),
        ("end_body", 5, _TYPE.TYPE_BOOL, _LABEL_OPTIONAL, None),
    ),
)
_message(
    "ApplicationResponse",
    (
        ("status", 1, _TYPE.TYPE_INT32, _LABEL_OPTIONAL, None),
        ("headers", 2, _TYPE.TYPE_MESSAGE, _LABEL_REPEATED, ".aoi.muse.native.Header"),
        ("body", 3, _TYPE.TYPE_BYTES, _LABEL_OPTIONAL, None),
        ("end_body", 4, _TYPE.TYPE_BOOL, _LABEL_OPTIONAL, None),
    ),
)
_message(
    "BodyChunk",
    (
        ("data", 1, _TYPE.TYPE_BYTES, _LABEL_OPTIONAL, None),
        ("end_body", 2, _TYPE.TYPE_BOOL, _LABEL_OPTIONAL, None),
    ),
)
_message(
    "Reset",
    (
        ("code", 1, _TYPE.TYPE_ENUM, _LABEL_OPTIONAL, ".aoi.muse.native.ResetCode"),
        ("reason", 2, _TYPE.TYPE_STRING, _LABEL_OPTIONAL, None),
    ),
)
_FRAME = _FILE.message_type.add()
_FRAME.name = "ServiceFrame"
_FRAME.oneof_decl.add().name = "kind"
for _name, _number, _type_name in (
    ("request", 2, ".aoi.muse.native.ApplicationRequest"),
    ("response", 3, ".aoi.muse.native.ApplicationResponse"),
    ("body_chunk", 4, ".aoi.muse.native.BodyChunk"),
    ("reset", 5, ".aoi.muse.native.Reset"),
):
    _field = _FRAME.field.add()
    _field.name = _name
    _field.number = _number
    _field.type = _TYPE.TYPE_MESSAGE
    _field.label = _LABEL_OPTIONAL
    _field.type_name = _type_name
    _field.oneof_index = 0
_field = _FRAME.field.add()
_field.name = "stream_id"
_field.number = 1
_field.type = _TYPE.TYPE_INT64
_field.label = _LABEL_OPTIONAL

_POOL = descriptor_pool.DescriptorPool()
_POOL.AddSerializedFile(_FILE.SerializeToString())


def _class(name: str):
    return message_factory.GetMessageClass(
        _POOL.FindMessageTypeByName(f"aoi.muse.native.{name}")
    )


def _redacted_renderer(name: str):
    def render(_message: Message) -> str:
        return f"{name}(<wire content redacted>)"

    return render


NoiseTransportFrame = _class("NoiseTransportFrame")
ServiceRequest = _class("ServiceRequest")
ServiceResponse = _class("ServiceResponse")
Header = _class("Header")
ApplicationRequest = _class("ApplicationRequest")
ApplicationResponse = _class("ApplicationResponse")
BodyChunk = _class("BodyChunk")
Reset = _class("Reset")
ServiceFrame = _class("ServiceFrame")

for _name, _message_class in (
    ("NoiseTransportFrame", NoiseTransportFrame),
    ("ServiceRequest", ServiceRequest),
    ("ServiceResponse", ServiceResponse),
    ("Header", Header),
    ("ApplicationRequest", ApplicationRequest),
    ("ApplicationResponse", ApplicationResponse),
    ("BodyChunk", BodyChunk),
    ("Reset", Reset),
    ("ServiceFrame", ServiceFrame),
):
    _renderer = _redacted_renderer(_name)
    _message_class.__repr__ = _renderer
    _message_class.__str__ = _renderer

DAEMON = 0
SENTINEL = 1
VAULT = 2
AUTHD = 3
RESET_UNSPECIFIED = 0
RESET_CANCELLED = 1
RESET_PROTOCOL_ERROR = 2


def parse(message_type: type[Message], payload: bytes) -> Message:
    try:
        message = message_type()
        message.ParseFromString(payload)
        return message
    except (DecodeError, TypeError, ValueError):
        raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT) from None


def serialize(message: Message) -> bytes:
    try:
        return message.SerializeToString()
    except (TypeError, ValueError):
        raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT) from None


@dataclass
class FrameAssembler:
    """Reassemble bounded transport-frame chunks after decryption."""

    max_chunks: int = 256
    max_bytes: int = 4 * 1024 * 1024
    _parts: dict[int, tuple[int, dict[int, bytes]]] = field(
        default_factory=dict, repr=False
    )

    def add(self, frame: Any) -> bytes | None:
        chunk_id = int(frame.chunk_id)
        index = int(frame.chunk_index)
        total = int(frame.total_chunks)
        payload = bytes(frame.payload)
        if not 1 <= total <= self.max_chunks or not 0 <= index < total:
            raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT)
        known = self._parts.get(chunk_id)
        if known is None:
            known = (total, {})
            self._parts[chunk_id] = known
        elif known[0] != total:
            self._parts.pop(chunk_id, None)
            raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT)
        parts = known[1]
        if index in parts:
            self._parts.pop(chunk_id, None)
            raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT)
        if sum(map(len, parts.values())) + len(payload) > self.max_bytes:
            self._parts.pop(chunk_id, None)
            raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT)
        parts[index] = payload
        if len(parts) != total:
            return None
        self._parts.pop(chunk_id, None)
        return b"".join(parts[position] for position in range(total))


def make_outer_frame(payload: bytes, *, chunk_id: int) -> Message:
    """Wrap a serialized ServiceRequest/ServiceResponse in a one-chunk frame.

    The serialized frame is what gets Noise-encrypted; the ciphertext is the
    WebSocket binary message.
    """

    frame = NoiseTransportFrame()
    frame.chunk_id = chunk_id
    frame.chunk_index = 0
    frame.total_chunks = 1
    frame.payload = payload
    return frame


def header_value(headers: Any, name: str) -> str | None:
    lowered = name.lower()
    for header in headers:
        if header.key.lower() == lowered:
            return header.value
    return None
