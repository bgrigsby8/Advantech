# Module Advantech

Viam modular components for Advantech ADAM I/O modules.

- The [ADAM-4117](https://www.advantech.com/en/products/gf-ifx8/adam-4117/mod_77a1e4a9-71fe-4d90-bdd8-87ed92b92bdc) 8-channel analog input module is a Viam sensor. It communicates over RS-485 serial using the ADAM ASCII protocol and exposes each channel's voltage reading.
- The ADAM-6266 4-relay, 4-digital-input module is a Viam board. It communicates over Modbus TCP and exposes each relay and input as a GPIO pin.

## Models

- [`brad-grigsby:Advantech:adam-4117`](brad-grigsby_Advantech_adam-4117.md) - Reads analog input channels from an ADAM-4117 over RS-485 serial
- [`brad-grigsby:Advantech:adam-6266`](brad-grigsby_Advantech_adam-6266.md) - Switches the relays and reads the digital inputs of an ADAM-6266 over Modbus TCP

## Development

The `adam-6266` tests run against an in-process Modbus simulator, so they need no hardware. Test dependencies are in `requirements-dev.txt`, separate from the module's runtime `requirements.txt`.

```sh
venv/bin/python -m pip install -r requirements-dev.txt
venv/bin/python -m pytest
```
