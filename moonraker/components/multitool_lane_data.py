#!/usr/bin/env python3
# Moonraker component for OrcaSlicer filament sync.
#
# OrcaSlicer reads AMS-style lane metadata from:
#   /server/database/item?namespace=lane_data
# This component receives tool/spool state from the Klipper multitool module,
# enriches it with Spoolman filament metadata when available, and publishes one
# database item per tool channel.

import logging
import re

LANE_NAMESPACE = "lane_data"
REMOTE_METHOD = "multitool_lane_data_update"


class MultitoolLaneData:
    def __init__(self, config):
        self.server = config.get_server()
        self.database = self.server.lookup_component("database")
        self.http_client = self._lookup_component("http_client")
        self.klippy_apis = self._lookup_component("klippy_apis")
        self.server.register_remote_method(REMOTE_METHOD, self.update_lane_data)

    def update_lane_data(self, tool_count=0, spool_ids=None, loaded=None,
                         current_tool=-1, rgb_enabled=False):
        eventloop = self.server.get_event_loop()
        eventloop.create_task(self._update_lane_data(
            tool_count, spool_ids, loaded, current_tool, rgb_enabled))

    async def _update_lane_data(self, tool_count, spool_ids, loaded,
                                current_tool, rgb_enabled):
        try:
            tool_count = self._safe_int(tool_count, 0)
            spool_ids = spool_ids if isinstance(spool_ids, list) else []
            loaded = loaded if isinstance(loaded, list) else []
            rgb_enabled = bool(rgb_enabled)
            for lane in range(tool_count):
                spool_id = self._list_int(spool_ids, lane, 0)
                is_loaded = self._list_value(loaded, lane, None)
                if is_loaded is False:
                    spool_id = 0
                data = await self._lane_payload(lane, spool_id)
                await self.database.insert_item(
                    LANE_NAMESPACE, "lane%d" % lane, data)
                if rgb_enabled:
                    await self._sync_rgb_color(lane, data.get("color"))
        except Exception:
            logging.exception(
                "multitool_lane_data: failed to update lane_data")

    async def _lane_payload(self, lane, spool_id):
        payload = self._empty_lane(lane)
        if spool_id <= 0:
            return payload

        payload["spool_id"] = spool_id
        spool = await self._fetch_spool(spool_id)
        if not isinstance(spool, dict):
            return payload

        filament = spool.get("filament") or {}
        if not isinstance(filament, dict):
            filament = {}

        payload["material"] = self._string_value(
            filament.get("material") or filament.get("type"))
        payload["color"] = self._color_value(
            filament.get("color_hex") or filament.get("color"))
        payload["nozzle_temp"] = self._int_or_empty(
            filament.get("nozzle_temperature")
            or filament.get("nozzle_temp")
            or filament.get("extruder_temp"))
        payload["bed_temp"] = self._int_or_empty(
            filament.get("bed_temperature")
            or filament.get("bed_temp"))
        return payload

    def _empty_lane(self, lane):
        return {
            "color": "",
            "material": "",
            "bed_temp": "",
            "nozzle_temp": "",
            "scan_time": "",
            "lane": str(lane),
            "spool_id": None,
        }

    async def _fetch_spool(self, spool_id):
        spoolman = self._lookup_component("spoolman")
        if spoolman is None:
            return None

        if hasattr(spoolman, "get_spool"):
            try:
                result = spoolman.get_spool(spool_id)
                if hasattr(result, "__await__"):
                    result = await result
                return result
            except Exception:
                logging.exception(
                    "multitool_lane_data: failed to query Spoolman component")
                return None

        if self.http_client is None:
            return None
        base_url = getattr(spoolman, "spoolman_url", None)
        if not base_url:
            return None
        url = "%s/v1/spool/%d" % (str(base_url).rstrip("/"), spool_id)
        try:
            response = await self.http_client.get(url)
            return response.json()
        except Exception:
            logging.exception(
                "multitool_lane_data: failed to fetch Spoolman spool %d",
                spool_id)
        return None

    async def _sync_rgb_color(self, lane, color):
        if self.klippy_apis is None:
            self.klippy_apis = self._lookup_component("klippy_apis")
            if self.klippy_apis is None:
                return
        if color:
            script = (
                "SET_MULTITOOL_RGB_COLOR TOOL=%d COLOR=%s SOURCE=spoolman"
                % (lane, color))
        else:
            script = (
                "SET_MULTITOOL_RGB_COLOR TOOL=%d SOURCE=spoolman CLEAR=1"
                % lane)
        try:
            result = self.klippy_apis.run_gcode(script)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logging.debug(
                "multitool_lane_data: unable to sync RGB color to Klipper",
                exc_info=True)

    def _safe_int(self, value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _lookup_component(self, name):
        try:
            return self.server.lookup_component(name, None)
        except TypeError:
            try:
                return self.server.lookup_component(name)
            except Exception:
                return None
        except Exception:
            return None

    def _list_int(self, values, index, default):
        if index >= len(values):
            return default
        return self._safe_int(values[index], default)

    def _list_value(self, values, index, default):
        if index >= len(values):
            return default
        return values[index]

    def _string_value(self, value):
        if value is None:
            return ""
        return str(value)

    def _int_or_empty(self, value):
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return ""

    def _color_value(self, value):
        if value is None:
            return ""
        value = str(value).strip()
        if not value or value.lower() == "#none":
            return ""
        digits = re.sub(r"[^0-9a-fA-F]", "", value)
        if len(digits) < 6:
            return ""
        return "#%s" % digits[:6].lower()


def load_component(config):
    return MultitoolLaneData(config)
