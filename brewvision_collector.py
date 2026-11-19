#!/usr/bin/env python3
"""
BrewVision Temperature Test

Device (from btmon):
  LE only, TI, DisplayOnly, Bonding + MITM + Legacy (no Secure Connections).
  Pairing fails with SMP Confirm value failed when the central asks for SC,
  and Pair() can also time out and drop the link.

This script:
  1. Disables Secure Connections (btmgmt sc off) so pairing is Legacy
  2. KeyboardOnly agent, PIN 0000
  3. Tries to bond, but does NOT require it
  4. Immediately reuses or reopens the BlueZ connection and notifies on ffa4
     (does not wait for Bleak to rediscover a sleeping device)

Requires: bleak, dbus-next
  pip install bleak dbus-next

Usage:
    python3 test_brewvision_notify.py
    python3 test_brewvision_notify.py D0:B5:C2:AE:6B:3A
"""

import asyncio
import json
import socket
import subprocess
import sys
import time
from datetime import datetime

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from dbus_next.aio import MessageBus
from dbus_next import BusType, Variant
from dbus_next.service import ServiceInterface, method

DEFAULT_MAC = "D0:B5:C2:AE:6B:3A"
TARGET_PASSKEY = 0            # numeric; BlueZ reports this as 0x0000
TARGET_PIN = "0000"           # 4 zeros, matches nRF Connect PIN
ADAPTER = "hci0"
AGENT_PATH = "/brewvision/agent"
AGENT_CAPABILITY = "KeyboardOnly"

TEMP_UUID = "0000ffa4-0000-1000-8000-00805f9b34fb"
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

SCAN_TIMEOUT = 12.0
CONNECT_SETTLE = 1.0
PAIR_TIMEOUT = 12.0           # short: device sleeps if we wait ~45s
RECONNECT_TRIES = 4

SCRIPT_START = None
PUSH_HOST = "127.0.0.1"
PUSH_PORT = 5599
CURRENT_MAC = DEFAULT_MAC
LAST_BATTERY = None
_push_sock = None


def push(extra=None):
    """Send one JSON line to the CBPi listener. Never raises."""
    global _push_sock
    payload = {"mac": CURRENT_MAC}
    if extra:
        payload.update(extra)
    if LAST_BATTERY is not None:
        payload.setdefault("battery", LAST_BATTERY)
    line = (json.dumps(payload) + "\n").encode()
    try:
        if _push_sock is None:
            s = socket.create_connection((PUSH_HOST, PUSH_PORT), timeout=0.4)
            s.settimeout(0.4)
            _push_sock = s
            log(f"[PUSH] connected to {PUSH_HOST}:{PUSH_PORT}")
        _push_sock.sendall(line)
    except Exception as e:
        if _push_sock is not None:
            try:
                _push_sock.close()
            except Exception:
                pass
            _push_sock = None
        log(f"[PUSH] {PUSH_HOST}:{PUSH_PORT} not ready ({e})")


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def format_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def run_cmd(args, timeout=8):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", "not found"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def force_legacy_pairing() -> bool:
    log("[SC] Forcing LE Legacy pairing (disable Secure Connections) ...")
    rc, out, err = run_cmd(["sudo", "btmgmt", "-i", ADAPTER, "power", "off"])
    log(f"     btmgmt power off: rc={rc} {out or err}")
    rc, out, err = run_cmd(["sudo", "btmgmt", "-i", ADAPTER, "sc", "off"])
    log(f"     btmgmt sc off: rc={rc} {out or err}")
    sc_off = rc == 0
    run_cmd(["sudo", "btmgmt", "-i", ADAPTER, "le", "on"])
    run_cmd(["sudo", "btmgmt", "-i", ADAPTER, "connectable", "on"])
    rc, out, err = run_cmd(["sudo", "btmgmt", "-i", ADAPTER, "bondable", "on"])
    log(f"     btmgmt bondable on: rc={rc} {out or err}")
    rc, out, err = run_cmd(["sudo", "btmgmt", "-i", ADAPTER, "power", "on"])
    log(f"     btmgmt power on: rc={rc} {out or err}")
    if sc_off:
        log("     Secure Connections disabled (Legacy preferred).")
    else:
        log("     WARNING: btmgmt sc off failed; BlueZ may still set SC.")
    log("")
    return sc_off


class LegacyPasskeyAgent(ServiceInterface):
    def __init__(self, passkey: int, pin: str):
        super().__init__("org.bluez.Agent1")
        self.passkey = passkey
        self.pin = pin

    @method()
    def Release(self):
        log("[AGENT] Release()")

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        log(f"[AGENT] RequestPinCode -> '{self.pin}'")
        return self.pin

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        log(f"[AGENT] DisplayPinCode {pincode}")

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        log(f"[AGENT] RequestPasskey -> {self.passkey:04d}")
        return self.passkey

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        log(f"[AGENT] DisplayPasskey {passkey:06d}")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        log(f"[AGENT] RequestConfirmation {passkey:06d} -> accept")

    @method()
    def RequestAuthorization(self, device: "o"):
        log("[AGENT] RequestAuthorization -> accept")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        log(f"[AGENT] AuthorizeService {uuid} -> accept")

    @method()
    def Cancel(self):
        log("[AGENT] Cancel()")


async def get_managed_objects(bus):
    intro = await bus.introspect("org.bluez", "/")
    obj = bus.get_proxy_object("org.bluez", "/", intro)
    mgr = obj.get_interface("org.freedesktop.DBus.ObjectManager")
    return await mgr.call_get_managed_objects()


async def find_device_path(bus, mac: str):
    objects = await get_managed_objects(bus)
    wanted = mac.upper()
    for path, ifaces in objects.items():
        props = ifaces.get("org.bluez.Device1")
        if not props:
            continue
        addr = props.get("Address")
        if addr and str(addr.value).upper() == wanted:
            return path
    return None


async def remove_device(bus, mac: str):
    path = await find_device_path(bus, mac)
    if not path:
        log(f"  No existing BlueZ object for {mac}")
        return
    log(f"  Removing {path}")
    try:
        adapter_path = f"/org/bluez/{ADAPTER}"
        intro = await bus.introspect("org.bluez", adapter_path)
        obj = bus.get_proxy_object("org.bluez", adapter_path, intro)
        adapter = obj.get_interface("org.bluez.Adapter1")
        await adapter.call_remove_device(path)
        log("  Removed.")
        await asyncio.sleep(0.4)
    except Exception as e:
        log(f"  Remove note: {e}")


async def setup_adapter(bus):
    adapter_path = f"/org/bluez/{ADAPTER}"
    intro = await bus.introspect("org.bluez", adapter_path)
    obj = bus.get_proxy_object("org.bluez", adapter_path, intro)
    props = obj.get_interface("org.freedesktop.DBus.Properties")
    for name, val in (("Powered", True), ("Pairable", True), ("Discoverable", True)):
        try:
            await props.call_set("org.bluez.Adapter1", name, Variant("b", val))
            log(f"  Adapter {name} = {val}")
        except Exception as e:
            log(f"  Adapter {name} note: {e}")


async def register_agent(bus):
    agent = LegacyPasskeyAgent(TARGET_PASSKEY, TARGET_PIN)
    bus.export(AGENT_PATH, agent)
    intro = await bus.introspect("org.bluez", "/org/bluez")
    obj = bus.get_proxy_object("org.bluez", "/org/bluez", intro)
    manager = obj.get_interface("org.bluez.AgentManager1")
    await manager.call_register_agent(AGENT_PATH, AGENT_CAPABILITY)
    await manager.call_request_default_agent(AGENT_PATH)
    log(
        f"  Agent: {AGENT_CAPABILITY}, "
        f"passkey={TARGET_PASSKEY:04d}, pin='{TARGET_PIN}'"
    )
    return manager


async def unregister_agent(bus, manager):
    if manager:
        try:
            await manager.call_unregister_agent(AGENT_PATH)
        except Exception:
            pass
    try:
        bus.unexport(AGENT_PATH)
    except Exception:
        pass


async def is_connected(bus, path: str) -> bool:
    try:
        intro = await bus.introspect("org.bluez", path)
        obj = bus.get_proxy_object("org.bluez", path, intro)
        props = obj.get_interface("org.freedesktop.DBus.Properties")
        c = await props.call_get("org.bluez.Device1", "Connected")
        return bool(c.value)
    except Exception:
        return False


async def bluez_connect(bus, path: str, timeout: float = 12.0) -> bool:
    intro = await bus.introspect("org.bluez", path)
    obj = bus.get_proxy_object("org.bluez", path, intro)
    device = obj.get_interface("org.bluez.Device1")
    try:
        await asyncio.wait_for(device.call_connect(), timeout=timeout)
    except Exception as e:
        log(f"  Connect() note: {type(e).__name__}: {e}")
    await asyncio.sleep(0.4)
    ok = await is_connected(bus, path)
    log(f"  Connected: {ok}")
    return ok


async def pair_legacy_passkey(bus, device_path: str) -> bool:
    """Connect, try Pair with PIN 0000. Do not drop the link on failure."""
    log("  Connect() before pair ...")
    await bluez_connect(bus, device_path)
    await asyncio.sleep(CONNECT_SETTLE)

    intro = await bus.introspect("org.bluez", device_path)
    obj = bus.get_proxy_object("org.bluez", device_path, intro)
    device = obj.get_interface("org.bluez.Device1")

    log(f"  Pair() passkey {TARGET_PASSKEY:04d} / PIN {TARGET_PIN} (timeout {PAIR_TIMEOUT:.0f}s) ...")
    try:
        await asyncio.wait_for(device.call_pair(), timeout=PAIR_TIMEOUT)
        log("  Pair() returned OK")
    except asyncio.TimeoutError:
        log("  Pair() timed out — device may drop the link. Will reconnect now.")
        return False
    except Exception as e:
        log(f"  Pair() FAILED: {type(e).__name__}: {e}")
        return False

    try:
        props = obj.get_interface("org.freedesktop.DBus.Properties")
        paired = await props.call_get("org.bluez.Device1", "Paired")
        log(f"  Paired = {paired.value}")
        return bool(paired.value)
    except Exception:
        return False


async def ensure_bluez_object(bus, mac: str) -> str | None:
    path = await find_device_path(bus, mac)
    if path:
        return path
    adapter_path = f"/org/bluez/{ADAPTER}"
    intro = await bus.introspect("org.bluez", adapter_path)
    obj = bus.get_proxy_object("org.bluez", adapter_path, intro)
    adapter = obj.get_interface("org.bluez.Adapter1")
    try:
        await adapter.call_set_discovery_filter({"Transport": Variant("s", "le")})
    except Exception:
        pass
    try:
        await adapter.call_start_discovery()
    except Exception as e:
        log(f"  Discovery note: {e}")
    for _ in range(int(SCAN_TIMEOUT * 2)):
        path = await find_device_path(bus, mac)
        if path:
            break
        await asyncio.sleep(0.5)
    try:
        await adapter.call_stop_discovery()
    except Exception:
        pass
    return path


async def reconnect_fast(bus, mac: str, path: str | None) -> str | None:
    """Get a live connection as fast as possible after a failed pair."""
    log("[RECONNECT] Opening link for notifications ...")
    if path and await is_connected(bus, path):
        log("  Still connected — using existing link")
        return path

    if path:
        for i in range(1, RECONNECT_TRIES + 1):
            log(f"  Connect attempt {i}/{RECONNECT_TRIES} ...")
            if await bluez_connect(bus, path, timeout=8.0):
                return path
            await asyncio.sleep(0.3)

    log("  Not connected. Short LE scan for a fresh advertisement ...")
    path = await ensure_bluez_object(bus, mac)
    if not path:
        log("  Device not advertising. Power-cycle BrewVision (blue LED) and rerun.")
        return None
    log(f"  Device path: {path}")
    if await bluez_connect(bus, path, timeout=12.0):
        return path
    return None


def print_temp(data: bytes, source: str = "ffa4"):
    global SCRIPT_START
    if len(data) < 2:
        return
    if SCRIPT_START is None:
        SCRIPT_START = time.monotonic()
    temp_c = data[0] + data[1] / 10.0
    temp_f = c_to_f(temp_c)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elapsed = format_elapsed(time.monotonic() - SCRIPT_START)
    print(
        f"{now}  |  +{elapsed:>7}  |  "
        f"{temp_c:5.1f} °C  |  {temp_f:5.1f} °F  |  raw: {data.hex()}  ({source})",
        flush=True,
    )
    push({"temp_c": round(temp_c, 2)})


async def find_char_path(bus, device_path: str, uuid: str) -> str | None:
    objects = await get_managed_objects(bus)
    wanted = uuid.lower()
    for path, ifaces in objects.items():
        if not path.startswith(device_path):
            continue
        props = ifaces.get("org.bluez.GattCharacteristic1")
        if not props:
            continue
        u = props.get("UUID")
        if u and str(u.value).lower() == wanted:
            return path
    return None


async def monitor_via_bluez(bus, device_path: str) -> bool:
    """StartNotify on ffa4 over the existing BlueZ connection. Returns when link drops."""
    # Resolve services if needed
    try:
        intro = await bus.introspect("org.bluez", device_path)
        obj = bus.get_proxy_object("org.bluez", device_path, intro)
        dev = obj.get_interface("org.bluez.Device1")
        props = obj.get_interface("org.freedesktop.DBus.Properties")
        resolved = await props.call_get("org.bluez.Device1", "ServicesResolved")
        if not resolved.value:
            log("  Waiting for services ...")
            for _ in range(20):
                await asyncio.sleep(0.25)
                resolved = await props.call_get("org.bluez.Device1", "ServicesResolved")
                if resolved.value:
                    break
    except Exception as e:
        log(f"  Service resolve note: {e}")

    char_path = await find_char_path(bus, device_path, TEMP_UUID)
    if not char_path:
        log("  ffa4 characteristic not in BlueZ cache yet")
        return False

    log(f"  ffa4 path: {char_path}")
    intro = await bus.introspect("org.bluez", char_path)
    cobj = bus.get_proxy_object("org.bluez", char_path, intro)
    char = cobj.get_interface("org.bluez.GattCharacteristic1")
    cprops = cobj.get_interface("org.freedesktop.DBus.Properties")

    def on_props(interface_name, changed, invalidated):
        if interface_name != "org.bluez.GattCharacteristic1":
            return
        if "Value" not in changed:
            return
        raw = changed["Value"].value
        data = bytes(raw)
        print_temp(data, "bluez")

    cprops.on_properties_changed(on_props)

    try:
        await char.call_start_notify()
    except Exception as e:
        log(f"  StartNotify failed: {e}")
        return False

    # Battery is optional
    batt_path = await find_char_path(bus, device_path, BATTERY_UUID)
    if batt_path:
        try:
            bintro = await bus.introspect("org.bluez", batt_path)
            bobj = bus.get_proxy_object("org.bluez", batt_path, bintro)
            bchar = bobj.get_interface("org.bluez.GattCharacteristic1")
            val = await bchar.call_read_value({})
            global LAST_BATTERY
            LAST_BATTERY = int(bytes(val)[0])
            log(f"  Battery: {LAST_BATTERY}%")
            push({"battery": LAST_BATTERY})
        except Exception as e:
            log(f"  Battery: {e}")

    log("  Notifications ON — monitoring (Ctrl+C to stop)")
    log("")
    log(
        f"{'Timestamp':<19}  |  {'Elapsed':>8}  |  "
        f"{'°C':>7}  |  {'°F':>7}  |  raw"
    )
    log("-" * 80)

    while await is_connected(bus, device_path):
        await asyncio.sleep(1)

    log("\nDevice disconnected.")
    return True


async def scan_bleak(mac: str) -> BLEDevice | None:
    mac = mac.upper()
    found = None

    def on_detect(device: BLEDevice, adv: AdvertisementData):
        nonlocal found
        if device.address.upper() == mac:
            found = device
            name = device.name or adv.local_name or "Unknown"
            log(f"  ✓ Found: {name}  [{device.address}]  RSSI: {adv.rssi} dBm")

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    try:
        end = time.monotonic() + SCAN_TIMEOUT
        while time.monotonic() < end and found is None:
            await asyncio.sleep(0.4)
    finally:
        await scanner.stop()
    return found


async def main(mac: str):
    global CURRENT_MAC
    mac = mac.upper().replace("-", ":")
    CURRENT_MAC = mac
    log("=" * 64)
    log("BrewVision collector – push readings to CraftBeerPi")
    log("=" * 64)
    log(f"MAC:              {mac}")
    log(f"CBPi listener:    {PUSH_HOST}:{PUSH_PORT}")
    log("Device profile:   DisplayOnly + MITM + LE Legacy only (no SC)")
    log(f"Passkey/PIN:      {TARGET_PIN}")
    log(f"Agent capability: {AGENT_CAPABILITY}")
    log(f"Pair timeout:     {PAIR_TIMEOUT:.0f}s (then reconnect immediately)")
    log("")

    force_legacy_pairing()

    bus = None
    agent_manager = None

    try:
        log("[1] System D-Bus ...")
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        log("    OK")

        log("[2] Adapter Pairable/Powered ...")
        await setup_adapter(bus)

        log("[3] Remove prior bond ...")
        await remove_device(bus, mac)

        log("[4] Register passkey agent ...")
        agent_manager = await register_agent(bus)

        log("[5] LE scan ...")
        device = await scan_bleak(mac)
        path = await find_device_path(bus, mac)
        if not path:
            log("  BlueZ has no object yet — discovering ...")
            path = await ensure_bluez_object(bus, mac)
        if not path and device is None:
            log("Device not found. Power-cycle BrewVision and rerun while the LED flashes blue.")
            return
        if path:
            log(f"  Device path: {path}")

        if path:
            log("[6] Attempt Legacy + passkey bond (optional) ...")
            paired = await pair_legacy_passkey(bus, path)
            if paired:
                log("  ✓ Bond succeeded")
            else:
                log("  ✗ Bond failed — continuing without bond")

        log("")
        path = await reconnect_fast(bus, mac, path)
        if not path:
            return

        log("[NOTIFY] ffa4 via BlueZ on the live connection ...")
        ok = await monitor_via_bluez(bus, path)
        if not ok:
            log("  BlueZ notify did not start. Reconnecting once more ...")
            path = await reconnect_fast(bus, mac, path)
            if path:
                await monitor_via_bluez(bus, path)

    except Exception as e:
        log(f"\nFATAL: {type(e).__name__}: {e}")
    finally:
        if bus and agent_manager is not None:
            await unregister_agent(bus, agent_manager)
        if bus:
            try:
                bus.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAC
    if len(sys.argv) > 2:
        PUSH_HOST = sys.argv[2]
    if len(sys.argv) > 3:
        PUSH_PORT = int(sys.argv[3])
    try:
        asyncio.run(main(target))
    except KeyboardInterrupt:
        log("\nStopped by user.")
