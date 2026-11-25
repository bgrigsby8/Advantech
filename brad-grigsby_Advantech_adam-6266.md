# Model brad-grigsby:Advantech:adam-6266

Switches the four Form C relays and reads the four digital inputs of an Advantech ADAM-6266 over Modbus TCP, as a Viam board component. Each relay and input is a GPIO pin.

The module only writes a relay when you call `set()` on a relay pin or send the `set_relays` command. Configuring, reconfiguring, reconnecting, the keepalive and closing the component only ever read, so they leave the relays as they are. Every state it returns was read from the device for that call; if the read or write fails, the call raises an error instead of returning a value.

## Configuration

```json
{
  "host": "192.168.1.50"
}
```

### Attributes

| Name                   | Type   | Inclusion | Description                                                                                           |
|------------------------|--------|-----------|-------------------------------------------------------------------------------------------------------|
| `host`                 | string | Required  | IP address or hostname of the ADAM-6266                                                               |
| `port`                 | int    | Optional  | Modbus TCP port. Default `502`                                                                        |
| `unit_id`              | int    | Optional  | Modbus unit id. Default `1`                                                                           |
| `timeout_s`            | float  | Optional  | Timeout in seconds for connecting and for each request. Default `1.0`                                 |
| `keepalive_interval_s` | float  | Optional  | If above `0`, read all eight states in the background at this interval, in seconds. Default `0` (off) |

Set `keepalive_interval_s` if you enable the ADAM's communication watchdog (the setting that drives relays to a fail-safe value when no packet arrives for a while). The ADAM-6200 manual describes the watchdog time as the Host Idle timeout, so choose an interval well below that value so the relays do not drop while the machine is otherwise idle. The keepalive never writes; dropping outputs on communication loss is left to the watchdog.

### Example Configuration

```json
{
  "host": "192.168.1.50",
  "port": 502,
  "unit_id": 1,
  "timeout_s": 1.0,
  "keepalive_interval_s": 5
}
```

A complete component entry, ready to paste into a machine config:

```json
{
  "name": "adam-io",
  "api": "rdk:component:board",
  "model": "brad-grigsby:Advantech:adam-6266",
  "attributes": { "host": "<ADAM IP>" }
}
```

## Pin names

| Pin name    | Function       | Manual coil | Modbus address | Access     |
|-------------|----------------|-------------|----------------|------------|
| `DI0`–`DI3` | Digital inputs | 00001–00004 | 0–3            | read only  |
| `RL0`–`RL3` | Form C relays  | 00017–00020 | 16–19          | read/write |

- `get()` reads that one coil from the device (function 01, Read Coils) and returns it. Nothing is cached, so a relay changed by the Advantech utility or the device's own logic shows up on the next read.
- `set()` on an `RL` pin writes that coil (function 05, Write Single Coil). `set()` on a `DI` pin raises an error and sends nothing.
- Any other pin name raises an error.
- States are reported raw: no inversion and no debouncing.
- PWM, analog readers, digital interrupts, `set_power_mode` and `stream_ticks` are not supported and raise `NotImplementedError`.

## do_command

Send exactly one command per call. Anything else raises an error.

### get_all

Reads all eight states (two requests, addresses 0–3 and 16–19).

```json
{ "get_all": true }
```

```json
{
  "DI0": false, "DI1": false, "DI2": true, "DI3": false,
  "RL0": true, "RL1": false, "RL2": false, "RL3": false
}
```

### set_relays

Sets the named relays, then reads back and returns all four relay states. Relays you do not name are not written.

```json
{ "set_relays": { "RL0": true, "RL1": false } }
```

```json
{ "RL0": true, "RL1": false, "RL2": false, "RL3": false }
```

Names and values are checked before anything is written: an unknown name, a `DI` name, or a value that is not `true` or `false` raises an error and changes nothing. The relays are written one at a time, so if the connection fails partway through, the earlier ones stay written; the error message lists which.

## Connection handling and errors

- The TCP connection is opened by the first request, not when the component is configured, so the component configures successfully even while the device is unreachable.
- The ADAM closes connections that sit idle past its Host Idle timeout (720 s by default, measured on a factory-default unit). The module treats that as normal and reconnects on the next call.
- If a request fails with a connection error or gets no reply within `timeout_s`, the module reconnects and retries that request once, then raises. Repeating a relay write is safe because writing a coil to the same value twice has the same result as writing it once. A single pin call to an unreachable device therefore takes up to about four times `timeout_s` to report an error (connect and request, twice).
- If the device answers with a Modbus exception, the call raises without a retry.
- Requests are sent one at a time; concurrent calls wait their turn.
