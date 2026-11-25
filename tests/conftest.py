"""An in-process stand-in for the ADAM-6266, built on the pymodbus server.

The simulator records every request frame as raw bytes off the socket and
decodes the Modbus header by hand, so the tests' claims about which function
code and address went over the wire do not depend on pymodbus decoding its own
client's output.
"""

import socket
import struct
from dataclasses import dataclass
from typing import List, Optional

import pytest
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice
from viam.proto.app.robot import ComponentConfig
from viam.utils import dict_to_struct

from models.adam_6266 import Adam6266

UNIT_ID = 1
COIL_COUNT = 32

# Seed pattern. Every pin differs from the coil on either side of it, so a
# read that is off by one in either direction returns the wrong value for
# every single pin, and DIn differs from RLn, so swapping the banks fails too.
SEED_DI = [True, False, True, False]  # addresses 0-3
SEED_RL = [False, True, False, True]  # addresses 16-19
SEED_NEIGHBOURS = {4: True, 15: True, 20: False}


def seeded_coils(count: int = COIL_COUNT) -> List[bool]:
    coils = [False] * count
    coils[0:4] = SEED_DI
    for address, value in SEED_NEIGHBOURS.items():
        if address < count:
            coils[address] = value
    for offset, value in enumerate(SEED_RL):
        if 16 + offset < count:
            coils[16 + offset] = value
    return coils


@dataclass(frozen=True)
class Request:
    """One Modbus request as it appeared on the wire."""

    unit: int
    function: int
    address: int
    value: int  # quantity for function 01, 0xFF00 / 0x0000 for function 05

    @property
    def is_write(self) -> bool:
        return self.function != 1


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Simulator:
    def __init__(self, coil_count: int = COIL_COUNT, port: Optional[int] = None):
        self.port = port or free_port()
        self.coil_count = coil_count
        self.requests: List[Request] = []
        self.connections_opened = 0
        self.swallow_responses = 0  # answer nothing to the next N requests
        self.max_in_flight = 0
        self._in_flight = 0
        self._coils = seeded_coils(coil_count)
        self._server: Optional[ModbusTcpServer] = None

    async def start(self) -> None:
        unused = [SimData(0, count=1, values=0, datatype=DataType.REGISTERS)]
        device = SimDevice(
            UNIT_ID,
            # Four separate blocks (coils, discrete inputs, holding, input),
            # which makes coil addresses refer to single bits.
            simdata=(
                [SimData(0, values=list(self._coils), datatype=DataType.BITS)],
                [SimData(0, values=[False], datatype=DataType.BITS)],
                unused,
                unused,
            ),
        )
        self._server = ModbusTcpServer(
            device,
            address=("127.0.0.1", self.port),
            trace_packet=self._trace_packet,
            trace_connect=self._trace_connect,
        )
        await self._server.serve_forever(background=True)

    async def stop(self) -> None:
        """Take the device off the network, remembering its coil states."""
        if self._server is not None:
            self._coils = await self.coils()
            await self._server.shutdown()
            self._server = None

    def drop_connections(self) -> None:
        """Close every client connection but keep listening, as the ADAM does
        when a connection passes its Host Idle timeout."""
        assert self._server is not None
        for handler in list(self._server.active_connections.values()):
            handler.close()

    async def coils(self) -> List[bool]:
        if self._server is None:
            return list(self._coils)
        return [bool(v) for v in await self._server.async_getValues(UNIT_ID, 1, 0, self.coil_count)]

    async def set_coil(self, address: int, value: bool) -> None:
        """Change a coil from the device side, without any Modbus traffic."""
        assert self._server is not None
        await self._server.async_setValues(UNIT_ID, 5, address, [value])

    @property
    def writes(self) -> List[Request]:
        return [r for r in self.requests if r.is_write]

    def _trace_connect(self, connected: bool) -> None:
        if connected:
            self.connections_opened += 1
        else:
            self._in_flight = 0

    def _trace_packet(self, sending: bool, data: bytes) -> bytes:
        if sending:
            self._in_flight = max(0, self._in_flight - 1)
            if self.swallow_responses > 0:
                self.swallow_responses -= 1
                return b""
            return data
        frame = bytes(data)
        while len(frame) >= 12:
            _tid, _proto, length, unit, function, address, value = struct.unpack(">HHHBBHH", frame[:12])
            self.requests.append(Request(unit, function, address, value))
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            frame = frame[6 + length :]
        return data


def make_config(port: int, **attributes) -> ComponentConfig:
    attributes = {"host": "127.0.0.1", "port": port, "timeout_s": 0.3, **attributes}
    return ComponentConfig(name="adam-io", attributes=dict_to_struct(attributes))


@pytest.fixture
async def sim():
    simulator = Simulator()
    await simulator.start()
    yield simulator
    await simulator.stop()


@pytest.fixture
async def board(sim):
    component = Adam6266.new(make_config(sim.port), {})
    yield component
    await component.close()
