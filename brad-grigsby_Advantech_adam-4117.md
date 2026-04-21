# Model brad-grigsby:Advantech:adam-4117

Reads analog input channels from an [Advantech ADAM-4117](https://www.advantech.com/en/products/gf-ifx8/adam-4117/mod_77a1e4a9-71fe-4d90-bdd8-87ed92b92bdc) 8-channel analog input module over RS-485 serial using the ADAM ASCII protocol. Each configured channel is returned as a floating-point voltage value.

## Configuration

```json
{
  "port": "/dev/ttyUSB0",
  "address": "01",
  "baud": 115200,
  "channels": [0, 1, 2, 3]
}
```

### Attributes

| Name       | Type             | Inclusion | Description                                                   |
|------------|------------------|-----------|---------------------------------------------------------------|
| `port`     | string           | Required  | Serial port the ADAM-4117 is connected to (e.g. `/dev/ttyUSB0`) |
| `address`  | string           | Required  | Module address as a two-character hex string (e.g. `"01"`)    |
| `baud`     | int              | Required  | Baud rate for the serial connection (e.g. `115200`)           |
| `channels` | array of int     | Required  | List of channel indices to read (0–7)                         |

### Example Configuration

```json
{
  "port": "/dev/ttyUSB0",
  "address": "01",
  "baud": 115200,
  "channels": [0, 1, 2, 3]
}
```

## Readings

`get_readings` returns one key per configured channel:

```json
{
  "channel_0": -0.0012,
  "channel_1":  0.5031,
  "channel_2":  1.2478,
  "channel_3": -0.0008
}
```

Channels not present in the device response are returned as `0.0`.
