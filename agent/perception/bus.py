"""感知总线 —— 统一 image / camera / sensor / text 四类输入

设计目的：Agent 各节点不再各自去调摄像头、读串口或猜上传路径，
而是从感知总线取"已归一化的感知对象"，避免重复采集与状态不一致。

Percept 是唯一的数据契约；payload 只存可序列化内容（路径/数值），
以便直接放入 LangGraph 的 state。
"""
import os, sys, time
import math
import threading
import uuid
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATA_DIR

CAPTURE_DIR = os.path.join(DATA_DIR, "captures")
_camera_lock = threading.Lock()
_sensor_lock = threading.Lock()


@dataclass
class Percept:
    """一个感知对象"""
    type: str                                   # text | image | camera | sensor
    source: str                                 # user | upload | camera0 | esp32
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


# ── 各通道采集 ──────────────────────────────────────────

def from_text(query: str) -> Percept:
    return Percept(type="text", source="user", payload={"text": query or ""})


def from_upload(image_path: str):
    """用户上传的图片"""
    if not image_path or not os.path.exists(image_path):
        return None
    return Percept(type="image", source="upload",
                   payload={"path": image_path, "name": os.path.basename(image_path)})


def from_camera(device: int = 0, tag: str = "cam"):
    """抓取摄像头一帧并落盘（返回 Percept；失败返回 None）"""
    try:
        import cv2
        with _camera_lock:
            cap = cv2.VideoCapture(device)
            try:
                ok, frame = cap.read()
            finally:
                cap.release()
        acquired_at = time.time()
        if not ok or frame is None:
            return None
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        path = os.path.join(CAPTURE_DIR, f"{tag}_{uuid.uuid4().hex}.jpg")
        if not cv2.imwrite(path, frame):
            return None
        return Percept(type="camera", source=f"camera{device}", payload={"path": path},ts=acquired_at)
    except Exception as e:
        print(f"[Percept] 摄像头采集失败: {e}")
        return None


def from_sensor():
    """读取 ESP32 环境数据（DHT22 温湿度 + MPU6050 加速度）"""
    try:
        from tools.sensor_reader import read_sensors_raw
        with _sensor_lock:
            d = read_sensors_raw()
        if not d:
            return None
        if d.get("fresh") is False:
            return None
        if d.get("unit") != "m/s2":
            return None
        keys = ("t","h","ax","ay","az")
        if any(d.get(k) is None or isinstance(d[k],bool) for k in keys):
            return None
        values = {k:float(d[k]) for k in keys}
        if not all(math.isfinite(v) for v in values.values()):
            return None
        metadata_keys = ("imu_model", "who_am_i", "protocol_version", "firmware_version", "boot_id", "sequence",
                         "device_time_us", "env_device_time_us", "received_at", "env_received_at",
                         "imu_age_s", "env_age_s", "fresh", "imu_valid", "env_valid",
                         "time_kind", "hardware_synchronized", "timing_verified", "imu_window")
        metadata = {k:d[k] for k in metadata_keys if k in d}
        received_at = d.get("received_at")
        if received_at is None:
            received_at = time.time()  # compatibility for legacy injected observations only
        return Percept(type="sensor", source="esp32", ts=received_at, payload={
            **values,
            "unit":"m/s2","unit_source":d.get("unit_source","unknown"),
            **metadata,
        })
    except Exception as e:
        print(f"[Percept] 传感器采集失败: {e}")
        return None


# ── 总线入口 ────────────────────────────────────────────

def collect(query: str = "", image_path: str = None,
            want_camera: bool = False, want_sensor: bool = False) -> list:
    """按需采集，返回感知对象列表（至少含 text）"""
    out = [from_text(query)]
    p = from_upload(image_path)
    if p:
        out.append(p)
    if want_camera and not image_path:
        p = from_camera()
        if p:
            out.append(p)
    if want_sensor:
        p = from_sensor()
        if p:
            out.append(p)
    return out


def to_state(percepts: list) -> list:
    """转为可放入 LangGraph state 的 dict 列表"""
    return [asdict(p) for p in (percepts or [])]


def first_of(percepts: list, ptype: str):
    """取指定类型的第一个感知对象"""
    for p in percepts or []:
        if p.type == ptype:
            return p
    return None


def summary(percepts: list) -> str:
    """感知摘要（供提示词使用）"""
    if not percepts:
        return ""
    lines = []
    for p in percepts:
        if p.type in ("image", "camera"):
            lines.append(f"- 图像（{p.source}）：{p.payload.get('path', '')}")
        elif p.type == "sensor":
            pl = p.payload
            lines.append(
                f"- 环境（ESP32）：温度 {pl.get('t')}℃、湿度 {pl.get('h')}%RH、"
                f"加速度 ({pl.get('ax')}, {pl.get('ay')}, {pl.get('az')}) m/s²"
            )
    return "\n".join(lines)
