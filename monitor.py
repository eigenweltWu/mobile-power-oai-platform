#!/usr/bin/env python3
"""Redmi/Android power and radio monitor.

The collector intentionally uses only the Python standard library. Android
measurements are read through ADB and exposed to the browser as Server-Sent
Events (SSE).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import mimetypes
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
INVALID_ANDROID_VALUES = {2147483647, -2147483648, 9223372036854775807}

BATTERY_STATUS = {
    1: "未知",
    2: "充电中",
    3: "放电中",
    4: "未充电",
    5: "已充满",
}

SHELL_SCRIPT = r"""
echo __MON_BATTERY__
dumpsys battery 2>/dev/null
echo __MON_CURRENT__
service call batteryproperties 1 i32 2 2>/dev/null
echo __MON_WIFI__
cmd wifi status 2>/dev/null || dumpsys wifi 2>/dev/null
echo __MON_TELEPHONY__
dumpsys telephony.registry 2>/dev/null
echo __MON_LOCATION__
dumpsys location 2>/dev/null
echo __MON_NETDEV__
cat /proc/net/dev 2>/dev/null
echo __MON_END__
"""


def finite_android_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed in INVALID_ANDROID_VALUES or abs(parsed) > 1_000_000_000:
        return None
    return parsed


def match_int(pattern: str, text: str, flags: int = re.I) -> int | None:
    match = re.search(pattern, text, flags)
    return finite_android_int(match.group(1)) if match else None


def match_float(pattern: str, text: str, flags: int = re.I) -> float | None:
    match = re.search(pattern, text, flags)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def split_sections(raw: str) -> dict[str, str]:
    parts = re.split(r"^__MON_([A-Z]+)__\s*$", raw, flags=re.M)
    return {
        parts[index]: parts[index + 1].strip()
        for index in range(1, len(parts) - 1, 2)
        if parts[index] != "END"
    }


class AdbError(RuntimeError):
    pass


class AdbClient:
    def __init__(self, adb_path: str | None = None, serial: str | None = None):
        self.adb = self._find_adb(adb_path)
        self.serial = serial
        self.device_info: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _find_adb(explicit: str | None) -> str:
        candidates = [
            explicit,
            os.environ.get("ANDROID_ADB"),
            shutil.which("adb"),
            str(ROOT / ".tools" / "platform-tools" / "adb.exe"),
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        raise AdbError(
            "未找到 ADB。请运行 start.ps1 自动准备，或用 --adb 指定 adb.exe。"
        )

    def _run(self, arguments: list[str], timeout: float = 15.0) -> str:
        command = [self.adb]
        if self.serial:
            command += ["-s", self.serial]
        command += arguments
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB 命令超时（{timeout:g} 秒）") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise AdbError(detail or f"ADB 返回代码 {result.returncode}")
        return result.stdout.replace("\r\n", "\n")

    def _run_host(self, arguments: list[str], timeout: float = 15.0) -> str:
        """Run an adb host command without selecting the current transport."""
        try:
            result = subprocess.run(
                [self.adb, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB 主机命令超时（{timeout:g} 秒）") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise AdbError(detail or f"ADB 返回代码 {result.returncode}")
        return result.stdout.replace("\r\n", "\n")

    def ensure_device(self) -> None:
        with self._lock:
            output = self._run(["devices", "-l"], timeout=8)
            devices: list[dict[str, str]] = []
            for line in output.splitlines()[1:]:
                if not line.strip():
                    continue
                columns = line.split()
                if len(columns) < 2 or columns[1] != "device":
                    continue
                record = {"serial": columns[0]}
                for column in columns[2:]:
                    if ":" in column:
                        key, value = column.split(":", 1)
                        record[key] = value
                devices.append(record)
            if self.serial:
                found = next((item for item in devices if item["serial"] == self.serial), None)
                if not found:
                    raise AdbError(f"设备 {self.serial} 未连接或未授权")
            elif len(devices) == 1:
                found = devices[0]
                self.serial = found["serial"]
            elif len(devices) > 1:
                usb_devices = [
                    item
                    for item in devices
                    if not re.search(r":\d+$", item["serial"])
                ]
                if len(usb_devices) == 1:
                    # adb-over-Wi-Fi and USB often expose the same phone twice.
                    # Prefer the sole USB transport so the dashboard can perform
                    # an explicit, verified handoff before the cable is removed.
                    found = usb_devices[0]
                    self.serial = found["serial"]
                else:
                    names = ", ".join(item["serial"] for item in devices)
                    raise AdbError(f"发现多台设备，请用 --serial 指定：{names}")
            elif not devices:
                raise AdbError("未发现已授权的 Android 设备")
            else:
                raise AdbError("未发现可用的 Android 设备")
            self.device_info = {
                "serial": found.get("serial", ""),
                "model": found.get("model", "Android"),
                "product": found.get("product", ""),
                "device": found.get("device", ""),
            }

    def collect(self) -> str:
        if not self.serial:
            self.ensure_device()
        try:
            return self._run(["shell", SHELL_SCRIPT], timeout=20)
        except AdbError:
            self.ensure_device()
            return self._run(["shell", SHELL_SCRIPT], timeout=20)

    def shell_command(self, command: str, timeout: float = 20.0) -> str:
        if not self.serial:
            self.ensure_device()
        return self._run(["shell", command], timeout=timeout)

    @property
    def wireless(self) -> bool:
        return bool(self.serial and re.search(r":\d+$", self.serial))

    def enable_wireless(self, port: int = 5555) -> dict[str, str]:
        """Move this monitor to adb-over-Wi-Fi before the USB cable is removed."""
        if not self.serial:
            self.ensure_device()
        address_output = self.shell_command(
            "ip -4 addr show dev wlan0 2>/dev/null", timeout=10
        )
        match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", address_output)
        if not match:
            raise AdbError("手机未连接 Wi-Fi，无法建立无线 ADB 控制链路")
        endpoint = f"{match.group(1)}:{port}"
        if self.serial == endpoint:
            return {"endpoint": endpoint, "message": "无线 ADB 已连接"}

        original_serial = self.serial
        self._run(["tcpip", str(port)], timeout=15)
        deadline = time.monotonic() + 12
        last_message = ""
        while time.monotonic() < deadline:
            try:
                last_message = self._run_host(
                    ["connect", endpoint], timeout=5
                ).strip()
                devices = self._run_host(["devices"], timeout=5)
                if re.search(
                    rf"^{re.escape(endpoint)}\s+device\b", devices, re.M
                ):
                    self.serial = endpoint
                    self.device_info["serial"] = endpoint
                    return {
                        "endpoint": endpoint,
                        "message": "无线 ADB 已连接；现在可以拔掉 USB 数据线",
                    }
            except AdbError as exc:
                last_message = str(exc)
            time.sleep(0.5)
        self.serial = original_serial
        raise AdbError(
            f"无线 ADB 连接失败：{last_message or endpoint}。请保持手机与电脑在同一 Wi-Fi。"
        )


class MeasurementParser:
    def __init__(self):
        self.previous_counters: dict[str, tuple[int, int]] = {}
        self.previous_counter_time: float | None = None

    @staticmethod
    def parse_current(parcel: str) -> int | None:
        words: list[int] = []
        for line in parcel.splitlines():
            match = re.search(
                r"0x[0-9a-f]+:\s+((?:[0-9a-f]{8}(?:\s+|$))+)", line, re.I
            )
            if match:
                words.extend(int(item, 16) for item in re.findall(r"[0-9a-f]{8}", match.group(1), re.I))
        if len(words) < 5 or words[2] != 1:
            return None
        unsigned = (words[4] << 32) | words[3]
        signed = unsigned - (1 << 64) if unsigned & (1 << 63) else unsigned
        return finite_android_int(signed)

    @staticmethod
    def parse_battery(text: str, current_text: str) -> dict[str, Any]:
        status_code = match_int(r"^\s*status:\s*(\d+)", text, re.M)
        voltage_mv = match_int(r"^\s*voltage:\s*(\d+)", text, re.M)
        raw_current_ua = MeasurementParser.parse_current(current_text)
        raw_current_ma = raw_current_ua / 1000 if raw_current_ua is not None else None

        current_ma = None
        direction = "未知"
        if raw_current_ma is not None:
            if status_code in (2, 5):
                current_ma = abs(raw_current_ma)
                direction = "充电"
            elif status_code in (3, 4):
                current_ma = -abs(raw_current_ma)
                direction = "放电"
            else:
                current_ma = raw_current_ma
        voltage_v = voltage_mv / 1000 if voltage_mv is not None else None
        power_w = (
            abs(current_ma) * voltage_v / 1000
            if current_ma is not None and voltage_v is not None
            else None
        )
        temperature = match_int(r"^\s*temperature:\s*(-?\d+)", text, re.M)
        return {
            "current_ma": current_ma,
            "raw_current_ma": raw_current_ma,
            "current_direction": direction,
            "current_source": "BatteryProperties CURRENT_NOW" if raw_current_ma is not None else None,
            "voltage_v": voltage_v,
            "power_w": power_w,
            "level_percent": match_int(r"^\s*level:\s*(\d+)", text, re.M),
            "temperature_c": temperature / 10 if temperature is not None else None,
            "status_code": status_code,
            "status": BATTERY_STATUS.get(status_code, "未知"),
            "usb_powered": bool(re.search(r"USB powered:\s*true", text, re.I)),
            "ac_powered": bool(re.search(r"AC powered:\s*true", text, re.I)),
            "charge_counter_uah": match_int(r"Charge counter:\s*(-?\d+)", text),
        }

    @staticmethod
    def parse_wifi(text: str) -> dict[str, Any]:
        enabled = not bool(re.search(r"Wifi is disabled|Wi-Fi is disabled", text, re.I))
        rssi = match_int(r"\bRSSI\s*[:=]\s*(-?\d+)", text)
        if rssi is None:
            rssi = match_int(r"\brssi[=:]\s*(-?\d+)", text)
        connected = enabled and rssi is not None

        ssid = None
        ssid_match = re.search(r"\bSSID:\s*([^,\n]+)", text)
        if ssid_match:
            candidate = ssid_match.group(1).strip().strip('"')
            if candidate not in ("<unknown ssid>", "null", "none"):
                ssid = candidate

        link = match_float(r"(?<!Tx )(?<!Rx )Link speed:\s*([\d.]+)\s*Mbps", text)
        tx_link = match_float(r"Tx Link speed:\s*([\d.]+)\s*Mbps", text)
        rx_link = match_float(r"Rx Link speed:\s*([\d.]+)\s*Mbps", text)
        return {
            "enabled": enabled,
            "connected": connected,
            "ssid": ssid,
            "rssi_dbm": rssi,
            "frequency_mhz": match_int(r"Frequency:\s*(\d+)\s*MHz", text),
            "link_mbps": link,
            "tx_link_mbps": tx_link if tx_link is not None else link,
            "rx_link_mbps": rx_link if rx_link is not None else link,
            "standard": (re.search(r"Wi-Fi standard:\s*([^,\n]+)", text) or [None, None])[1],
            "state": "已连接" if connected else ("已开启，未连接" if enabled else "已关闭"),
        }

    @staticmethod
    def _extract_signal(section: str, start: str, end: str | None = None) -> str:
        ending = re.escape(end) if end else r"$"
        match = re.search(re.escape(start) + r"(.*?)(?=" + ending + r")", section, re.S)
        return match.group(1) if match else ""

    @staticmethod
    def parse_cellular(text: str) -> dict[str, Any]:
        starts = list(re.finditer(r"^\s*mServiceState=", text, re.M))
        records: list[dict[str, Any]] = []
        for index, start in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            section = text[start.start():end]
            if "mSignalStrength=" not in section:
                continue

            data_state = match_int(r"mDataConnectionState=(\d+)", section)
            data_reg = match_int(r"mDataRegState=(\d+)", section)
            voice_reg = match_int(r"mVoiceRegState=(\d+)", section)
            connected = data_state == 2
            registered = data_reg == 0 or "registrationState=HOME" in section

            technology_match = re.search(
                r"getRilDataRadioTechnology=\d+\(([^)]+)\)", section
            )
            technology = technology_match.group(1) if technology_match else "Unknown"

            lte = MeasurementParser._extract_signal(
                section, "mLte=CellSignalStrengthLte:", ",mNr="
            )
            nr = MeasurementParser._extract_signal(
                section, "mNr=CellSignalStrengthNr:", "},primary="
            )
            gsm = MeasurementParser._extract_signal(
                section, "mGsm=CellSignalStrengthGsm:", ",mWcdma="
            )
            wcdma = MeasurementParser._extract_signal(
                section, "mWcdma=CellSignalStrengthWcdma:", ",mTdscdma="
            )

            lte_rssi = match_int(r"\brssi=(-?\d+)", lte)
            lte_rsrp = match_int(r"\brsrp=(-?\d+)", lte)
            lte_rsrq = match_int(r"\brsrq=(-?\d+)", lte)
            lte_rssnr = match_int(r"\brssnr=(-?\d+)", lte)
            nr_rsrp = match_int(r"\bssRsrp\s*=\s*(-?\d+)", nr)
            nr_rsrq = match_int(r"\bssRsrq\s*=\s*(-?\d+)", nr)
            nr_sinr = match_int(r"\bssSinr\s*=\s*(-?\d+)", nr)
            gsm_rssi = match_int(r"\brssi=(-?\d+)", gsm)
            wcdma_rscp = match_int(r"\brscp=(-?\d+)", wcdma)
            nr_available = bool(re.search(r"isNrAvailable\s*=\s*true", section, re.I))
            endc_available = bool(re.search(r"isEnDcAvailable\s*=\s*true", section, re.I))

            if nr_rsrp is not None or technology.upper() == "NR":
                radio = "5G"
                rssi = None
                rsrp, rsrq, sinr = nr_rsrp, nr_rsrq, nr_sinr
                level = match_int(r"\blevel\s*=\s*(\d+)", nr)
            elif lte_rsrp is not None or technology.upper() in ("LTE", "LTE_CA"):
                radio = "4G"
                rssi = lte_rssi
                rsrp, rsrq = lte_rsrp, lte_rsrq
                sinr = lte_rssnr / 10 if lte_rssnr is not None else None
                level = match_int(r"\blevel=(\d+)", lte)
            elif gsm_rssi is not None or technology.upper() in ("GSM", "GPRS", "EDGE"):
                radio = "2G"
                rssi, rsrp, rsrq, sinr = gsm_rssi, None, None, None
                level = match_int(r"(?:mLevel|level)=(\d+)", gsm)
            elif wcdma_rscp is not None or technology.upper() in ("UMTS", "HSPA", "HSPAP"):
                radio = "3G"
                rssi, rsrp, rsrq, sinr = None, wcdma_rscp, None, None
                level = match_int(r"\blevel=(\d+)", wcdma)
            else:
                radio = "无"
                rssi = rsrp = rsrq = sinr = level = None

            score = (
                int(connected) * 100
                + int(registered) * 50
                + int(radio in ("4G", "5G")) * 20
                + int(any(value is not None for value in (rssi, rsrp))) * 10
            )
            records.append(
                {
                    "registered": registered,
                    "connected": connected,
                    "data_state_code": data_state,
                    "data_reg_code": data_reg,
                    "voice_reg_code": voice_reg,
                    "network_type": technology,
                    "radio": radio,
                    "rssi_dbm": rssi,
                    "rsrp_dbm": rsrp,
                    "rsrq_db": rsrq,
                    "sinr_db": sinr,
                    "level": level,
                    "nr_available": nr_available,
                    "endc_available": endc_available,
                    "_score": score,
                }
            )

        if records:
            chosen = max(records, key=lambda item: item["_score"])
            chosen.pop("_score", None)
        else:
            chosen = {
                "registered": False,
                "connected": False,
                "data_state_code": None,
                "data_reg_code": None,
                "voice_reg_code": None,
                "network_type": "Unknown",
                "radio": "无",
                "rssi_dbm": None,
                "rsrp_dbm": None,
                "rsrq_db": None,
                "sinr_db": None,
                "level": None,
                "nr_available": False,
                "endc_available": False,
            }

        # The dashboard is specifically a 4G/5G monitor. An unregistered SIM can
        # still report a GSM emergency-scan RSSI; keep it for diagnostics without
        # presenting it as the requested LTE/NR signal.
        if not chosen["registered"] and chosen["radio"] not in ("4G", "5G"):
            chosen["observed_legacy_rssi_dbm"] = chosen["rssi_dbm"]
            chosen["rssi_dbm"] = None
            chosen["radio"] = "无"

        if chosen["radio"] in ("4G", "5G") and chosen["connected"]:
            state = f'{chosen["radio"]} 数据已连接'
        elif chosen["radio"] in ("4G", "5G") and chosen["registered"]:
            state = f'{chosen["radio"]} 已驻留'
        elif chosen["registered"]:
            state = f'{chosen["radio"]} 驻留，4G/5G 未工作'
        else:
            state = "未注册 / 无信号"
        chosen["state"] = state
        chosen["subscriptions_seen"] = len(records)
        return chosen

    @staticmethod
    def parse_gps(text: str) -> dict[str, Any]:
        provider_match = re.search(
            r"^\s*gps provider:\s*(.*?)(?=^\s{4}\S.*provider:|^\s{2}\S|\Z)",
            text,
            re.M | re.S | re.I,
        )
        provider = provider_match.group(1) if provider_match else text
        enabled_match = re.search(r"^\s*enabled=(true|false)", provider, re.M | re.I)
        enabled = (
            enabled_match.group(1).lower() == "true"
            if enabled_match
            else "gps provider" in text.lower()
        )
        active = bool(
            re.search(r"service:\s*ProviderRequest\[(?!OFF)", provider, re.I)
            or re.search(r"mStarted=true", provider, re.I)
        )
        last_location = re.search(r"last location=(.+)", provider, re.I)
        has_fix = bool(
            last_location
            and last_location.group(1).strip().lower() not in ("null", "none")
        )

        cn0_values: list[float] = []
        arrays = re.findall(r"(?:m?Cn0s|cn0s)\s*=\s*\[([^\]]+)\]", text, re.I)
        if arrays:
            candidates = re.findall(r"[\d.]+", arrays[-1])
        else:
            # Some Android releases print one GnssStatus record per satellite.
            # Use the last status-sized group rather than accumulated history.
            candidates = re.findall(r"\bcn0DbHz\s*[=:]\s*([\d.]+)", text, re.I)[-64:]
        for value in candidates:
            number = float(value)
            if 0 <= number <= 99:
                cn0_values.append(number)
        visible = match_int(r"\bmSvCount\s*=\s*(\d+)", text)
        if visible is None and cn0_values:
            visible = len(cn0_values)
        used = len(re.findall(r"usedInFix\s*=\s*true", text, re.I)) or None
        ordered = sorted(cn0_values, reverse=True)
        top4 = sum(ordered[:4]) / len(ordered[:4]) if ordered else None
        average = sum(cn0_values) / len(cn0_values) if cn0_values else None
        return {
            "enabled": enabled,
            "active": active,
            "has_fix": has_fix,
            "satellites_visible": visible,
            "satellites_used": used,
            "cn0_avg_dbhz": average,
            "cn0_top4_dbhz": top4,
            "cn0_max_dbhz": max(cn0_values) if cn0_values else None,
            "source": "dumpsys location / GNSS status",
            "state": (
                "已定位"
                if has_fix
                else ("正在搜星" if active else ("已开启，未工作" if enabled else "已关闭"))
            ),
            "signal_available": bool(cn0_values),
        }

    @staticmethod
    def parse_netdev(text: str) -> dict[str, tuple[int, int]]:
        counters: dict[str, tuple[int, int]] = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            name, values = line.split(":", 1)
            columns = values.split()
            if len(columns) >= 9 and columns[0].isdigit() and columns[8].isdigit():
                counters[name.strip()] = (int(columns[0]), int(columns[8]))
        return counters

    def calculate_rates(
        self, counters: dict[str, tuple[int, int]], now: float
    ) -> dict[str, dict[str, Any]]:
        elapsed = now - self.previous_counter_time if self.previous_counter_time else None

        def aggregate(patterns: tuple[str, ...]) -> tuple[int, int, list[str]]:
            matched = [
                name for name in counters
                if any(re.fullmatch(pattern, name) for pattern in patterns)
            ]
            return (
                sum(counters[name][0] for name in matched),
                sum(counters[name][1] for name in matched),
                matched,
            )

        wifi_rx, wifi_tx, wifi_interfaces = aggregate((r"wlan\d+",))
        cell_rx, cell_tx, cell_interfaces = aggregate(
            (r"rmnet_data\d+", r"r_rmnet_data\d+", r"ccmni\d+", r"pdp\d+")
        )

        def rate(
            key: str, rx: int, tx: int, interfaces: list[str]
        ) -> dict[str, Any]:
            previous = self.previous_counters.get(key)
            down = up = 0.0
            if previous and elapsed and elapsed > 0:
                down = max(0, rx - previous[0]) * 8 / elapsed / 1000
                up = max(0, tx - previous[1]) * 8 / elapsed / 1000
            self.previous_counters[key] = (rx, tx)
            return {
                "down_kbps": down,
                "up_kbps": up,
                "interfaces": interfaces,
            }

        result = {
            "wifi": rate("wifi", wifi_rx, wifi_tx, wifi_interfaces),
            "cellular": rate("cellular", cell_rx, cell_tx, cell_interfaces),
        }
        self.previous_counter_time = now
        return result

    def parse(self, raw: str, now: float) -> dict[str, Any]:
        sections = split_sections(raw)
        battery = self.parse_battery(
            sections.get("BATTERY", ""), sections.get("CURRENT", "")
        )
        wifi = self.parse_wifi(sections.get("WIFI", ""))
        cellular = self.parse_cellular(sections.get("TELEPHONY", ""))
        gps = self.parse_gps(sections.get("LOCATION", ""))
        counters = self.parse_netdev(sections.get("NETDEV", ""))
        rates = self.calculate_rates(counters, now)
        wifi.update(rates["wifi"])
        cellular.update(rates["cellular"])
        return {
            "battery": battery,
            "gps": gps,
            "cellular": cellular,
            "wifi": wifi,
        }


class MockCollector:
    device_info = {
        "serial": "MOCK-K30I",
        "model": "Redmi_K30i_5G",
        "product": "picasso_48m",
        "device": "picasso",
    }

    def __init__(self):
        self.started = time.monotonic()

    def sample(self) -> dict[str, Any]:
        t = time.monotonic() - self.started
        current = -(420 + 130 * math.sin(t / 4) + random.uniform(-22, 22))
        voltage = 3.86 - min(t / 30000, 0.08)
        gps_cn0 = max(0, 31 + 8 * math.sin(t / 7) + random.uniform(-2, 2))
        cell_rsrp = -93 + 11 * math.sin(t / 9) + random.uniform(-2, 2)
        wifi_rssi = -49 + 9 * math.sin(t / 6) + random.uniform(-2, 2)
        return {
            "battery": {
                "current_ma": current,
                "raw_current_ma": current,
                "current_direction": "放电",
                "current_source": "模拟数据",
                "voltage_v": voltage,
                "power_w": abs(current) * voltage / 1000,
                "level_percent": 72,
                "temperature_c": 30.2,
                "status_code": 3,
                "status": "放电中",
                "usb_powered": False,
                "ac_powered": False,
                "charge_counter_uah": 3112000,
            },
            "gps": {
                "enabled": True,
                "active": True,
                "has_fix": True,
                "satellites_visible": 17,
                "satellites_used": 10,
                "cn0_avg_dbhz": gps_cn0 - 5,
                "cn0_top4_dbhz": gps_cn0,
                "cn0_max_dbhz": gps_cn0 + 7,
                "source": "模拟数据",
                "state": "已定位",
                "signal_available": True,
            },
            "cellular": {
                "registered": True,
                "connected": True,
                "network_type": "NR",
                "radio": "5G",
                "rssi_dbm": None,
                "rsrp_dbm": cell_rsrp,
                "rsrq_db": -11,
                "sinr_db": 18,
                "level": 3,
                "nr_available": True,
                "endc_available": True,
                "state": "5G 数据已连接",
                "subscriptions_seen": 1,
                "down_kbps": 320 + random.random() * 900,
                "up_kbps": 60 + random.random() * 200,
                "interfaces": ["rmnet_data0"],
            },
            "wifi": {
                "enabled": True,
                "connected": True,
                "ssid": "Lab-5G",
                "rssi_dbm": wifi_rssi,
                "frequency_mhz": 5745,
                "link_mbps": 866,
                "tx_link_mbps": 780,
                "rx_link_mbps": 866,
                "standard": "11ac",
                "state": "已连接",
                "down_kbps": 540 + random.random() * 1800,
                "up_kbps": 100 + random.random() * 500,
                "interfaces": ["wlan0"],
            },
        }


GPSTEST_PACKAGE = "com.android.gpstest.osmdroid"
GPSTEST_COMPONENT = (
    "com.android.gpstest.osmdroid/com.android.gpstest.ui.MainActivity"
)
GPSTEST_APK_URL = (
    "https://github.com/barbeau/gpstest/releases/download/"
    "v3.10.5/osmdroidRelease-v3.10.5.apk"
)
GPSTEST_APK_SHA256 = (
    "440932994bf79eeca71c1aaa177c1368357c32071214d974d27541b20e0fd7bd"
)


class WorkloadController:
    """Starts and stops phone-side GNSS and interface-bound network loads."""

    def __init__(
        self,
        adb: AdbClient | None,
        latest_sample: Any,
        mock: bool,
        transfer_bytes: int,
        download_url: str,
        upload_url: str,
    ):
        self.adb = adb
        self.latest_sample = latest_sample
        self.mock = mock
        self.transfer_bytes = transfer_bytes
        self.download_url = download_url
        self.upload_url = upload_url
        self.config_path = ROOT / "config.json"
        if self.config_path.is_file():
            try:
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                saved_size = int(saved.get("transfer_size_mb", 0))
                saved_download = str(saved.get("download_url", ""))
                saved_upload = str(saved.get("upload_url", ""))
                if (
                    1 <= saved_size <= 100
                    and urlparse(saved_download.format(bytes=1)).scheme in ("http", "https")
                    and urlparse(saved_upload).scheme in ("http", "https")
                ):
                    self.transfer_bytes = saved_size * 1_000_000
                    self.download_url = saved_download
                    self.upload_url = saved_upload
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
        self.lock = threading.RLock()
        self.stop_events = {
            name: threading.Event() for name in ("gps", "wifi", "cellular")
        }
        self.threads: dict[str, threading.Thread | None] = {
            name: None for name in self.stop_events
        }
        self.gps_location_was_enabled = True
        self.cellular_wifi_was_enabled = False
        self.cellular_wifi_temporarily_disabled = False
        self.states: dict[str, dict[str, Any]] = {
            "gps": self._initial_state("持续 GNSS 搜星"),
            "wifi": self._initial_state("WLAN 双向数据负载"),
            "cellular": self._initial_state("4G/5G 双向数据负载"),
        }

    @staticmethod
    def _initial_state(label: str) -> dict[str, Any]:
        return {
            "label": label,
            "active": False,
            "status": "idle",
            "message": "未启动",
            "interface": None,
            "driver": None,
            "last_down_mbps": None,
            "last_up_mbps": None,
            "cn0_avg_dbhz": None,
            "cn0_top4_dbhz": None,
            "cn0_max_dbhz": None,
            "satellites_visible": None,
            "subscription_id": None,
            "sim_slot": None,
            "cycles": 0,
            "total_bytes": 0,
            "updated_at": None,
        }

    def _update(self, name: str, **values: Any) -> None:
        with self.lock:
            self.states[name].update(values)
            self.states[name]["updated_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            payload = json.loads(json.dumps(self.states, ensure_ascii=False))
            payload["config"] = {
                "transfer_size_mb": self.transfer_bytes / 1_000_000,
                "download_url": self.download_url,
                "upload_url": self.upload_url,
            }
            return payload

    def update_config(
        self, transfer_size_mb: Any, download_url: Any, upload_url: Any
    ) -> dict[str, Any]:
        try:
            size = int(transfer_size_mb)
        except (TypeError, ValueError) as exc:
            raise ValueError("每轮数据量必须是整数 MB") from exc
        if not 1 <= size <= 100:
            raise ValueError("每轮数据量必须在 1–100 MB 之间")
        download = str(download_url or "").strip()
        upload = str(upload_url or "").strip()
        try:
            download_test = download.format(bytes=size * 1_000_000)
        except (KeyError, ValueError) as exc:
            raise ValueError("下载 URL 中只有 {bytes} 是有效占位符") from exc
        for label, value in (("下载", download_test), ("上传", upload)):
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"{label} URL 必须使用 http 或 https")
        with self.lock:
            busy = any(
                self.states[name]["status"] in ("starting", "running", "stopping")
                for name in ("wifi", "cellular")
            )
            if busy:
                raise ValueError("请先停止 WLAN 和蜂窝测速，再修改服务器")
            self.transfer_bytes = size * 1_000_000
            self.download_url = download
            self.upload_url = upload
            saved = {
                "transfer_size_mb": size,
                "download_url": download,
                "upload_url": upload,
            }
            temporary = self.config_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(saved, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.config_path)
        return self.snapshot()

    def start(self, name: str) -> dict[str, Any]:
        if name not in self.states:
            raise ValueError("未知负载类型")
        if name == "cellular" and self.adb and self.adb.wireless:
            raise ValueError(
                "当前使用无线 ADB；蜂窝测速需要关闭 Wi-Fi，会中断监控。"
                "请改用可断供电的 USB 硬件、手机热点控制链路，或重新连接 USB。"
            )
        with self.lock:
            if self.states[name]["status"] in ("starting", "running"):
                return self.snapshot()
            self.stop_events[name].clear()
            self.states[name].update(
                active=True,
                status="starting",
                message="正在启动…",
                driver=None,
                last_down_mbps=None,
                last_up_mbps=None,
                cn0_avg_dbhz=None,
                cn0_top4_dbhz=None,
                cn0_max_dbhz=None,
                satellites_visible=None,
                cycles=0,
                total_bytes=0,
            )
        target = self._gps_start if name == "gps" else lambda: self._network_loop(name)
        thread = threading.Thread(
            target=target,
            name=f"workload-{name}",
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return self.snapshot()

    def stop(self, name: str) -> dict[str, Any]:
        if name not in self.states:
            raise ValueError("未知负载类型")
        self.stop_events[name].set()
        current = self.snapshot()[name]
        if current["status"] not in ("idle", "error"):
            self._update(name, status="stopping", message="正在停止…")
        if name == "gps":
            threading.Thread(
                target=self._gps_stop, name="workload-gps-stop", daemon=True
            ).start()
        elif current["status"] in ("idle", "error"):
            self._update(name, active=False, status="idle", message="已停止")
        return self.snapshot()

    def _gps_start(self) -> None:
        if self.mock:
            self._update(
                "gps", active=True, status="running", message="模拟持续搜星已启动"
            )
            return
        try:
            assert self.adb is not None
            location_state = self.adb.shell_command(
                "cmd location is-location-enabled", timeout=10
            ).strip().lower()
            self.gps_location_was_enabled = location_state == "true"
            if not self.gps_location_was_enabled:
                self.adb.shell_command(
                    "cmd location set-location-enabled true", timeout=10
                )

            if self._start_cit_gps():
                if self.stop_events["gps"].is_set():
                    self._gps_stop()
                    return
                self._update(
                    "gps",
                    active=True,
                    status="running",
                    driver="MIUI CIT",
                    message="手机内置 CIT GPS 测试已启动并持续搜星",
                )
                self._cit_metrics_loop()
                return

            package_path = self.adb.shell_command(
                f"pm path {GPSTEST_PACKAGE}", timeout=10
            ).strip()
            if not package_path.startswith("package:"):
                apk_path = ROOT / ".tools" / "gpstest-v3.10.5.apk"
                apk_path.parent.mkdir(exist_ok=True)
                if (
                    not apk_path.is_file()
                    or hashlib.sha256(apk_path.read_bytes()).hexdigest()
                    != GPSTEST_APK_SHA256
                ):
                    self._update(
                        "gps",
                        message="正在下载开源 GPSTest 辅助应用…",
                    )
                    urllib.request.urlretrieve(GPSTEST_APK_URL, apk_path)
                digest = hashlib.sha256(apk_path.read_bytes()).hexdigest()
                if digest != GPSTEST_APK_SHA256:
                    raise RuntimeError("GPSTest APK 校验失败，已取消安装")
                self._update("gps", message="正在安装 GPSTest 辅助应用…")
                result = self.adb._run(
                    ["install", "-r", str(apk_path)], timeout=120
                )
                if "Success" not in result:
                    raise RuntimeError(result.strip() or "GPSTest 安装失败")

            for permission in (
                "android.permission.ACCESS_COARSE_LOCATION",
                "android.permission.ACCESS_FINE_LOCATION",
                "android.permission.POST_NOTIFICATIONS",
            ):
                try:
                    self.adb.shell_command(
                        f"pm grant {GPSTEST_PACKAGE} {permission}", timeout=10
                    )
                except AdbError:
                    pass
            self.adb.shell_command(
                f"am start -W -n {GPSTEST_COMPONENT}", timeout=25
            )
            if self.stop_events["gps"].is_set():
                self._gps_stop()
                return
            self._update(
                "gps",
                active=True,
                status="running",
                driver="GPSTest",
                message="GPSTest 已在手机前台持续请求 GNSS",
            )
        except AdbError as exc:
            detail = str(exc)
            if "USER_RESTRICTED" in detail or detail == "ADB 返回代码 1":
                detail = (
                    "手机拒绝通过 USB 安装辅助应用。请在开发者选项开启"
                    "“USB 安装”，再点击启动搜星。"
                )
            self._update(
                "gps",
                active=False,
                status="error",
                message=detail,
            )
        except Exception as exc:
            self._update(
                "gps",
                active=False,
                status="error",
                message=str(exc),
            )

    def _start_cit_gps(self) -> bool:
        """Use Xiaomi's built-in CIT GPS test when available."""
        assert self.adb is not None
        try:
            package_path = self.adb.shell_command(
                "pm path com.miui.cit", timeout=10
            ).strip()
            if not package_path.startswith("package:"):
                return False
            self._update("gps", message="正在打开手机内置 CIT GPS 测试…")
            self.adb.shell_command(
                "am start -W -n com.miui.cit/.CitLauncherActivity", timeout=20
            )
            size_output = self.adb.shell_command("wm size", timeout=8)
            size_match = re.search(r"(\d+)x(\d+)", size_output)
            width = int(size_match.group(1)) if size_match else 1080
            height = int(size_match.group(2)) if size_match else 2400
            for _ in range(8):
                xml_output = self.adb.shell_command(
                    "uiautomator dump /sdcard/mobile-monitor-cit.xml "
                    ">/dev/null 2>&1; "
                    "cat /sdcard/mobile-monitor-cit.xml",
                    timeout=15,
                )
                node_match = re.search(
                    r'<node\b[^>]*\btext="[^"]*GPS[^"]*"[^>]*'
                    r'\bbounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    xml_output,
                    re.I,
                )
                if node_match:
                    left, top, right, bottom = map(int, node_match.groups())
                    self.adb.shell_command(
                        f"input tap {(left + right) // 2} {(top + bottom) // 2}",
                        timeout=8,
                    )
                    time.sleep(0.8)
                    activities = self.adb.shell_command(
                        "dumpsys activity activities", timeout=12
                    )
                    if "com.miui.cit/.sensor.CitGpsCheckActivity" in activities:
                        return True
                    break
                self.adb.shell_command(
                    f"input swipe {width // 2} {int(height * 0.80)} "
                    f"{width // 2} {int(height * 0.22)} 350",
                    timeout=8,
                )
                time.sleep(0.35)
            self.adb.shell_command("am force-stop com.miui.cit", timeout=8)
        except AdbError:
            try:
                self.adb.shell_command("am force-stop com.miui.cit", timeout=8)
            except AdbError:
                pass
        return False

    def _cit_metrics_loop(self) -> None:
        assert self.adb is not None
        while not self.stop_events["gps"].is_set():
            try:
                xml_output = self.adb.shell_command(
                    "uiautomator dump /sdcard/mobile-monitor-gps.xml "
                    ">/dev/null 2>&1; "
                    "cat /sdcard/mobile-monitor-gps.xml",
                    timeout=15,
                )
                values = [
                    float(value)
                    for value in re.findall(r"\bSnr:\s*([\d.]+)", xml_output, re.I)
                    if 0 <= float(value) <= 99
                ]
                visible = match_int(r"卫星数量:\s*(\d+)", xml_output)
                if values:
                    ordered = sorted(values, reverse=True)
                    top = ordered[:4]
                    self._update(
                        "gps",
                        cn0_avg_dbhz=sum(values) / len(values),
                        cn0_top4_dbhz=sum(top) / len(top),
                        cn0_max_dbhz=max(values),
                        satellites_visible=visible or len(values),
                    )
                elif visible is not None:
                    self._update("gps", satellites_visible=visible)
            except AdbError:
                pass
            self.stop_events["gps"].wait(3.0)

    def enrich_sample(self, payload: dict[str, Any]) -> dict[str, Any]:
        gps_load = self.snapshot()["gps"]
        gps = payload.get("gps")
        if (
            isinstance(gps, dict)
            and gps_load.get("status") == "running"
            and gps_load.get("cn0_top4_dbhz") is not None
        ):
            gps.update(
                cn0_avg_dbhz=gps_load["cn0_avg_dbhz"],
                cn0_top4_dbhz=gps_load["cn0_top4_dbhz"],
                cn0_max_dbhz=gps_load["cn0_max_dbhz"],
                satellites_visible=gps_load["satellites_visible"],
                signal_available=True,
                source=f'{gps_load.get("driver", "CIT")} foreground status',
            )
        return payload

    def _gps_stop(self) -> None:
        try:
            if not self.mock and self.adb:
                driver = self.snapshot()["gps"].get("driver")
                package = "com.miui.cit" if driver == "MIUI CIT" else GPSTEST_PACKAGE
                self.adb.shell_command(f"am force-stop {package}", timeout=10)
                if not self.gps_location_was_enabled:
                    self.adb.shell_command(
                        "cmd location set-location-enabled false", timeout=10
                    )
            self._update(
                "gps",
                active=False,
                status="idle",
                driver=None,
                message="持续搜星已停止",
            )
        except Exception as exc:
            self._update(
                "gps", active=False, status="error", message=f"停止失败：{exc}"
            )

    def _select_interface(self, name: str) -> str:
        sample = self.latest_sample() or {}
        section = sample.get(name, {})
        if name == "wifi":
            if not section.get("connected"):
                raise RuntimeError("Wi-Fi 未连接，无法启动 WLAN 测速")
            candidates = [
                item
                for item in section.get("interfaces", [])
                if re.fullmatch(r"wlan\d+", item)
            ]
            return candidates[0] if candidates else "wlan0"

        if not section.get("connected"):
            raise RuntimeError("蜂窝数据链路未连接；测试白卡当前无可用路由")
        assert self.adb is not None
        connectivity = self.adb.shell_command("dumpsys connectivity", timeout=15)
        blocks = re.split(
            r"(?=^\s*NetworkAgentInfo\{network\{)",
            connectivity,
            flags=re.M,
        )
        candidates: list[tuple[str, int | None]] = []
        for block in blocks:
            if (
                "Transports: CELLULAR" not in block
                or "INTERNET" not in block
                or "VALIDATED" not in block
            ):
                continue
            interface_match = re.search(
                r"InterfaceName:\s*((?:r_)?rmnet_data\d+|ccmni\d+|pdp\d+)",
                block,
            )
            if not interface_match:
                continue
            sub_match = re.search(
                r"(?:mSubId\s*=\s*|SubscriptionIds:\s*\{)(\d+)",
                block,
            )
            candidates.append(
                (
                    interface_match.group(1),
                    int(sub_match.group(1)) if sub_match else None,
                )
            )
        if not candidates:
            raise RuntimeError("未找到带 INTERNET + VALIDATED 的蜂窝数据网络")

        # Prefer the general-purpose APN; IMS can also be validated but does not
        # carry ordinary application traffic.
        interface, sub_id = candidates[0]
        for candidate_interface, candidate_sub_id in candidates:
            candidate_block = next(
                (
                    block
                    for block in blocks
                    if f"InterfaceName: {candidate_interface}" in block
                ),
                "",
            )
            if "extra: ims" not in candidate_block and "Capabilities: IMS" not in candidate_block:
                interface, sub_id = candidate_interface, candidate_sub_id
                break

        sim_slot = None
        if sub_id is not None:
            subscriptions = self.adb.shell_command("dumpsys isub", timeout=12)
            slot_match = re.search(
                rf"\{{id={sub_id}\b[^\n]*simSlotIndex=(\d+)",
                subscriptions,
            )
            if slot_match:
                sim_slot = int(slot_match.group(1)) + 1
        self._update(
            "cellular",
            subscription_id=sub_id,
            sim_slot=sim_slot,
        )
        return interface

    def _prepare_cellular_route(self, interface: str) -> None:
        assert self.adb is not None
        wifi_status = self.adb.shell_command("cmd wifi status", timeout=10)
        self.cellular_wifi_was_enabled = "Wifi is enabled" in wifi_status
        if self.cellular_wifi_was_enabled:
            self._update(
                "cellular",
                message="正在临时关闭 Wi-Fi，并切换到蜂窝默认路由…",
            )
            self.adb.shell_command("svc wifi disable", timeout=10)
            self.cellular_wifi_temporarily_disabled = True

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            route = self.adb.shell_command(
                "ip route get 1.1.1.1 2>/dev/null", timeout=8
            )
            if re.search(rf"\bdev\s+{re.escape(interface)}\b", route):
                return
            if self.stop_events["cellular"].wait(0.6):
                raise RuntimeError("蜂窝测速已取消")
        raise RuntimeError(f"等待 {interface} 成为默认网络超时")

    def _restore_wifi_after_cellular(self) -> None:
        if (
            self.cellular_wifi_temporarily_disabled
            and self.cellular_wifi_was_enabled
            and self.adb
        ):
            try:
                self.adb.shell_command("svc wifi enable", timeout=10)
            except AdbError:
                return
            finally:
                self.cellular_wifi_temporarily_disabled = False

    def _curl_transfer(
        self, interface: str, upload: bool
    ) -> tuple[float, int, int]:
        assert self.adb is not None
        label = "UPLOAD" if upload else "DOWNLOAD"
        url = self.upload_url if upload else self.download_url.format(
            bytes=self.transfer_bytes
        )
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError("测速 URL 必须使用 http 或 https")
        common = (
            f"curl --interface {shlex.quote(interface)} --location --fail "
            "--silent --show-error --connect-timeout 5 --max-time 20 "
            "--output /dev/null "
        )
        if upload:
            payload = "/data/local/tmp/mobile-monitor-payload.bin"
            command = (
                f"{common} --request POST --data-binary @{payload} "
                f"--write-out '{label}:%{{speed_upload}}:%{{size_upload}}:%{{http_code}}' "
                f"{shlex.quote(url)} 2>&1 || true"
            )
        else:
            command = (
                f"{common} "
                f"--write-out '{label}:%{{speed_download}}:%{{size_download}}:%{{http_code}}' "
                f"{shlex.quote(url)} 2>&1 || true"
            )
        output = self.adb.shell_command(command, timeout=25)
        match = re.search(
            rf"{label}:([\d.]+):([\d.]+):(\d+)", output
        )
        if not match:
            raise RuntimeError(f"{label} 测速没有返回有效结果")
        bytes_per_second = float(match.group(1))
        transferred = int(float(match.group(2)))
        status_code = int(match.group(3))
        if bytes_per_second <= 0 or transferred <= 0 or not 200 <= status_code < 400:
            raise RuntimeError(output.strip() or f"{label} 测速未产生有效流量")
        return bytes_per_second * 8 / 1_000_000, transferred, status_code

    def _network_loop(self, name: str) -> None:
        if self.mock:
            self._update(
                name,
                active=True,
                status="running",
                interface="wlan0" if name == "wifi" else "rmnet_data0",
                message="模拟双向负载已启动",
            )
            return
        route_prepared = False
        try:
            assert self.adb is not None
            interface = self._select_interface(name)
            if name == "cellular":
                self._prepare_cellular_route(interface)
                route_prepared = True
            payload_mb = max(1, math.ceil(self.transfer_bytes / 1_048_576))
            self.adb.shell_command(
                "dd if=/dev/zero "
                "of=/data/local/tmp/mobile-monitor-payload.bin "
                f"bs=1048576 count={payload_mb} 2>/dev/null",
                timeout=20,
            )
            self._update(
                name,
                active=True,
                status="running",
                interface=interface,
                message=(
                    f"正在通过 {interface} 产生双向数据负载"
                    + ("（Wi-Fi 已暂时关闭）" if name == "cellular" else "")
                ),
            )
            while not self.stop_events[name].is_set():
                down_mbps, down_bytes, _ = self._curl_transfer(
                    interface, upload=False
                )
                if self.stop_events[name].is_set():
                    break
                up_mbps, up_bytes, _ = self._curl_transfer(
                    interface, upload=True
                )
                with self.lock:
                    cycles = self.states[name]["cycles"] + 1
                    total = (
                        self.states[name]["total_bytes"]
                        + down_bytes
                        + up_bytes
                    )
                self._update(
                    name,
                    active=True,
                    status="running",
                    last_down_mbps=down_mbps,
                    last_up_mbps=up_mbps,
                    cycles=cycles,
                    total_bytes=total,
                    message=f"双向负载运行中 · 第 {cycles} 轮",
                )
                self.stop_events[name].wait(0.15)
            self._update(
                name, active=False, status="idle", message="数据负载已停止"
            )
        except Exception as exc:
            detail = str(exc)
            if "curl: (28)" in detail and name == "cellular":
                detail = (
                    "数据卡蜂窝接口已成为默认网络，但测速服务器连接超时。"
                    "请重试，或在“测速服务器设置”中更换可达地址。"
                )
            elif "curl: (28)" in detail:
                detail = (
                    "测速服务器连接超时。请检查当前网络，或在"
                    "“测速服务器设置”中更换为可达的 HTTP 地址。"
                )
            self._update(
                name,
                active=False,
                status="error",
                message=detail,
            )
        finally:
            if name == "cellular" and (
                route_prepared or self.cellular_wifi_temporarily_disabled
            ):
                self._restore_wifi_after_cellular()

    def close(self) -> None:
        for name in self.stop_events:
            self.stop_events[name].set()
        if self.snapshot()["gps"]["status"] in ("starting", "running", "stopping"):
            self._gps_stop()
        for thread in self.threads.values():
            if thread and thread.is_alive():
                thread.join(timeout=1)
        self._restore_wifi_after_cellular()
        if self.adb:
            try:
                self.adb.shell_command(
                    "rm -f /data/local/tmp/mobile-monitor-payload.bin "
                    "/sdcard/mobile-monitor-cit.xml "
                    "/sdcard/mobile-monitor-gps.xml",
                    timeout=8,
                )
            except AdbError:
                pass


CSV_FIELDS = [
    "seq",
    "timestamp",
    "elapsed_s",
    "battery.current_ma",
    "battery.raw_current_ma",
    "battery.voltage_v",
    "battery.power_w",
    "battery.level_percent",
    "battery.charge_counter_uah",
    "battery.remaining_charge_mah",
    "battery.delta_level_percent",
    "battery.delta_charge_mah",
    "battery.consumed_mah",
    "battery.average_discharge_ma",
    "battery.baseline_timestamp",
    "battery.elapsed_since_baseline_s",
    "battery.temperature_c",
    "battery.status",
    "battery.usb_powered",
    "gps.state",
    "gps.cn0_avg_dbhz",
    "gps.cn0_top4_dbhz",
    "gps.cn0_max_dbhz",
    "gps.satellites_visible",
    "gps.satellites_used",
    "cellular.state",
    "cellular.radio",
    "cellular.network_type",
    "cellular.rssi_dbm",
    "cellular.rsrp_dbm",
    "cellular.rsrq_db",
    "cellular.sinr_db",
    "cellular.down_kbps",
    "cellular.up_kbps",
    "wifi.state",
    "wifi.ssid",
    "wifi.rssi_dbm",
    "wifi.tx_link_mbps",
    "wifi.rx_link_mbps",
    "wifi.down_kbps",
    "wifi.up_kbps",
    "error",
]


def nested_value(record: dict[str, Any], dotted: str) -> Any:
    value: Any = record
    for key in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


class Sampler:
    def __init__(
        self,
        interval: float,
        history_size: int,
        adb: AdbClient | None,
        mock: bool,
        log_enabled: bool,
    ):
        self.interval = interval
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self.adb = adb
        self.mock = MockCollector() if mock else None
        self.parser = MeasurementParser()
        self.enricher: Any = None
        self.log_enabled = log_enabled
        self.started_at = time.monotonic()
        self.seq = 0
        self.latest: dict[str, Any] | None = None
        self.running = True
        self.shutdown_event = threading.Event()
        self.condition = threading.Condition()
        self.csv_lock = threading.Lock()
        self.battery_lock = threading.RLock()
        self.battery_baseline: dict[str, Any] | None = None
        self.previous_usb_powered: bool | None = None
        DATA_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.csv_path = DATA_DIR / f"session-{stamp}.csv"
        self.thread = threading.Thread(target=self._loop, name="phone-sampler", daemon=True)

    @property
    def device_info(self) -> dict[str, str]:
        if self.mock:
            return self.mock.device_info
        return self.adb.device_info if self.adb else {}

    def start(self) -> None:
        if self.log_enabled:
            with self.csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
                csv.DictWriter(handle, fieldnames=CSV_FIELDS).writeheader()
        self.thread.start()

    def set_running(self, running: bool) -> None:
        self.running = running
        with self.condition:
            self.condition.notify_all()

    def reset_battery_baseline(self) -> dict[str, Any]:
        with self.condition:
            latest = self.latest
        battery = latest.get("battery", {}) if latest else {}
        level = battery.get("level_percent")
        counter = battery.get("charge_counter_uah")
        if level is None and counter is None:
            raise ValueError("尚未取得有效电量数据，无法设置基线")
        now_iso = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with self.battery_lock:
            self.battery_baseline = {
                "level_percent": level,
                "charge_counter_uah": counter,
                "monotonic": time.monotonic(),
                "timestamp": now_iso,
            }
        return self.power_status()

    def _track_battery(
        self, payload: dict[str, Any], captured_monotonic: float, captured_at: str
    ) -> None:
        battery = payload.get("battery")
        if not isinstance(battery, dict):
            return
        level = battery.get("level_percent")
        counter = battery.get("charge_counter_uah")
        battery["remaining_charge_mah"] = (
            counter / 1000 if isinstance(counter, (int, float)) else None
        )
        usb_powered = battery.get("usb_powered")
        with self.battery_lock:
            unplugged_now = (
                self.previous_usb_powered is True and usb_powered is False
            )
            if self.battery_baseline is None or unplugged_now:
                self.battery_baseline = {
                    "level_percent": level,
                    "charge_counter_uah": counter,
                    "monotonic": captured_monotonic,
                    "timestamp": captured_at,
                }
            if isinstance(usb_powered, bool):
                self.previous_usb_powered = usb_powered

            baseline = self.battery_baseline
            elapsed = max(
                0.0, captured_monotonic - float(baseline["monotonic"])
            )
            baseline_level = baseline.get("level_percent")
            baseline_counter = baseline.get("charge_counter_uah")
            delta_level = (
                level - baseline_level
                if isinstance(level, (int, float))
                and isinstance(baseline_level, (int, float))
                else None
            )
            delta_charge = (
                (counter - baseline_counter) / 1000
                if isinstance(counter, (int, float))
                and isinstance(baseline_counter, (int, float))
                else None
            )
            consumed = (
                max(0.0, -delta_charge) if delta_charge is not None else None
            )
            average_discharge = (
                consumed / (elapsed / 3600)
                if consumed is not None and elapsed >= 30
                else None
            )
            battery.update(
                {
                    "baseline_level_percent": baseline_level,
                    "baseline_charge_counter_uah": baseline_counter,
                    "baseline_timestamp": baseline["timestamp"],
                    "elapsed_since_baseline_s": elapsed,
                    "delta_level_percent": delta_level,
                    "delta_charge_mah": delta_charge,
                    "consumed_mah": consumed,
                    "average_discharge_ma": average_discharge,
                    "baseline_auto_reset_on_unplug": unplugged_now,
                }
            )

    def power_status(self) -> dict[str, Any]:
        with self.condition:
            latest = self.latest
        battery = latest.get("battery", {}) if latest else {}
        usb_powered = battery.get("usb_powered")
        wireless = bool(self.adb and self.adb.wireless)
        if usb_powered is False:
            state = "battery_only"
            message = "USB 供电已断开，正在按电池基线记录耗电"
        elif wireless:
            state = "ready_to_unplug"
            message = "无线 ADB 已就绪；请拔掉 USB 数据线以真实断开充电"
        elif usb_powered is True:
            state = "usb_powered"
            message = "USB 正在供电；先建立无线 ADB，再拔掉数据线"
        else:
            state = "unknown"
            message = "等待手机供电状态"
        return {
            "state": state,
            "message": message,
            "usb_powered": usb_powered,
            "wireless_adb": wireless,
            "adb_endpoint": self.adb.serial if wireless and self.adb else None,
            "software_charge_disable_supported": False,
            "baseline_timestamp": battery.get("baseline_timestamp"),
        }

    def prepare_wireless_adb(self) -> dict[str, Any]:
        if self.mock or not self.adb:
            raise ValueError("模拟模式不能建立无线 ADB")
        connection = self.adb.enable_wireless()
        result = self.power_status()
        result.update(connection)
        return result

    def stop(self) -> None:
        self.shutdown_event.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=3)

    def _append_csv(self, sample: dict[str, Any]) -> None:
        if not self.log_enabled:
            return
        row = {field: nested_value(sample, field) for field in CSV_FIELDS}
        with self.csv_lock:
            with self.csv_path.open("a", newline="", encoding="utf-8-sig") as handle:
                csv.DictWriter(handle, fieldnames=CSV_FIELDS).writerow(row)

    def _publish(self, payload: dict[str, Any]) -> None:
        self.seq += 1
        captured_monotonic = time.monotonic()
        captured_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        self._track_battery(payload, captured_monotonic, captured_at)
        sample = {
            "seq": self.seq,
            "timestamp": captured_at,
            "elapsed_s": captured_monotonic - self.started_at,
            "device": self.device_info,
            "collector": {
                "connected": "error" not in payload,
                "paused": not self.running,
                "mock": bool(self.mock),
                "interval_s": self.interval,
            },
            **payload,
        }
        self._append_csv(sample)
        with self.condition:
            self.latest = sample
            self.history.append(sample)
            self.condition.notify_all()

    def _loop(self) -> None:
        while not self.shutdown_event.is_set():
            if not self.running:
                self.shutdown_event.wait(0.25)
                continue
            cycle_start = time.monotonic()
            try:
                if self.mock:
                    payload = self.mock.sample()
                else:
                    assert self.adb is not None
                    raw = self.adb.collect()
                    payload = self.parser.parse(raw, time.monotonic())
                if self.enricher:
                    payload = self.enricher(payload)
                self._publish(payload)
            except Exception as exc:  # Keep the web interface alive on disconnect.
                self._publish({"error": str(exc)})
            remaining = self.interval - (time.monotonic() - cycle_start)
            if remaining > 0:
                self.shutdown_event.wait(remaining)

    def wait_after(self, sequence: int, timeout: float = 15) -> dict[str, Any] | None:
        with self.condition:
            if not self.latest or self.latest["seq"] <= sequence:
                self.condition.wait(timeout)
            if self.latest and self.latest["seq"] > sequence:
                return self.latest
            return None


def make_handler(sampler: Sampler, workloads: WorkloadController):
    class MonitorHandler(BaseHTTPRequestHandler):
        server_version = "K30iMonitor/1.0"

        def log_message(self, format_string: str, *args: Any) -> None:
            if getattr(self.server, "verbose", False):
                super().log_message(format_string, *args)

        def send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/status":
                self.send_json(
                    {
                        "latest": sampler.latest,
                        "running": sampler.running,
                        "device": sampler.device_info,
                        "csv_file": sampler.csv_path.name if sampler.log_enabled else None,
                        "workloads": workloads.snapshot(),
                        "power_control": sampler.power_status(),
                    }
                )
                return
            if parsed.path == "/api/power":
                self.send_json(sampler.power_status())
                return
            if parsed.path == "/api/workloads":
                self.send_json(workloads.snapshot())
                return
            if parsed.path == "/api/history":
                query = parse_qs(parsed.query)
                try:
                    limit = max(1, min(3600, int(query.get("limit", ["300"])[0])))
                except ValueError:
                    limit = 300
                self.send_json(list(sampler.history)[-limit:])
                return
            if parsed.path == "/api/events":
                self._events()
                return
            if parsed.path == "/api/export":
                self._export_csv()
                return
            self._static(parsed.path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in (
                "/api/control",
                "/api/power",
                "/api/workloads/control",
                "/api/workloads/config",
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self.send_json({"error": "请求格式无效"}, 400)
                return
            action = payload.get("action")
            if parsed.path == "/api/power":
                try:
                    if action == "prepare_wireless":
                        result = sampler.prepare_wireless_adb()
                    elif action == "reset_baseline":
                        result = sampler.reset_battery_baseline()
                    else:
                        raise ValueError("未知电源控制操作")
                except (ValueError, AdbError) as exc:
                    self.send_json({"error": str(exc)}, 400)
                    return
                self.send_json({"ok": True, "power_control": result})
                return
            if parsed.path == "/api/control":
                if action == "pause":
                    sampler.set_running(False)
                elif action == "resume":
                    sampler.set_running(True)
                else:
                    self.send_json({"error": "未知操作"}, 400)
                    return
                self.send_json({"ok": True, "running": sampler.running})
                return
            try:
                if parsed.path == "/api/workloads/config":
                    result = workloads.update_config(
                        payload.get("transfer_size_mb"),
                        payload.get("download_url"),
                        payload.get("upload_url"),
                    )
                elif action == "start":
                    result = workloads.start(str(payload.get("name", "")))
                elif action == "stop":
                    result = workloads.stop(str(payload.get("name", "")))
                else:
                    raise ValueError("未知操作")
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            self.send_json({"ok": True, "workloads": result})

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last = -1
            try:
                while not sampler.shutdown_event.is_set():
                    sample = sampler.wait_after(last, 12)
                    if sample:
                        last = sample["seq"]
                        data = json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
                        self.wfile.write(f"id: {last}\ndata: {data}\n\n".encode("utf-8"))
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _export_csv(self) -> None:
            if not sampler.log_enabled or not sampler.csv_path.exists():
                self.send_json({"error": "当前未启用 CSV 记录"}, 404)
                return
            with sampler.csv_lock:
                body = sampler.csv_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header(
                "Content-Disposition", f'attachment; filename="{sampler.csv_path.name}"'
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, request_path: str) -> None:
            relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
            target = (STATIC_DIR / relative).resolve()
            try:
                target.relative_to(STATIC_DIR.resolve())
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = target.read_bytes()
            media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if media_type.startswith("text/") or media_type in ("application/javascript", "application/json"):
                media_type += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return MonitorHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Android 电量与无线信号实时监控")
    parser.add_argument("--adb", help="adb/adb.exe 路径")
    parser.add_argument("--serial", help="ADB 设备序列号或 ip:port")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--interval", type=float, default=1.5, help="采样间隔秒数")
    parser.add_argument("--history", type=int, default=3600, help="内存保留样本数")
    parser.add_argument("--no-log", action="store_true", help="不写入 CSV")
    parser.add_argument("--mock", action="store_true", help="使用模拟数据")
    parser.add_argument(
        "--speed-size-mb",
        type=int,
        default=2,
        help="每轮测速的下载和上传数据量 MB（默认各 2 MB）",
    )
    parser.add_argument(
        "--speed-download-url",
        default="https://speed.cloudflare.com/__down?bytes={bytes}",
        help="下载测速 URL，可使用 {bytes} 占位符",
    )
    parser.add_argument(
        "--speed-upload-url",
        default="https://speed.cloudflare.com/__up",
        help="上传测速 URL",
    )
    parser.add_argument("--open", action="store_true", help="自动打开浏览器")
    parser.add_argument("--verbose", action="store_true", help="输出 HTTP 请求日志")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval < 0.5:
        print("采样间隔不能小于 0.5 秒。", file=sys.stderr)
        return 2
    if not 1 <= args.speed_size_mb <= 100:
        print("每轮测速数据量必须在 1–100 MB 之间。", file=sys.stderr)
        return 2
    try:
        adb = None if args.mock else AdbClient(args.adb, args.serial)
        if adb:
            try:
                adb.ensure_device()
                print(
                    f"已连接：{adb.device_info.get('model', 'Android')} "
                    f"({adb.device_info.get('serial', '')})"
                )
            except AdbError as exc:
                print(f"设备暂未就绪：{exc}；监控服务仍会启动并自动重试。")
    except AdbError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    sampler = Sampler(
        interval=args.interval,
        history_size=max(60, args.history),
        adb=adb,
        mock=args.mock,
        log_enabled=not args.no_log,
    )
    workloads = WorkloadController(
        adb=adb,
        latest_sample=lambda: sampler.latest,
        mock=args.mock,
        transfer_bytes=args.speed_size_mb * 1_000_000,
        download_url=args.speed_download_url,
        upload_url=args.speed_upload_url,
    )
    sampler.enricher = workloads.enrich_sample
    sampler.start()
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(sampler, workloads)
    )
    server.daemon_threads = True
    server.verbose = args.verbose
    url = f"http://{args.host}:{args.port}"
    print(f"监控界面：{url}")
    if not args.no_log:
        print(f"数据记录：{sampler.csv_path}")
    print("按 Ctrl+C 停止。")
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.shutdown()
        server.server_close()
        workloads.close()
        sampler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
