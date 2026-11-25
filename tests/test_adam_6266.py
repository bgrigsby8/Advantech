"""Tests for the adam-6266 board model, run against the simulator in conftest.

Nothing here touches real hardware. See conftest.py for what the simulator is.
"""

import asyncio

import pytest

from conftest import SEED_DI, SEED_RL, UNIT_ID, Request, Simulator, free_port, make_config, seeded_coils
from models.adam_6266 import DI_PINS, PIN_ADDRESSES, RL_PINS, Adam6266, Adam6266Error

ON, OFF = 0xFF00, 0x0000
EXPECTED_ADDRESS = {"DI0": 0, "DI1": 1, "DI2": 2, "DI3": 3, "RL0": 16, "RL1": 17, "RL2": 18, "RL3": 19}
SEED_BY_PIN = {**dict(zip(DI_PINS, SEED_DI)), **dict(zip(RL_PINS, SEED_RL))}


def read(address: int, count: int = 1) -> Request:
    return Request(UNIT_ID, 1, address, count)


def write(address: int, high: bool) -> Request:
    return Request(UNIT_ID, 5, address, ON if high else OFF)


# --------------------------------------------------------------------------- #
# The simulator itself
# --------------------------------------------------------------------------- #


async def test_simulator_answers_hand_built_frames(sim):
    """Frames written out byte by byte from the Modbus spec, no pymodbus client.

    This pins down the simulator's addressing independently, so the address
    tests below are not just pymodbus agreeing with itself.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", sim.port)

    async def exchange(frame_hex: str) -> str:
        writer.write(bytes.fromhex(frame_hex))
        await writer.drain()
        return (await asyncio.wait_for(reader.read(64), 1)).hex()

    # Read Coils, unit 1, start 0x0000, quantity 4 -> one data byte, DI0 in bit 0.
    assert await exchange("0001 0000 0006 01 01 0000 0004") == "0001000000040101" + "01" + "05"
    # Read Coils, start 0x0010 (16), quantity 4 -> RL0 in bit 0.
    assert await exchange("0002 0000 0006 01 01 0010 0004") == "0002000000040101" + "01" + "0a"
    # Write Single Coil 0x0013 (19) off, echoed back, then visible in a read.
    assert await exchange("0003 0000 0006 01 05 0013 0000") == "000300000006010500130000"
    assert await exchange("0004 0000 0006 01 01 0010 0004") == "0004000000040101" + "01" + "02"

    writer.close()
    await writer.wait_closed()


# --------------------------------------------------------------------------- #
# 1. Addresses
# --------------------------------------------------------------------------- #


def test_pin_table_matches_the_manual():
    assert PIN_ADDRESSES == EXPECTED_ADDRESS


@pytest.mark.parametrize("pin_name", list(EXPECTED_ADDRESS))
async def test_get_reads_the_pins_own_coil(sim, board, pin_name):
    pin = await board.gpio_pin_by_name(pin_name)

    assert await pin.get() is SEED_BY_PIN[pin_name]
    # Exactly one request, function 01, at the protocol address, quantity 1.
    assert sim.requests == [read(EXPECTED_ADDRESS[pin_name])]


@pytest.mark.parametrize("pin_name", list(EXPECTED_ADDRESS))
async def test_get_is_a_live_read_not_a_cache(sim, board, pin_name):
    pin = await board.gpio_pin_by_name(pin_name)
    first = await pin.get()

    # Something other than this module changes the coil on the device.
    await sim.set_coil(EXPECTED_ADDRESS[pin_name], not first)

    assert await pin.get() is (not first)


# --------------------------------------------------------------------------- #
# 2. Writes change exactly one coil
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pin_name", RL_PINS)
@pytest.mark.parametrize("high", [True, False])
async def test_set_changes_exactly_that_coil(sim, board, pin_name, high):
    address = EXPECTED_ADDRESS[pin_name]
    expected = seeded_coils()
    expected[address] = high

    await (await board.gpio_pin_by_name(pin_name)).set(high)

    assert await sim.coils() == expected
    assert sim.writes == [write(address, high)]


# --------------------------------------------------------------------------- #
# 3. Refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pin_name", DI_PINS)
@pytest.mark.parametrize("high", [True, False])
async def test_set_on_a_digital_input_raises_and_sends_nothing(sim, board, pin_name, high):
    pin = await board.gpio_pin_by_name(pin_name)

    with pytest.raises(Adam6266Error, match=f"{pin_name} is a digital input"):
        await pin.set(high)

    assert sim.requests == []
    assert await sim.coils() == seeded_coils()


@pytest.mark.parametrize("name", ["RL4", "DI4", "DO0", "rl0", "0", "16", "", "RL0 "])
async def test_unknown_pin_name_raises(sim, board, name):
    with pytest.raises(Adam6266Error, match="unknown pin"):
        await board.gpio_pin_by_name(name)
    assert sim.requests == []


async def test_unsupported_board_features_raise_not_implemented(sim, board):
    pin = await board.gpio_pin_by_name("RL0")
    for call in (
        pin.get_pwm(),
        pin.set_pwm(0.5),
        pin.get_pwm_frequency(),
        pin.set_pwm_frequency(100),
        board.analog_by_name("A0"),
        board.digital_interrupt_by_name("DI0"),
        board.set_power_mode(0),
        board.stream_ticks([]),
    ):
        with pytest.raises(NotImplementedError):
            await call
    assert sim.requests == []


# --------------------------------------------------------------------------- #
# 4. Lifecycle never writes
# --------------------------------------------------------------------------- #


async def test_create_and_close_write_nothing(sim):
    board = Adam6266.new(make_config(sim.port), {})
    await board.close()

    assert sim.writes == []
    assert await sim.coils() == seeded_coils()


async def test_create_read_reconfigure_and_close_write_nothing(sim):
    board = Adam6266.new(make_config(sim.port), {})
    assert await (await board.gpio_pin_by_name("RL1")).get() is True
    board.reconfigure(make_config(sim.port, timeout_s=0.5), {})
    assert await (await board.gpio_pin_by_name("DI0")).get() is True
    board.reconfigure(make_config(sim.port, keepalive_interval_s=0.02), {})
    await asyncio.sleep(0.15)
    await board.close()

    assert len(sim.requests) > 2  # the device really was talked to
    assert sim.writes == []
    assert await sim.coils() == seeded_coils()


async def test_close_is_idempotent_and_later_calls_raise(sim, board):
    pin = await board.gpio_pin_by_name("RL0")
    await board.close()
    await board.close()

    with pytest.raises(Adam6266Error, match="closed"):
        await pin.get()
    with pytest.raises(Adam6266Error, match="closed"):
        await pin.set(True)
    assert sim.requests == []


async def test_reconfigure_after_close_reopens(sim, board):
    await board.close()
    board.reconfigure(make_config(sim.port), {})
    assert await (await board.gpio_pin_by_name("RL1")).get() is True


# --------------------------------------------------------------------------- #
# 5. Connection loss
# --------------------------------------------------------------------------- #


async def test_reconnects_after_the_device_drops_the_connection(sim, board):
    pin = await board.gpio_pin_by_name("RL1")
    assert await pin.get() is True
    assert sim.connections_opened == 1

    sim.drop_connections()

    assert await pin.get() is True
    assert sim.connections_opened == 2


async def test_reconnects_without_a_retry_when_the_drop_was_already_noticed(sim, board):
    # The usual Host Idle case: the device hung up some time before the next
    # call, so the module already knows and simply opens a new connection.
    pin = await board.gpio_pin_by_name("RL1")
    assert await pin.get() is True
    sim.drop_connections()
    await asyncio.sleep(0.05)

    assert await pin.get() is True
    assert sim.requests == [read(17), read(17)]
    assert sim.connections_opened == 2


async def test_write_goes_through_after_the_device_drops_the_connection(sim, board):
    pin = await board.gpio_pin_by_name("RL0")
    assert await pin.get() is False
    sim.drop_connections()

    await pin.set(True)

    assert (await sim.coils())[16] is True
    assert sim.connections_opened == 2


async def test_raises_when_the_device_is_down_then_recovers(sim, board):
    pin = await board.gpio_pin_by_name("RL1")
    assert await pin.get() is True

    await sim.stop()
    with pytest.raises(Adam6266Error, match="failed on ADAM-6266 at 127.0.0.1"):
        await pin.get()
    with pytest.raises(Adam6266Error, match="failed on ADAM-6266 at 127.0.0.1"):
        await pin.set(False)
    with pytest.raises(Adam6266Error):
        await board.do_command({"get_all": True})
    with pytest.raises(Adam6266Error):
        await board.do_command({"set_relays": {"RL0": True}})

    await sim.start()
    assert await pin.get() is True
    assert (await sim.coils()) == seeded_coils()  # the failed writes changed nothing


async def test_raises_when_the_device_was_never_reachable():
    board = Adam6266.new(make_config(free_port()), {})
    try:
        with pytest.raises(Adam6266Error, match="after a reconnect and retry"):
            await (await board.gpio_pin_by_name("DI0")).get()
    finally:
        await board.close()


async def test_lost_reply_is_retried_once_on_a_new_connection(sim, board):
    pin = await board.gpio_pin_by_name("DI2")
    sim.swallow_responses = 1

    assert await pin.get() is True

    assert sim.requests == [read(2), read(2)]
    assert sim.connections_opened == 2


async def test_lost_write_reply_is_retried_once(sim, board):
    pin = await board.gpio_pin_by_name("RL2")
    sim.swallow_responses = 1

    await pin.set(True)

    assert sim.writes == [write(18, True), write(18, True)]
    expected = seeded_coils()
    expected[18] = True
    assert await sim.coils() == expected


async def test_gives_up_after_exactly_one_retry(sim, board):
    pin = await board.gpio_pin_by_name("DI0")
    sim.swallow_responses = 100

    with pytest.raises(Adam6266Error, match="after a reconnect and retry"):
        await pin.get()

    assert sim.requests == [read(0), read(0)]


async def test_device_exception_reply_raises_and_is_not_retried():
    # A device with coils 0-15 only: it answers RL3 with "illegal data address".
    sim = Simulator(coil_count=16)
    await sim.start()
    board = Adam6266.new(make_config(sim.port), {})
    try:
        pin = await board.gpio_pin_by_name("RL3")
        with pytest.raises(Adam6266Error, match="rejected by the device"):
            await pin.get()
        with pytest.raises(Adam6266Error, match="rejected by the device"):
            await pin.set(True)
        assert sim.requests == [read(19), write(19, True)]
    finally:
        await board.close()
        await sim.stop()


async def test_cancelled_call_does_not_poison_the_next_one(sim, board):
    di0 = await board.gpio_pin_by_name("DI0")  # seeded True
    di1 = await board.gpio_pin_by_name("DI1")  # seeded False
    sim.swallow_responses = 1

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(di0.get(), 0.05)

    assert await di1.get() is False
    assert await di0.get() is True


# --------------------------------------------------------------------------- #
# 6. do_command
# --------------------------------------------------------------------------- #


async def test_get_all_returns_what_the_device_holds(sim, board):
    assert await board.do_command({"get_all": True}) == SEED_BY_PIN
    assert sim.requests == [read(0, 4), read(16, 4)]

    await sim.set_coil(1, True)  # DI1
    await sim.set_coil(19, False)  # RL3

    assert await board.do_command({"get_all": True}) == {**SEED_BY_PIN, "DI1": True, "RL3": False}


async def test_set_relays_writes_only_the_named_relays_and_returns_all_four(sim, board):
    result = await board.do_command({"set_relays": {"RL0": True, "RL1": False}})

    coils = await sim.coils()
    expected = seeded_coils()
    expected[16], expected[17] = True, False
    assert coils == expected
    assert result == dict(zip(RL_PINS, coils[16:20])) == {"RL0": True, "RL1": False, "RL2": False, "RL3": True}
    assert sim.requests == [write(16, True), write(17, False), read(16, 4)]


@pytest.mark.parametrize(
    "relays",
    [
        {"RL0": True, "DI0": True},
        {"RL0": True, "RL4": True},
        {"RL0": True, "RL1": 1},
        {"RL0": True, "RL1": "on"},
        {"RL0": True, "RL1": None},
        {},
        ["RL0"],
        True,
    ],
)
async def test_invalid_set_relays_raises_before_writing_anything(sim, board, relays):
    with pytest.raises(Adam6266Error):
        await board.do_command({"set_relays": relays})

    assert sim.requests == []
    assert await sim.coils() == seeded_coils()


@pytest.mark.parametrize(
    "command",
    [
        {},
        {"get_all": False},
        {"get_all": "yes"},
        {"set_all": True},
        {"get_all": True, "set_relays": {"RL0": True}},
    ],
)
async def test_unsupported_commands_raise(sim, board, command):
    with pytest.raises(Adam6266Error):
        await board.do_command(command)
    assert sim.requests == []


# --------------------------------------------------------------------------- #
# Concurrency, keepalive, config
# --------------------------------------------------------------------------- #


async def test_concurrent_calls_are_serialized_on_the_wire(sim, board):
    pins = {name: await board.gpio_pin_by_name(name) for name in EXPECTED_ADDRESS}
    calls = []
    for _ in range(5):
        calls += [pins[name].get() for name in EXPECTED_ADDRESS]
        calls += [board.do_command({"get_all": True})]
        calls += [pins["RL0"].set(True), pins["RL3"].set(False)]

    results = await asyncio.gather(*calls)

    assert sim.max_in_flight == 1
    assert sim.connections_opened == 1
    assert results[0:4] == SEED_DI  # the inputs never change
    expected = seeded_coils()
    expected[16], expected[19] = True, False
    assert await sim.coils() == expected


async def test_keepalive_reads_at_the_interval_and_stops_on_close(sim):
    board = Adam6266.new(make_config(sim.port, keepalive_interval_s=0.03), {})
    await asyncio.sleep(0.25)
    await board.close()
    seen = list(sim.requests)

    assert len(seen) >= 6  # at least three rounds of two reads
    assert set(seen) == {read(0, 4), read(16, 4)}

    await asyncio.sleep(0.15)
    assert sim.requests == seen


async def test_keepalive_survives_the_device_going_away(sim):
    board = Adam6266.new(make_config(sim.port, keepalive_interval_s=0.03, timeout_s=0.1), {})
    try:
        await asyncio.sleep(0.1)
        await sim.stop()
        await asyncio.sleep(0.3)
        sim.requests.clear()
        await sim.start()
        await asyncio.sleep(0.3)

        assert read(0, 4) in sim.requests
        assert sim.writes == []
    finally:
        await board.close()


async def test_keepalive_is_off_by_default(sim, board):
    await asyncio.sleep(0.2)
    assert sim.requests == []
    assert sim.connections_opened == 0


def test_validate_config_accepts_the_minimal_and_full_configs():
    assert Adam6266.validate_config(make_config(502)) == ([], [])
    full = make_config(1502, unit_id=3, timeout_s=2, keepalive_interval_s=10)
    assert Adam6266.validate_config(full) == ([], [])


@pytest.mark.parametrize(
    "attributes",
    [
        {"host": ""},
        {"host": None},
        {"host": 5},
        {"port": 0},
        {"port": 70000},
        {"port": 502.5},
        {"port": "502"},
        {"port": True},
        {"unit_id": 256},
        {"unit_id": -1},
        {"timeout_s": 0},
        {"timeout_s": -1},
        {"timeout_s": "1"},
        {"keepalive_interval_s": -1},
    ],
)
def test_validate_config_rejects_bad_attributes(attributes):
    with pytest.raises(Adam6266Error):
        Adam6266.validate_config(make_config(**{"port": 502, **attributes}))


async def test_defaults_are_port_502_unit_1():
    from viam.proto.app.robot import ComponentConfig
    from viam.utils import dict_to_struct

    board = Adam6266.new(ComponentConfig(name="adam-io", attributes=dict_to_struct({"host": "192.0.2.10"})), {})
    try:
        settings = board._settings
        assert (settings.host, settings.port, settings.unit_id) == ("192.0.2.10", 502, 1)
        assert (settings.timeout_s, settings.keepalive_interval_s) == (1.0, 0.0)
    finally:
        await board.close()
