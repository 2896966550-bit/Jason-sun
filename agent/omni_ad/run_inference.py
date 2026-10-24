"""
Equis × Omni-AD 推理封装器

提供两个对外接口，供 Agent 和外部脚本调用：
  - init_predictor()    全局初始化（加载模型+记忆库，仅需调用一次）
  - detect_anomaly()    单图推理（接受 OpenCV BGR uint8 numpy 数组）

设计要点：
  1. 全局单例：模型只加载一次，后续调用复用 predictor 实例
  2. 格式兼容：接受 (H, W, 3) uint8 BGR（摄像头直出格式），内部自动转换
  3. 容错处理：加载失败返回明确的错误信息，不抛异常导致 Agent 崩溃
"""

import os
import math
import hashlib
import threading
import cv2
import numpy as np
from typing import Optional, Dict, Any

from agent.omni_ad.utils.config import load_config

# ---------------------------------------------------------------------------
# 路径解析（代码审计修复）
# ---------------------------------------------------------------------------
# 原实现把默认路径写成相对路径：
#     "agent/omni_ad/weights/student_best.pth"
# 它按**当前工作目录**解析。而 start_server.bat 会先 `cd agent`，
# 于是解析成 <root>\agent\agent\omni_ad\weights\student_best.pth —— 不存在。
# 后果：init_predictor() 恒返回 False，缺陷检测分支失效、合规分支的
# anomaly_score 恒为 None（OOD 分量恒 0），即"创新②"从未真正参与计算。
#
# 现改为基于本文件位置的绝对路径，与 CWD 解耦。
_HERE = os.path.dirname(os.path.abspath(__file__))          # .../agent/omni_ad
_DEFAULT_WEIGHTS = os.path.join(_HERE, "weights")
_DEFAULT_CONFIGS = os.path.join(_HERE, "configs")


def _resolve(path: str, default: str) -> str:
    """把可能是相对路径的入参解析为绝对路径。

    先在 CWD 下找（保持向后兼容：从项目根运行时传
    "agent/omni_ad/weights/x.pth" 仍可用），找不到则退回基于
    本文件位置的默认路径。
    """
    if path and os.path.isabs(path):
        return path
    if path and os.path.exists(path):
        return os.path.abspath(path)
    return default


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_predictor: Optional[Any] = None
_initialized: bool = False
_init_error: Optional[str] = None
_model_identity: dict = {}
# 审计修复：失败也要缓存。原实现只在成功时置 _initialized=True，
# 于是路径错误/设备不可用时，**每次调用**都会重走一遍
# os.path.exists -> load_config ->（路径修好后还会 torch.load 42MB 权重）。
_init_attempted: bool = False
# v4 维护：线程安全单例锁（防止三元 Agent 并发时重复初始化）
_predictor_lock = threading.Lock()


def init_predictor(
    checkpoint_path: str = "",
    memory_bank_path: str = "",
    config_path: str = "",
    device: str = "cuda",
) -> bool:
    """初始化全局 AnomalyPredictor（启动时调用一次，线程安全）。

    v4 维护：加锁防止三元 Agent 并发时重复初始化。
    审计修复：
      1) 默认路径改为基于本文件位置的绝对路径（不再依赖 CWD）；
      2) 失败结果也缓存（_init_attempted），避免每个请求重复探测/重复加载；
      3) device="cuda" 但 CUDA 不可用时自动降级 cpu，并记录提示。

    加载顺序：config → student checkpoint → memory bank → 组装 predictor。
    失败时设置 _init_error 并返回 False，不抛异常。

    Args:
        checkpoint_path:  学生模型 .pth 路径（空 = 用默认绝对路径）
        memory_bank_path: 记忆库 .pkl 路径（空 = 用默认绝对路径）
        config_path:      YAML 配置路径（空 = 用默认绝对路径）
        device:           推理设备（"cuda" 或 "cpu"）

    Returns:
        True 表示初始化成功，False 表示失败（原因见 _init_error）

    对应文档: v4 变更记录 #7 - run_inference 单例保护
    """
    global _predictor, _initialized, _init_error, _init_attempted, _model_identity

    # 快速路径：已成功初始化
    if _initialized and _predictor is not None:
        return True
    # 快速路径：已失败过（负缓存），直接复用上次的错误，不再重复探测
    if _init_attempted and not _initialized:
        return False

    # 整个初始化过程必须在锁内执行（含 load config/权重/predictor），
    # 否则并发线程会同时进入初始化，造成重复加载模型（内存翻倍）甚至竞态
    with _predictor_lock:
        if _initialized and _predictor is not None:   # double-check
            return True
        if _init_attempted and not _initialized:
            return False

        def _fail(msg: str) -> bool:
            """统一失败出口：记录错误 + 置负缓存"""
            global _init_error, _init_attempted
            _init_error = msg
            _init_attempted = True
            print(f"[Omni-AD] 初始化失败：{msg}")
            return False

        # 0. 解析路径
        ckpt = _resolve(checkpoint_path,
                        os.path.join(_DEFAULT_WEIGHTS, "student_best.pth"))
        mbank = _resolve(memory_bank_path,
                         os.path.join(_DEFAULT_WEIGHTS, "memory_bank.pkl"))
        cfg_p = _resolve(config_path,
                         os.path.join(_DEFAULT_CONFIGS, "equis_inference.yaml"))

        # 0b. 设备可用性：请求 cuda 但不可用时降级 cpu，而不是直接失败
        want_device = device
        if str(device).startswith("cuda"):
            try:
                import torch
                if not torch.cuda.is_available():
                    want_device = "cpu"
                    print("[Omni-AD] 未检测到可用 CUDA，自动降级为 CPU 推理"
                          "（延迟会显著上升）")
            except Exception:
                want_device = "cpu"
                print("[Omni-AD] torch 不可用，降级为 CPU 推理")

        # 1. Load config
        if not os.path.exists(cfg_p):
            return _fail(f"Config not found: {cfg_p}")
        try:
            cfg = load_config(cfg_p)
        except Exception as e:
            return _fail(f"Failed to load config ({cfg_p}): {e}")

        # 2. Check weight files exist
        for path, name in ((ckpt, "checkpoint"), (mbank, "memory bank")):
            if not os.path.exists(path):
                return _fail(f"{name} not found: {path}")

        # 3. Build predictor
        try:
            from agent.omni_ad.inference.predictor import load_predictor
            _predictor = load_predictor(
                config=cfg,
                checkpoint_path=ckpt,
                memory_bank_path=mbank,
                device=want_device,
            )
            _model_identity = {}
            for name, path in (("checkpoint_sha256",ckpt),("memory_bank_sha256",mbank),
                               ("config_sha256",cfg_p)):
                with open(path,"rb") as stream:
                    _model_identity[name] = hashlib.file_digest(stream,"sha256").hexdigest()
            _initialized = True
            _init_attempted = True
            print(f"[Omni-AD] 初始化成功（device={want_device}）")
            return True
        except Exception as e:
            return _fail(f"Failed to load predictor: {type(e).__name__}: {e}")


def init_status() -> dict:
    """诊断用：返回当前初始化状态（供 /api/compliance 或排障使用）"""
    return {
        "initialized": _initialized,
        "attempted": _init_attempted,
        "error": _init_error,
        "weights_dir": _DEFAULT_WEIGHTS,
        "model_identity": dict(_model_identity),
    }


def reset_predictor() -> None:
    """清空单例与负缓存（权重更新后 / 测试用）"""
    global _predictor, _initialized, _init_error, _init_attempted, _model_identity
    with _predictor_lock:
        _predictor = None
        _initialized = False
        _init_error = None
        _init_attempted = False
        _model_identity = {}


def _detection_error(message, *, status="unavailable", code="detection_unavailable"):
    return {"success":False,"execution_status":status,"anomaly_score":None,
            "is_anomaly":None,"verdict":"未完成异常检测","class_used":None,
            "error":str(message),"error_code":code}


def detect_anomaly(
    image_bgr: np.ndarray,
    class_name: Optional[str] = None,
) -> Dict[str, Any]:
    """对单张 BGR 图像做异常检测。

    接受 OpenCV 摄像头直出的 (H, W, 3) uint8 BGR 格式，内部自动做：
      BGR → RGB → resize(448) → ImageNet 归一化 → 推理。

    Args:
        image_bgr:  (H, W, 3) uint8 BGR 图像（OpenCV 默认格式）
        class_name: 已建库的明确类别；缺少或未知类别均不能回退到其他产品。

    Returns:
        {
            "success": bool,
            "anomaly_score": float | None, # raw KNN score；不可用时不返回伪造的0
            "is_anomaly": bool | None,    # score > 实际配置阈值；不表示工件合格
            "verdict": str,              # 中文判定文字
            "class_used": str,           # 实际使用的记忆库类别
            "error": str | None,         # 错误信息（success=False 时填充）
        }
    """
    global _predictor, _initialized, _init_error

    if not isinstance(class_name, str) or not class_name.strip() or class_name.strip() == "default":
        return _detection_error("请指定已建库的工件类别 class_id/class_name",
                                status="needs_input",code="missing_class")
    class_name = class_name.strip()
    if (not isinstance(image_bgr,np.ndarray) or image_bgr.size == 0
            or image_bgr.ndim != 3 or image_bgr.shape[2] != 3 or image_bgr.dtype != np.uint8):
        return _detection_error("图像必须是非空 H×W×3 uint8 BGR 数组",
                                status="invalid_input",code="invalid_image")

    # Ensure predictor is initialized
    if not _initialized:
        ok = init_predictor()
        if not ok:
            return _detection_error(_init_error or "检测引擎未就绪")

    # Explicit unsupported class: never compare against another product's prototypes.
    if not _predictor.memory_bank.has_class(class_name):
        available = list(_predictor.memory_bank.prototypes.keys())
        return {**_detection_error("该工件类别尚未建库: "+class_name,
                                  status="unsupported",code="class_not_enrolled"),
                "requested_class":class_name,"supported_classes":available}
    effective_class = class_name

    # BGR → RGB conversion (OpenCV default is BGR)
    # Run inference
    try:
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        result = _predictor.predict_numpy(
            image_rgb,
            class_name=effective_class,
            return_heatmap=False,  # Agent 不需要热力图
        )
    except Exception as e:
        return _detection_error("推理异常: "+str(e),code="inference_failed")

    # 防御：predictor 可能返回缺少 image_score 的异常结果
    if not isinstance(result, dict) or "image_score" not in result:
        return _detection_error("推理结果缺少 image_score",code="invalid_model_output")
    try:
        score = float(result["image_score"])
    except (TypeError,ValueError,OverflowError):
        return _detection_error("推理分数不是有效数值",code="invalid_model_output")
    if not math.isfinite(score):
        return _detection_error("推理分数不是有限数值",code="invalid_model_output")
    # Raw KNN score = 1 - mean(cos_sim / τ) with τ=0.1.
    # Range: ≈-10 (cos≈1.0, normal) to +1 (cos≈0, anomalous).
    # 阈值 -4.0 ≈ cos_sim 0.5：与正常原型的相似度低于 50% 判为异常。
    #
    # P5-45：阈值改为"单一来源"。原先后端写死 -4.0，前端 cards.js 也**各自
    # 写死** -4.0 用于绘制刻度；后端一改配置，前端就在静默撒谎。
    # 现从 configs/equis_inference.yaml 的 inference.threshold 读取（带默认值），
    # 并在返回值里下发，供前端使用。
    threshold = _get_threshold()
    if not math.isfinite(threshold):
        return _detection_error("异常阈值不是有限数值",code="invalid_threshold")

    return {
        "success": True,
        "execution_status": "success",
        "anomaly_score": score,  # keep the physical-research input unrounded
        # R4: the deployed -4 cutoff missed all 1258 MVTec test anomalies.
        # Keep raw scores available, but a candidate research cutoff is not a calibrated decision.
        "threshold": None,
        "legacy_threshold": threshold,
        "decision_execution_status": "needs_calibration",
        "decision_policy": "score_only_pending_calibration",
        "is_anomaly": None,
        "verdict": "视觉分数已计算；阈值待标定，暂不作正常/异常二分结论",
        "class_used": effective_class,
        "error": None,
        "score_protocol": {"definition":"1-mean(topk(cosine/temperature))",
                           "top_k":_predictor.memory_bank.top_k,
                           "temperature":_predictor.memory_bank.temperature},
        "model_identity": dict(_model_identity),
    }


# ── 阈值单一来源（P5-45）────────────────────────────────────

_DEFAULT_THRESHOLD = -4.0
_threshold_cache = None
_threshold_lock = threading.Lock()


def _get_threshold() -> float:
    """从推理配置读取异常阈值；配置不可用时回退内置默认值。

    对应 equis_inference.yaml 的 inference.threshold。
    读取失败不抛异常 —— 检测功能不应因为配置问题而中断。
    """
    global _threshold_cache
    if _threshold_cache is not None:
        return _threshold_cache
    with _threshold_lock:
        if _threshold_cache is not None:
            return _threshold_cache
        val = _DEFAULT_THRESHOLD
        try:
            cfg_path = os.path.join(_DEFAULT_CONFIGS, "equis_inference.yaml")
            if os.path.exists(cfg_path):
                cfg = load_config(cfg_path)
                t = cfg.get("inference", None)
                if t is not None:
                    got = t.get("threshold", None)
                    if got is not None:
                        val = float(got)
                        print(f"[Omni-AD] 异常阈值来自配置: {val}")
        except Exception as e:
            print(f"[Omni-AD] 阈值读取失败({type(e).__name__})，使用默认 {_DEFAULT_THRESHOLD}")
        _threshold_cache = val
        return val


def get_threshold() -> float:
    """公开访问器（供 /api/compliance 或前端查询）"""
    return _get_threshold()
