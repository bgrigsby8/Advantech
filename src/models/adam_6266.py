"""Viam board model for the Advantech ADAM-6266 (4 relays, 4 digital inputs).

The device is driven over Modbus TCP. Both the digital inputs and the relays
live in the coil (0X) space, so everything is read with function 01 (Read
Coils) and relays are written with function 05 (Write Single Coil).

Safety properties this module is built around:

* A coil is written only by an explicit ``set()`` on an ``RL`` pin or by the
  ``set_relays`` command. Constructing, reconfiguring, closing, reconnecting
  and the optional keepalive only ever read.
* A state is returned only if it was read from the device for that call. Any
  failure raises; there is no cache and no default value.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from typing_extensions import Self
from viam.components.board import Board, TickStream
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.easy_resource import EasyResource
from viam.resource.types import Model, ModelFamily
from viam.utils import ValueTypes, struct_to_dict

# ADAM-6200 series manual, Appendix A.1.8. The manual lists 1-based coil
# numbers (00001-00004 and 00017-00020); the protocol address is one less.
DI_BASE_ADDRESS = 0
RL_BASE_ADDRESS = 16
CHANNELS_PER_BANK = 4

DI_PINS: Tuple[str, ...] = tuple(f"DI{i}" for i in range(CHANNELS_PER_BANK))
RL_PINS: Tuple[str, ...] = tuple(f"RL{i}" for i in range(CHANNELS_PER_BANK))
PIN_ADDRESSES: Dict[str, int] = {
    **{name: DI_BASE_ADDRESS + i for i, name in enumerate(DI_PINS)},
    **{name: RL_BASE_ADDRESS + i for i, name in enumerate(RL_PINS)},
}

FC_READ_COILS = 1
FC_WRITE_SINGLE_COIL = 5

DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 1
DEFAULT_TIMEOUT_S = 1.0
DEFAULT_KEEPALIVE_INTERVAL_S = 0.0


class Adam6266Error(Exception):
    """A request to the ADAM-6266 failed, or was refused by this module."""


@dataclass(frozen=True)
class _Settings:
    host: str
    port: int
    unit_id: int
    timeout_s: float
    keepalive_interval_s: float


def _number(attributes: Mapping[str, Any], key: str, default: float) -> float:
    value = attributes.get(key, default)
    if value is None:
        return default
    # bool is a subclass of int; `"port": true` is a mistake, not port 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Adam6266Error(f"{key} must be a number, got {value!r}")
    return float(value)


def _integer(attributes: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = _number(attributes, key, default)
    # Config numbers arrive as floats (502.0); accept those, reject 502.5.
    if not value.is_integer() or not low <= value <= high:
        raise Adam6266Error(f"{key} must be an integer from {low} to {high}, got {value!r}")
    return int(value)


def _parse_settings(config: ComponentConfig) -> _Settings:
    attributes = struct_to_dict(config.attributes)

    host = attributes.get("host")
    if not isinstance(host, str) or not host.strip():
        raise Adam6266Error("host is required (IP address or hostname of the ADAM-6266)")

    timeout_s = _number(attributes, "timeout_s", DEFAULT_TIMEOUT_S)
    if timeout_s <= 0:
        raise Adam6266Error(f"timeout_s must be greater than 0, got {timeout_s!r}")

    keepalive_interval_s = _number(attributes, "keepalive_interval_s", DEFAULT_KEEPALIVE_INTERVAL_S)
    if keepalive_interval_s < 0:
        raise Adam6266Error(f"keepalive_interval_s must be 0 (off) or greater, got {keepalive_interval_s!r}")

    return _Settings(
        host=host.strip(),
        port=_integer(attributes, "port", DEFAULT_PORT, 1, 65535),
        unit_id=_integer(attributes, "unit_id", DEFAULT_UNIT_ID, 0, 255),
        timeout_s=timeout_s,
        keepalive_interval_s=keepalive_interval_s,
    )


class Adam6266Pin(Board.GPIOPin):
    """One ADAM-6266 channel: ``DI0``-``DI3`` (read only) or ``RL0``-``RL3``."""

    def __init__(self, name: str, board: "Adam6266"):
        super().__init__(name)
        self._board = board

    async def get(self, *, extra: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, **kwargs) -> bool:
        return await self._board._read_pin(self.name)

    async def set(self, high: bool, *, extra: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, **kwargs):
        await self._board._write_relays({self.name: high})

    async def get_pwm(self, *, extra: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, **kwargs) -> float:
        raise NotImplementedError("the ADAM-6266 model does not support PWM")

    async def set_pwm(
        self, duty_cycle: float, *, extra: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, **kwargs
    ):
        raise NotImplementedError("the ADAM-6266 model does not support PWM")

    async def get_pwm_frequency(
        self, *, extra: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, **kwargs
    ) -> int:
        raise NotImplementedError("the ADAM-6266 model does not support PWM")

    async def set_pwm_frequency(
        self, frequency: int, *, extra: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, **kwargs
    ):
        raise NotImplementedError("the ADAM-6266 model does not support PWM")


class Adam6266(Board, EasyResource):
    MODEL: ClassVar[Model] = Model(ModelFamily("brad-grigsby", "Advantech"), "adam-6266")

    def __init__(self, name: str):
        super().__init__(name)
        self._settings: Optional[_Settings] = None
        # One Modbus transaction on the wire at a time. Viam calls the
        # component concurrently, and the keepalive shares the connection.
        self._lock = asyncio.Lock()
        self._client: Optional[AsyncModbusTcpClient] = None
        self._client_settings: Optional[_Settings] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._closed = False

    @classmethod
    def new(cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]) -> Self:
        board = cls(config.name)
        board.reconfigure(config, dependencies)
        return board

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Tuple[Sequence[str], Sequence[str]]:
        _parse_settings(config)
        return [], []

    def reconfigure(self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]) -> None:
        """Apply new attributes. Performs no device I/O and never writes a coil.

        The connection is opened lazily by the first request (or by the
        keepalive), so an unreachable device does not fail configuration.
        """
        settings = _parse_settings(config)
        self._stop_keepalive()
        self._settings = settings
        self._closed = False
        # A request in flight keeps its connection; the next one sees that the
        # settings changed and opens a new one.
        if not self._lock.locked():
            self._drop_connection()
        if settings.keepalive_interval_s > 0:
            # Raises if there is no running loop. That is deliberate: the
            # keepalive is what stops the ADAM watchdog from dropping relays,
            # so failing to start it must not pass silently.
            self._keepalive_task = asyncio.get_running_loop().create_task(
                self._keepalive(settings.keepalive_interval_s), name=f"adam-6266 keepalive {self.name}"
            )
        self.logger.info(
            "Configured ADAM-6266 at %s:%d unit %d (timeout %.3gs, keepalive %s)",
            settings.host,
            settings.port,
            settings.unit_id,
            settings.timeout_s,
            f"every {settings.keepalive_interval_s:g}s" if settings.keepalive_interval_s > 0 else "off",
        )

    async def close(self):
        """Stop the keepalive and close the socket. Leaves the relays alone."""
        self._closed = True
        task = self._stop_keepalive()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            self._drop_connection()

    # ------------------------------------------------------------------ #
    # Board API
    # ------------------------------------------------------------------ #

    async def gpio_pin_by_name(self, name: str) -> Board.GPIOPin:
        if name not in PIN_ADDRESSES:
            raise Adam6266Error(
                f"unknown pin {name!r}; the ADAM-6266 has {', '.join(DI_PINS)} (inputs) and {', '.join(RL_PINS)} (relays)"
            )
        return Adam6266Pin(name, self)

    async def analog_by_name(self, name: str) -> Board.Analog:
        raise NotImplementedError("the ADAM-6266 has no analog channels")

    async def digital_interrupt_by_name(self, name: str) -> Board.DigitalInterrupt:
        raise NotImplementedError("the ADAM-6266 model does not support digital interrupts")

    async def set_power_mode(
        self,
        mode: int,
        duration: Optional[timedelta] = None,
        *,
        extra: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ):
        raise NotImplementedError("the ADAM-6266 model does not support power modes")

    async def stream_ticks(
        self, interrupts: List[Board.DigitalInterrupt], *, timeout: Optional[float] = None, **kwargs
    ) -> TickStream:
        raise NotImplementedError("the ADAM-6266 model does not support digital interrupts")

    async def do_command(
        self, command: Mapping[str, ValueTypes], *, timeout: Optional[float] = None, **kwargs
    ) -> Mapping[str, ValueTypes]:
        """``{"get_all": true}`` or ``{"set_relays": {"RL0": true, ...}}``, one per call."""
        keys = sorted(command.keys())
        if keys == ["get_all"]:
            if command["get_all"] is not True:
                raise Adam6266Error(f'get_all must be true, got {command["get_all"]!r}')
            return await self._read_all()
        if keys == ["set_relays"]:
            relays = command["set_relays"]
            if not isinstance(relays, Mapping) or not relays:
                raise Adam6266Error('set_relays must be a non-empty object such as {"RL0": true, "RL1": false}')
            return await self._write_relays(relays)
        raise Adam6266Error(
            f'unsupported command {keys!r}; send exactly one of {{"get_all": true}} or {{"set_relays": {{...}}}}'
        )

    # ------------------------------------------------------------------ #
    # Device operations. Each holds the lock for its whole duration.
    # ------------------------------------------------------------------ #

    async def _read_pin(self, name: str) -> bool:
        async with self._lock:
            return (await self._read_coils(PIN_ADDRESSES[name], 1))[0]

    async def _read_all(self) -> Dict[str, bool]:
        async with self._lock:
            return await self._read_banks()

    async def _read_banks(self) -> Dict[str, bool]:
        # Two reads rather than one spanning 0-19, so no request touches the
        # addresses between the two banks. Caller holds the lock.
        inputs = await self._read_coils(DI_BASE_ADDRESS, CHANNELS_PER_BANK)
        relays = await self._read_coils(RL_BASE_ADDRESS, CHANNELS_PER_BANK)
        return {**dict(zip(DI_PINS, inputs)), **dict(zip(RL_PINS, relays))}

    async def _write_relays(self, relays: Mapping[str, Any]) -> Dict[str, bool]:
        """Write the given relays and return all four relay states, read back.

        Everything is validated before the first write, so a bad name or value
        changes nothing.
        """
        for name, high in relays.items():
            if name in DI_PINS:
                raise Adam6266Error(f"{name} is a digital input and cannot be set")
            if name not in RL_PINS:
                raise Adam6266Error(f"unknown relay {name!r}; the ADAM-6266 has {', '.join(RL_PINS)}")
            if not isinstance(high, bool):
                raise Adam6266Error(f"value for {name} must be true or false, got {high!r}")

        async with self._lock:
            written: List[str] = []
            try:
                for name, high in relays.items():
                    await self._write_coil(PIN_ADDRESSES[name], high)
                    written.append(name)
            except Adam6266Error as exc:
                if len(relays) > 1:
                    done = ", ".join(written) or "none"
                    raise Adam6266Error(f"{exc} (relays written before the failure: {done})") from exc
                raise
            states = await self._read_coils(RL_BASE_ADDRESS, CHANNELS_PER_BANK)
            return dict(zip(RL_PINS, states))

    async def _keepalive(self, interval_s: float) -> None:
        """Read (never write) at a fixed interval so the ADAM watchdog sees traffic."""
        failing = False
        while True:
            await asyncio.sleep(interval_s)
            try:
                async with self._lock:
                    await self._read_banks()
            except Exception as exc:
                # Catch everything: if this task died, nothing would keep the
                # ADAM watchdog fed and the relays would drop mid-job.
                if not failing:
                    self.logger.warning("ADAM-6266 keepalive read failed, will keep trying: %s", exc)
                failing = True
            else:
                if failing:
                    self.logger.info("ADAM-6266 keepalive read succeeded again")
                failing = False

    def _stop_keepalive(self) -> Optional[asyncio.Task]:
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None:
            task.cancel()
        return task

    # ------------------------------------------------------------------ #
    # Modbus transport. Callers hold the lock.
    # ------------------------------------------------------------------ #

    async def _read_coils(self, address: int, count: int) -> List[bool]:
        what = f"read of coils {address}-{address + count - 1}"

        async def request(client: AsyncModbusTcpClient, unit_id: int) -> List[bool]:
            response = await client.read_coils(address, count=count, device_id=unit_id)
            self._check_response(response, FC_READ_COILS, what)
            # The reply is padded to a whole byte, so more bits than asked for
            # is normal; fewer means the reply does not answer the request.
            if len(response.bits) < count:
                raise Adam6266Error(f"{what} returned {len(response.bits)} bits, expected {count}")
            return [bool(bit) for bit in response.bits[:count]]

        return await self._transact(what, request)

    async def _write_coil(self, address: int, high: bool) -> None:
        what = f"write of coil {address} to {'on' if high else 'off'}"

        async def request(client: AsyncModbusTcpClient, unit_id: int) -> None:
            response = await client.write_coil(address, high, device_id=unit_id)
            self._check_response(response, FC_WRITE_SINGLE_COIL, what)
            # A successful Write Single Coil echoes the request.
            if response.address != address or not response.bits or bool(response.bits[0]) != high:
                raise Adam6266Error(f"{what} was not confirmed: the device echoed {response}")

        await self._transact(what, request)

    @staticmethod
    def _check_response(response: Any, function_code: int, what: str) -> None:
        if response.isError():
            raise Adam6266Error(f"{what} was rejected by the device: {response}")
        if response.function_code != function_code:
            raise Adam6266Error(f"{what} got an unexpected reply: {response}")

    async def _transact(self, what: str, request):
        """Run one request, reconnecting and retrying once on a connection error.

        The retry is safe for writes because Write Single Coil is idempotent.
        A reply from the device that says "no" (a Modbus exception, a wrong
        echo) is not a connection error and is raised without a retry.
        """
        if self._closed or self._settings is None:
            raise Adam6266Error(f"{what} refused: the component is closed")
        settings = self._settings
        where = f"ADAM-6266 at {settings.host}:{settings.port}"

        last_error: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                client = await self._connected_client(settings)
                return await request(client, settings.unit_id)
            except (ModbusException, OSError, asyncio.TimeoutError) as exc:
                # Never reuse a connection after a failure: a late reply to
                # this request must not be taken as the answer to the next.
                self._drop_connection()
                last_error = exc
                if attempt == 1:
                    self.logger.debug("%s failed on %s (%s); reconnecting and retrying once", what, where, exc)
            except BaseException:
                # Cancellation, or a reply we do not accept as an answer.
                # Not retried, and the connection is not trusted afterwards.
                self._drop_connection()
                raise
        raise Adam6266Error(f"{what} failed on {where} after a reconnect and retry: {last_error}") from last_error

    async def _connected_client(self, settings: _Settings) -> AsyncModbusTcpClient:
        if self._client is not None and self._client_settings == settings and self._client.connected:
            return self._client
        # Dropped by the device (it closes idle connections), never opened, or
        # opened with old settings. Start from a fresh client every time.
        self._drop_connection()
        client = AsyncModbusTcpClient(
            settings.host,
            port=settings.port,
            timeout=settings.timeout_s,
            # Reconnecting and retrying are done here, once, where they are
            # visible. Turn off pymodbus's own background reconnect and its
            # per-request retries so the two do not multiply.
            retries=0,
            reconnect_delay=0,
            name=f"adam-6266 {self.name}",
        )
        try:
            connected = await client.connect()
        except BaseException:
            client.close()
            raise
        if not connected:
            client.close()
            raise ConnectionError(f"could not connect within {settings.timeout_s:g}s")
        self._client = client
        self._client_settings = settings
        return client

    def _drop_connection(self) -> None:
        client, self._client, self._client_settings = self._client, None, None
        if client is not None:
            client.close()
