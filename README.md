# cbpi4-BrewVision

CraftBeerPi only listens. A separate script owns the Bluetooth thermometer and pushes readings in.

CBPi was hanging because the plugin talked to BlueZ during startup. That code is no longer imported by the plugin.

## Pieces

| Piece | Role |
|---|---|
| `cbpi4_BrewVision` | Sensor plugin. Listens on `127.0.0.1:5599`. No Bluetooth. |
| `brewvision_collector.py` | Connects to the BrewVision and pushes JSON lines. |

## Install the plugin

```bash
cd cbpi4-BrewVision
pip install -e .
# or: pipx runpip cbpi4 install -e .
```

Restart CraftBeerPi. Add a sensor of type **BrewVision**:

- MAC Address: `D0:B5:C2:AE:6B:3A` (blank accepts any)
- Listen Host: `127.0.0.1`
- Listen Port: `5599`
- Unit: `F` or `C`
- Offset: calibration, applied after the unit conversion

**BrewVision Battery** uses the same host, port, and MAC. It does not open its own connection.

## Run the collector

On the Pi, outside CraftBeerPi (its own terminal or a systemd unit):

```bash
pip install bleak dbus-next
sudo python3 brewvision_collector.py D0:B5:C2:AE:6B:3A
# optional host and port:
sudo python3 brewvision_collector.py D0:B5:C2:AE:6B:3A 127.0.0.1 5599
```

`sudo` is required for `btmgmt sc off`. Start CraftBeerPi first so the port is open, then start the collector. If the plugin is not up yet, the collector keeps the thermometer link and retries the push.

Each line sent:

```json
{"mac":"D0:B5:C2:AE:6B:3A","temp_c":25.4,"battery":86}
```

## Restarting the thermometer link

Restart `brewvision_collector.py`. Do not restart CraftBeerPi for a dropped Bluetooth link. Power-cycle the BrewVision if its LED is off.

## Changelog

- **0.3.0** – Plugin is a TCP listener only. Bluetooth moved to `brewvision_collector.py`.
- **0.2.0** – BlueZ inside the plugin (could hang CBPi startup).
- **0.1.0** – Bleak inside the plugin.
