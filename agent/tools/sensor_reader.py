"""ESP32 serial reader: bounded background drain, freshness, reconnect and diagnostics.

EQUIS_SENSOR_PORT can select COM10 explicitly. Multiple compatible adapters require
an explicit port. Importing this module never opens hardware or starts a thread.
"""
import atexit
from collections import deque
import copy
import json
import os
import threading
import time

import serial
import serial.tools.list_ports
from tools.sensor_protocol import SensorState, normalize_units as _normalize_units


def _find_port():
    explicit = os.environ.get("EQUIS_SENSOR_PORT", "").strip()
    if explicit:
        return explicit
    ports = []
    for p in serial.tools.list_ports.comports():
        desc = " ".join(str(v or "") for v in (p.description, p.manufacturer)).lower()
        if any(s in desc for s in ("ch340", "cp210", "ch9102", "esp32", "silicon")):
            ports.append(p.device)
    if len(ports) > 1:
        raise ValueError("发现多个传感器串口，请设置 EQUIS_SENSOR_PORT 指定设备")
    return ports[0] if ports else None


class SensorReader:
    def __init__(self):
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread = None
        self.conn = None
        self.state = SensorState()
        self.error = None
        self.port = None
        self.records = deque(maxlen=4096)
        self.cursor = 0

    def start(self):
        with self.condition:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name="equis-sensor", daemon=True)
            self.thread.start()

    def _record(self, raw, wall, mono):
        error = None
        try:
            packet = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError("nonfinite_json")))
            self.state.ingest(packet, wall, mono)
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            self.state.stats["invalid"] += 1
            error = str(exc)
        self.cursor += 1
        self.records.append({"cursor": self.cursor, "received_at": wall,
                             "received_monotonic": mono, "raw": raw,
                             "accepted": error is None, "error": error})
        self.condition.notify_all()

    def _run(self):
        pending = b""
        while not self.stop_event.is_set():
            try:
                if self.conn is None:
                    port = _find_port()
                    if not port:
                        raise OSError("未发现 ESP32 串口")
                    conn = serial.Serial(port=None, baudrate=115200, timeout=0.2, write_timeout=0.2)
                    conn.dtr = False
                    conn.rts = False
                    conn.port = port
                    try:
                        conn.open()
                        conn.reset_input_buffer()  # discard pre-connection backlog
                    except Exception:
                        conn.close()
                        raise
                    with self.condition:
                        self.conn, self.port = conn, port
                        self.state.reset()
                        self.state.stats["reconnections"] += 1
                        self.error = None
                        pending = b""
                chunk = self.conn.read(max(1, min(self.conn.in_waiting, 8192)))
                if not chunk:
                    continue
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not line:
                        continue
                    with self.condition:
                        if len(line) > 8192:
                            self.state.stats["invalid"] += 1
                            self.error = "串口数据行过长"
                        else:
                            self._record(line.decode("utf-8", "replace").strip(), time.time(), time.monotonic())
                if len(pending) > 8192:
                    pending = b""
                    with self.condition:
                        self.state.stats["invalid"] += 1
                        self.error = "串口数据缺少换行或长度超限"
            except (serial.SerialException, OSError, ValueError) as exc:
                with self.condition:
                    self.error = str(exc)
                    self.state.reset()  # no old readings survive a disconnected transport
                    if self.conn is not None:
                        try:
                            self.conn.close()
                        except OSError:
                            pass
                    self.conn = None
                    self.condition.notify_all()
                self.stop_event.wait(1.0)
        with self.condition:
            if self.conn is not None:
                self.conn.close()
            self.conn = None
            self.state.reset()
            self.condition.notify_all()

    def snapshot(self, timeout=0):
        deadline = time.monotonic()+max(0, timeout)
        self.start()
        with self.condition:
            while True:
                result = self.state.snapshot(time.monotonic())
                if result and (result["fresh"] or timeout <= 0):
                    return result
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    return result
                self.condition.wait(min(remaining, 0.2))

    def status(self):
        with self.condition:
            return {"port": self.port, "connected": bool(self.conn and self.conn.is_open),
                    "error": self.error, "stats": dict(self.state.stats),
                    "snapshot": self.state.snapshot(time.monotonic())}

    def packets_since(self, cursor):
        with self.condition:
            lost = max(0, self.records[0]["cursor"]-cursor-1) if self.records else 0
            return copy.deepcopy([r for r in self.records if r["cursor"] > cursor]), lost

    def close(self):
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        if thread is not None and thread.is_alive():
            raise RuntimeError("传感器读取线程未退出，不能重复打开串口")


_reader = SensorReader()


def close_serial():
    _reader.close()


def read_sensors_raw(timeout=4.0):
    """Latest joined snapshot; stale/failed groups contain None, never fabricated zero."""
    return _reader.snapshot(timeout)


def get_sensor_status():
    return _reader.status()


def get_sensor_data():
    d = read_sensors_raw()
    if not d:
        return "传感器不可用："+str(get_sensor_status().get("error") or "未收到有效数据")
    def fmt(value, digits=2):
        return "无有效读数" if value is None else f"{value:.{digits}f}"
    lines = [f"DHT22: 温度 {fmt(d['t'],1)}°C, 湿度 {fmt(d['h'],1)}%RH",
             f"IMU {d['imu_model']} 加速度（含重力）: X={fmt(d['ax'])} Y={fmt(d['ay'])} Z={fmt(d['az'])} [{d['unit']}]",
             f"IMU 陀螺仪: X={fmt(d['gx'])} Y={fmt(d['gy'])} Z={fmt(d['gz'])} [{d['gyro_unit']}]",
             f"固件={d['firmware_version'] or '旧版未声明'}; 序号={d['sequence']}; 数据有效={d['fresh']}"]
    if d["unit_source"] != "declared":
        lines.append("旧固件单位仅为诊断推断，不作为物理融合依据")
    window = d["imu_window"]
    if window["ac_rms_m_s2"] is not None:
        lines.append(f"窗口去均值加速度 RMS={fmt(window['ac_rms_m_s2'],4)} m/s²（未校准）; "
                     f"有效样本 {window['valid_count']}/{window['sample_count']}; 实际采样率 {fmt(window['effective_hz'],1)} Hz")
    lines.append("主机接收时间不代表相机曝光同步；窗口指标不能换算为毫米误差。")
    return "\n".join(lines)


atexit.register(close_serial)

if __name__ == "__main__":
    try:
        print(get_sensor_data())
    finally:
        close_serial()
