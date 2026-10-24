"""Request-bound observations. Host timing does not imply hardware synchronization."""
import copy
import hashlib
import math
from pathlib import Path
import re
import time
import uuid

IDENTITY_FIELDS = ("specimen_id", "feature_id", "class_id", "calibration_id", "reference_id")


def new_context(metadata=None, *, query="", now=None):
    metadata = {} if metadata is None else metadata
    if not isinstance(metadata, dict) or set(metadata) - set(IDENTITY_FIELDS):
        raise ValueError("measurement_context 只接受工件、特征、类别、标定和参考的身份字段")
    identity = {}
    for key in IDENTITY_FIELDS:
        value = metadata.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 128):
            raise ValueError("无效身份字段: " + key)
        identity[key] = value.strip() if value is not None else None
    classes = re.findall(r"^\s*(?:工件类别|类别|class_id|class_name)\s*[:：=]\s*([\w-]+)\s*$", query or "", re.I|re.M)
    if len(set(classes)) > 1 or (classes and identity["class_id"] not in (None,classes[0])):
        raise ValueError("工件类别声明冲突，请明确唯一的 class_id")
    if classes:
        identity["class_id"] = classes[0]
    return {"schema_version":"measurement-context-v1", "event_id":uuid.uuid4().hex,
            "created_at":time.time() if now is None else now,
            "identity_source":"user_declared", **identity, "observations":{},
            "fusion_status":"no_bound_observations"}


def bind_percepts(context, percepts, *, max_skew_seconds=2.0):
    context = copy.deepcopy(context)
    event_id = context["event_id"]
    observations = {}
    for percept in percepts or []:
        if percept.get("event_id") not in (None, event_id):
            raise ValueError("感知对象来自其他测量事件")
        ptype = percept.get("type")
        if ptype not in ("image", "camera", "sensor"):
            continue
        key = "sensor" if ptype == "sensor" else "image"
        if key in observations:
            continue  # upload has priority; never combine two independently acquired frames
        ts = percept.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int,float)) or not math.isfinite(ts):
            raise ValueError("感知数据缺少有效的主机接收时间")
        payload = copy.deepcopy(percept.get("payload") or {})
        observation = {"event_id":event_id,"observation_id":uuid.uuid4().hex,
                       "source":percept.get("source"),"received_at":ts,
                       "time_kind":"host_receipt","payload":payload}
        if key == "image":
            path = Path(payload.get("path") or "")
            if not path.is_file():
                continue
            observation.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                               capture_time_known=ptype == "camera")
        observations[key] = observation
    context["observations"] = observations
    image, sensor = observations.get("image"), observations.get("sensor")
    if not image or not sensor:
        status = "missing_image_or_sensor"
    elif not image["capture_time_known"]:
        status = "unverified_upload_capture_time"
    elif sensor["payload"].get("unit") != "m/s2" or sensor["payload"].get("unit_source") != "declared":
        status = "unverified_sensor_units"
    elif abs(image["received_at"]-sensor["received_at"]) > max_skew_seconds:
        status = "host_time_mismatch"
    elif min(image["received_at"],sensor["received_at"]) < context["created_at"]-max_skew_seconds:
        status = "stale_observation"
    elif not all(context.get(k) for k in ("specimen_id","feature_id","class_id")):
        status = "missing_measurement_identity"
    else:
        status = "host_time_aligned"  # request association only, not hardware sync or metrological proof
    context["fusion_status"] = status
    context["hardware_synchronized"] = False
    return context


def bound_sensor(context, *, require_alignment=False):
    context = context or {}
    item = (context.get("observations") or {}).get("sensor")
    if not context.get("event_id") or not item or item.get("event_id") != context.get("event_id"):
        return None
    ts = item.get("received_at")
    if not isinstance(ts,(int,float)) or not math.isfinite(ts) or ts < context["created_at"]-2:
        return None
    if require_alignment and context.get("fusion_status") != "host_time_aligned":
        return None
    payload = item.get("payload") or {}
    if payload.get("unit") != "m/s2":
        return None
    keys = ("t","h","ax","ay","az")
    if any(isinstance(payload.get(k),bool) or not isinstance(payload.get(k),(int,float))
           or not math.isfinite(payload[k]) for k in keys):
        return None
    return {k:payload[k] for k in keys}


def bound_image_path(context):
    context = context or {}
    item = (context.get("observations") or {}).get("image")
    if not context.get("event_id") or not item or item.get("event_id") != context["event_id"]:
        return None
    path = Path((item.get("payload") or {}).get("path") or "")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
        return None
    return str(path)


def detection_failure(message, *, status="unavailable", code="unbound_observation", event_id=None):
    return {"success":False,"execution_status":status,"anomaly_score":None,
            "is_anomaly":None,"class_used":None,"verdict":"未完成异常检测",
            "error":message,"error_code":code,"event_id":event_id}


def detect_bound_anomaly(context):
    context = context or {}
    event_id = context.get("event_id")
    class_id = context.get("class_id")
    if not class_id:
        return detection_failure("请明确指定已建库的工件类别（class_id）；不能使用任意默认类别",
                                 status="needs_input",code="missing_class",event_id=event_id)
    image = (context.get("observations") or {}).get("image")
    if not image or image.get("event_id") != event_id:
        return detection_failure("本测量事件没有绑定图像",event_id=event_id)
    path = Path((image.get("payload") or {}).get("path") or "")
    if not path.is_file():
        return detection_failure("绑定图像不存在",event_id=event_id)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != image.get("sha256"):
        return detection_failure("绑定图像已被修改，需重新采集事件",code="image_changed",event_id=event_id)
    import cv2
    import numpy as np
    frame = cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
    from agent.omni_ad.run_inference import detect_anomaly
    result = detect_anomaly(frame,class_name=class_id)
    return {**result,"event_id":event_id,"image_observation_id":image["observation_id"],
            "image_sha256":image["sha256"],"identity_source":context.get("identity_source")}
