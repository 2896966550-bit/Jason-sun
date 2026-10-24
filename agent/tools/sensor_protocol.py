"""Bounded sensor state and protocol validation; no serial IO or app credentials."""
from collections import deque
import copy
import math

KEYS = ("t", "h", "ax", "ay", "az", "gx", "gy", "gz")


def finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def normalize_units(data):
    """Legacy compatibility: inferred units are marked and never authorize fusion."""
    if not isinstance(data, dict):
        return None
    out = dict(data)
    out.update({k: finite(data.get(k)) for k in KEYS})
    for key, low, high in (("t", -40, 80), ("h", 0, 100)):
        if out[key] is not None and not low <= out[key] <= high:
            out[key] = None
    unit = str(data.get("unit", "")).strip().lower()
    declared = unit in ("g", "m/s2", "m/s^2", "ms2")
    out["unit_source"] = "declared" if declared else "inferred"
    values = [out[k] for k in ("ax", "ay", "az")]
    scale = 1.0
    if unit == "g":
        scale = 9.80665
    elif not declared:
        # Only diagnostic backwards compatibility. Explicitly unknown units are NOT inferred.
        mag = math.hypot(*values) if all(v is not None for v in values) else None
        if unit:
            out["unit_source"] = "unknown"
        elif mag is not None and 0.5 < mag < 1.5:
            scale = 9.80665
        elif mag is None or not 5 < mag < 15:
            out["unit_source"] = "unknown"
    out["unit"] = "unknown" if out["unit_source"] == "unknown" else "m/s2"
    if out["unit"] == "m/s2":
        for key in ("ax", "ay", "az"):
            out[key] = None if out[key] is None else finite(out[key] * scale)
    return out


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class SensorState:
    """Call under the reader's condition lock. Device time is NOT PC/exposure time."""
    def __init__(self):
        self.stats = {k: 0 for k in ("accepted", "invalid", "sequence_gaps", "duplicates",
                                    "boot_changes", "invalid_imu", "reconnections")}
        self.reset()

    def reset(self):
        self.boot = None
        self.seq = None
        self.fw = None
        self.mode = None
        self.imu = None
        self.env = None
        self.info = {}
        self.latest_us = 0
        self.window = deque(maxlen=2048)

    def ingest(self, packet, wall, mono):
        if not isinstance(packet, dict):
            raise ValueError("packet_not_object")
        if "v" not in packet:
            if not any(k in packet for k in KEYS):
                raise ValueError("unknown_legacy_packet")
            if self.mode == "v2":
                raise ValueError("legacy_packet_in_v2_stream")
            data = normalize_units(packet)
            self.mode = "legacy"
            item = {"data": data, "wall": wall, "mono": mono, "us": None}
            self.env = self.imu = item
            self.stats["accepted"] += 1
            return
        if type(packet.get("v")) is not int or packet["v"] != 2:
            raise ValueError("unsupported_protocol")
        boot, seq, kind = packet.get("boot"), packet.get("seq"), packet.get("type")
        if not isinstance(boot, str) or not 1 <= len(boot) <= 64 or not integer(seq):
            raise ValueError("invalid_identity_or_sequence")
        if kind not in ("imu", "env", "info"):
            raise ValueError("unknown_packet_type")
        parsed = []
        if kind == "imu":
            if packet.get("unit") != "m/s2" or packet.get("gyro_unit") != "deg/s":
                raise ValueError("unverified_units")
            rows = packet.get("samples")
            if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
                raise ValueError("invalid_batch")
            previous = self.imu["us"] if self.boot == boot and self.imu else None
            for row in rows:
                if not isinstance(row, list) or len(row) != 8 or not integer(row[0]) or type(row[7]) is not bool:
                    raise ValueError("invalid_sample")
                if previous is not None and row[0] <= previous:
                    raise ValueError("nonmonotonic_device_time")
                previous = row[0]
                vals = [finite(v) for v in row[1:7]]
                valid = row[7] and all(v is not None for v in vals)
                # Configured sensor full scales, allowing rounding at the endpoint.
                valid = valid and all(abs(v) <= 8*9.80665+0.01 for v in vals[:3]) and all(abs(v) <= 500.1 for v in vals[3:])
                parsed.append({"us": row[0], "wall": wall, "mono": mono,
                               "valid": valid, "data": dict(zip(KEYS[2:], vals if valid else [None]*6))})
        else:
            if not integer(packet.get("t_us")):
                raise ValueError("invalid_device_time")
            if kind == "env":
                if packet.get("temperature_unit") != "degC" or packet.get("humidity_unit") != "percentRH" or type(packet.get("valid")) is not bool:
                    raise ValueError("invalid_environment_contract")
                data = normalize_units(packet)
                valid = packet["valid"] and data["t"] is not None and data["h"] is not None
                parsed = [{"us": packet["t_us"], "wall": wall, "mono": mono,
                           "valid": valid, "data": {k: data[k] if valid else None for k in ("t", "h")}}]
                if self.boot == boot and self.env and parsed[0]["us"] <= self.env["us"]:
                    raise ValueError("nonmonotonic_environment_time")
        if self.boot == boot and self.seq is not None and seq <= self.seq:
            self.stats["duplicates"] += 1
            raise ValueError("duplicate_or_out_of_order")
        if self.boot != boot:
            if self.boot is not None:
                self.stats["boot_changes"] += 1
            self.reset()
            self.boot = boot
        elif self.seq is not None:
            self.stats["sequence_gaps"] += seq-self.seq-1
        self.mode, self.seq, self.fw = "v2", seq, packet.get("fw")
        if kind == "imu":
            for sample in parsed:
                self.imu = sample
                self.window.append(sample)
                if not sample["valid"]:
                    self.stats["invalid_imu"] += 1
            self.latest_us = max(self.latest_us, parsed[-1]["us"])
        elif kind == "env":
            self.env = parsed[0]
            self.latest_us = max(self.latest_us, self.env["us"])
        else:
            self.info = copy.deepcopy(packet)
            self.latest_us = max(self.latest_us, packet["t_us"])
        self.stats["accepted"] += 1

    def snapshot(self, now):
        if self.mode is None:
            return None
        out = {k: None for k in KEYS}
        imu_age = max(0, now-self.imu["mono"]) if self.imu else None
        env_age = max(0, now-self.env["mono"]) if self.env else None
        limit = 3.5 if self.mode == "legacy" else 0.5
        imu_ok = bool(self.imu and imu_age <= limit)
        env_ok = bool(self.env and env_age <= 5.0)
        if self.mode == "v2":
            imu_ok = imu_ok and self.imu["valid"] and self.latest_us-self.imu["us"] <= 500000
            env_ok = env_ok and self.env["valid"] and self.latest_us-self.env["us"] <= 5000000
        if imu_ok:
            out.update({k: self.imu["data"].get(k) for k in KEYS[2:]})
        if env_ok:
            out.update({k: self.env["data"].get(k) for k in ("t", "h")})
        source = self.imu["data"].get("unit_source", "unknown") if self.mode == "legacy" and self.imu else "declared"
        unit = self.imu["data"].get("unit", "unknown") if self.mode == "legacy" and self.imu else "m/s2"
        out.update(imu_model=self.info.get("imu_model", "unverified"), who_am_i=self.info.get("who_am_i"),
                   protocol_version=2 if self.mode == "v2" else 1, firmware_version=self.fw,
                   boot_id=self.boot, sequence=self.seq, unit=unit, unit_source=source,
                   gyro_unit="deg/s" if self.mode == "v2" else "unverified",
                   imu_valid=imu_ok, env_valid=env_ok,
                   fresh=bool(imu_ok and env_ok and all(out[k] is not None for k in KEYS[:5])),
                   imu_age_s=imu_age, env_age_s=env_age,
                   received_at=self.imu["wall"] if self.imu else None,
                   env_received_at=self.env["wall"] if self.env else None,
                   device_time_us=self.imu["us"] if self.imu else None,
                   env_device_time_us=self.env["us"] if self.env else None,
                   time_kind="host_receipt_not_exposure_sync", hardware_synchronized=False,
                   info=copy.deepcopy(self.info), stats=dict(self.stats),
                   timing_verified=self.mode == "v2")
        rows = [s for s in self.window if self.latest_us-s["us"] <= 2000000]
        valid_rows = [s for s in rows if s["valid"]]
        metrics = {"sample_count": len(rows), "valid_count": len(valid_rows),
                   "ac_rms_m_s2": None, "effective_hz": None, "max_gap_ms": None,
                   "duration_s": 0.0, "calibrated": False}
        if len(rows) >= 2:
            dt = (rows[-1]["us"]-rows[0]["us"])/1e6
            metrics.update(duration_s=dt, effective_hz=(len(rows)-1)/dt,
                           max_gap_ms=max((b["us"]-a["us"])/1000 for a,b in zip(rows, rows[1:])))
        if len(valid_rows) >= 2 and imu_ok:
            means = {k: sum(s["data"][k] for s in valid_rows)/len(valid_rows) for k in KEYS[2:5]}
            metrics["ac_rms_m_s2"] = math.sqrt(sum(sum((s["data"][k]-means[k])**2 for k in means) for s in valid_rows)/len(valid_rows))
        out["imu_window"] = metrics
        return out
