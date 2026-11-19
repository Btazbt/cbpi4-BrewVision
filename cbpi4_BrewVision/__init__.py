# -*- coding: utf-8 -*-
"""
CraftBeerPi listener for BrewVision.

This plugin does not open Bluetooth. brewvision_collector.py owns the
thermometer and pushes one JSON object per line:

    {"mac":"D0:B5:C2:AE:6B:3A","temp_c":25.4,"battery":86}

Default listen address is 127.0.0.1:5599.
"""
import asyncio
import json
import logging

from cbpi.api import *
from cbpi.api.base import CBPiBase

logger = logging.getLogger(__name__)

# One TCP server per (host, port). Sensors subscribe to it.
_SERVERS = {}


class _Hub:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.server = None
        self.subs = []
        self.starting = None

    async def ensure(self):
        if self.server is not None:
            return
        if self.starting is not None:
            await self.starting
            return
        self.starting = asyncio.get_event_loop().create_future()
        try:
            self.server = await asyncio.start_server(
                self._client, self.host, self.port
            )
            logger.info("BrewVision listening on %s:%s", self.host, self.port)
            self.starting.set_result(True)
        except Exception as e:
            logger.error("BrewVision listen failed %s:%s: %s", self.host, self.port, e)
            self.starting.set_result(False)
            self.starting = None
            raise

    def subscribe(self, fn):
        self.subs.append(fn)

    def unsubscribe(self, fn):
        if fn in self.subs:
            self.subs.remove(fn)

    async def _client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        logger.info("BrewVision collector connected %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode().strip())
                except Exception:
                    continue
                for fn in list(self.subs):
                    try:
                        await fn(msg)
                    except Exception as e:
                        logger.warning("BrewVision subscriber: %s", e)
        except Exception as e:
            logger.info("BrewVision collector closed: %s", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("BrewVision collector disconnected")


def _hub(host, port):
    key = (host, int(port))
    if key not in _SERVERS:
        _SERVERS[key] = _Hub(*key)
    return _SERVERS[key]


@parameters([
    Property.Text(label="MAC Address", configurable=True, description="Only accept this thermometer. Blank accepts any."),
    Property.Text(label="Listen Host", configurable=True, default_value="127.0.0.1", description="Address the collector connects to"),
    Property.Number(label="Listen Port", configurable=True, default_value=5599, description="Port the collector connects to"),
    Property.Select(label="Unit", options=["F", "C"], description="Temperature unit (default F)"),
    Property.Number(label="Offset", configurable=True, default_value=0, description="Added after unit conversion"),
    Property.Number(label="Interval", configurable=True, default_value=2, description="Minimum seconds between CBPi updates"),
])
class BrewVisionSensor(CBPiSensor):

    def __init__(self, cbpi, id, props):
        super(BrewVisionSensor, self).__init__(cbpi, id, props)
        self.value = 0.0
        self.mac = str(self.props.get("MAC Address", "") or "").upper().replace("-", ":")
        self.host = str(self.props.get("Listen Host", "127.0.0.1") or "127.0.0.1")
        self.port = int(float(self.props.get("Listen Port", 5599) or 5599))
        self.unit = self.props.get("Unit", "F") or "F"
        self.offset = float(self.props.get("Offset", 0) or 0)
        self.interval = float(self.props.get("Interval", 2) or 2)
        self.running = False
        self._last_push = 0.0
        self._hub = None

    def get_unit(self):
        return "°F" if self.unit == "F" else "°C"

    def _convert(self, temp_c):
        if self.unit == "F":
            return round(temp_c * 9.0 / 5.0 + 32.0 + self.offset, 2)
        return round(temp_c + self.offset, 2)

    async def _on_msg(self, msg):
        mac = str(msg.get("mac", "")).upper()
        if self.mac and mac and mac != self.mac:
            return
        if "temp_c" not in msg:
            return
        self.value = self._convert(float(msg["temp_c"]))
        now = asyncio.get_event_loop().time()
        if now - self._last_push < self.interval:
            return
        self._last_push = now
        logger.info("BrewVision %s -> %s %s", mac or self.mac, self.value, self.get_unit())
        await self.push_update(self.value)

    async def start(self):
        self.running = True
        self._hub = _hub(self.host, self.port)
        self._hub.subscribe(self._on_msg)
        try:
            await self._hub.ensure()
        except Exception:
            logger.error("BrewVision sensor cannot listen. Is the port already in use?")

    async def stop(self):
        self.running = False
        if self._hub:
            self._hub.unsubscribe(self._on_msg)

    def get_state(self):
        return dict(value=self.value)


@parameters([
    Property.Text(label="MAC Address", configurable=True, description="Only accept this thermometer. Blank accepts any."),
    Property.Text(label="Listen Host", configurable=True, default_value="127.0.0.1"),
    Property.Number(label="Listen Port", configurable=True, default_value=5599),
    Property.Number(label="Interval", configurable=True, default_value=30, description="Minimum seconds between battery updates"),
])
class BrewVisionBattery(CBPiSensor):

    def __init__(self, cbpi, id, props):
        super(BrewVisionBattery, self).__init__(cbpi, id, props)
        self.value = 0
        self.mac = str(self.props.get("MAC Address", "") or "").upper().replace("-", ":")
        self.host = str(self.props.get("Listen Host", "127.0.0.1") or "127.0.0.1")
        self.port = int(float(self.props.get("Listen Port", 5599) or 5599))
        self.interval = float(self.props.get("Interval", 30) or 30)
        self._last_push = 0.0
        self._hub = None

    def get_unit(self):
        return "%"

    async def _on_msg(self, msg):
        mac = str(msg.get("mac", "")).upper()
        if self.mac and mac and mac != self.mac:
            return
        if "battery" not in msg:
            return
        val = int(msg["battery"])
        now = asyncio.get_event_loop().time()
        if val == self.value and now - self._last_push < self.interval:
            return
        if now - self._last_push < self.interval and self.value:
            return
        self.value = val
        self._last_push = now
        await self.push_update(self.value)

    async def start(self):
        self._hub = _hub(self.host, self.port)
        self._hub.subscribe(self._on_msg)
        try:
            await self._hub.ensure()
        except Exception:
            logger.error("BrewVision battery cannot listen. Is the port already in use?")

    async def stop(self):
        if self._hub:
            self._hub.unsubscribe(self._on_msg)

    def get_state(self):
        return dict(value=self.value)


def setup(cbpi: CBPiBase):
    cbpi.plugin.register("BrewVision", BrewVisionSensor)
    cbpi.plugin.register("BrewVision Battery", BrewVisionBattery)
