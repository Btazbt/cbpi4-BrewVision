#!/usr/bin/env python3
"""
Standalone test script for Blichmann BrewVision thermometer.
Run this on your Raspberry Pi to verify temperature reading before using the CBPi plugin.

Usage:
    python3 test_brewvision.py [MAC]
    python3 test_brewvision.py D0:B5:C2:AE:6B:3A
"""

import asyncio
import sys
from bleak import BleakClient

TEMP_CHAR_UUID = "0000ffa4-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

DEFAULT_MAC = "D0:B5:C2:AE:6B:3A"


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def notification_handler(sender, data: bytearray):
    if data and len(data) >= 2:
        temp_c = data[0] + data[1] / 10.0
        temp_f = c_to_f(temp_c)
        print(f"  Notification → {temp_c:.1f} °C  /  {temp_f:.1f} °F   (raw: {data.hex()})")


async def main(mac: str):
    print(f"Connecting to BrewVision ({mac}) ...")
    print("(Make sure the device is powered on)")

    async with BleakClient(mac, timeout=20.0) as client:
        print("Connected!")

        # Read battery
        try:
            batt = await client.read_gatt_char(BATTERY_CHAR_UUID)
            print(f"Battery: {batt[0]}%")
        except Exception as e:
            print(f"Battery read failed: {e}")

        # Enable notifications
        await client.start_notify(TEMP_CHAR_UUID, notification_handler)
        print("Listening for temperature notifications (Ctrl+C to stop)...\n")

        # Also do an initial read
        try:
            data = await client.read_gatt_char(TEMP_CHAR_UUID)
            if data and len(data) >= 2:
                temp_c = data[0] + data[1] / 10.0
                print(f"  Initial read → {temp_c:.1f} °C  /  {c_to_f(temp_c):.1f} °F   (raw: {data.hex()})")
        except Exception as e:
            print(f"Initial read failed: {e}")

        # Keep running
        while True:
            await asyncio.sleep(1)


if __name__ == "__main__":
    mac = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAC
    try:
        asyncio.run(main(mac))
    except KeyboardInterrupt:
        print("\nStopped.")
