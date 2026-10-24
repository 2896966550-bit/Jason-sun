"""
Configuration system for Omni-AD Anomaly Detection.
Centralized YAML-based config with dot-access for all hyperparameters.

Usage:
    from utils.config import load_config
    cfg = load_config("configs/default.yaml")
    print(cfg.model.teacher.feature_dim)  # -> 384
"""

import os
import yaml
from pathlib import Path
from typing import Any, Dict, Optional


class Config:
    """Dot-accessible configuration object.

    Recursively converts nested dicts into nested Config objects,
    enabling clean attribute access: cfg.model.teacher.name
    """

    def __init__(self, config_dict: Dict[str, Any]):
        for key, value in config_dict.items():
            if isinstance(value, dict):
                value = Config(value)
            elif isinstance(value, list):
                # Keep lists as-is (don't wrap elements)
                pass
            setattr(self, key, value)

    def to_dict(self) -> Dict[str, Any]:
        """Convert back to plain dict for serialization."""
        result = {}
        for key, value in self.__dict__.items():
            if key.startswith("_"):
                continue
            if isinstance(value, Config):
                result[key] = value.to_dict()
            else:
                result[key] = value
        return result

    def get(self, key: str, default: Any = None) -> Any:
        """Safe getter with default value."""
        return getattr(self, key, default)

    def __repr__(self) -> str:
        return f"Config({self.to_dict()})"


def _derive_paths(config: Config) -> Config:
    """Auto-derive output paths from experiment settings.

    Gracefully skips derivation when `data` or `experiment` sections are
    absent (e.g. inference-only configs that do not define those sections).
    """
    data = getattr(config, "data", None)
    if data is not None:
        root = Path(data.get("root", "./data/Omni-AD"))
        if not hasattr(data, "train_dir"):
            data.train_dir = str(root / data.get("train_subdir", "train"))
        if not hasattr(data, "test_dir"):
            data.test_dir = str(root / data.get("test_subdir", "test"))
        if not hasattr(data, "mask_dir"):
            data.mask_dir = str(root / data.get("mask_subdir", "ground_truth"))

    exp = getattr(config, "experiment", None)
    if exp is not None:
        output_root = Path(exp.get("output_dir", "./outputs"))
        exp_name = exp.get("name", "omni_ad_baseline")
        config.output_dir = str(output_root / exp_name)
        config.checkpoint_dir = str(Path(config.output_dir) / "checkpoints")
        config.log_dir = str(Path(config.output_dir) / "logs")
        config.memory_bank_dir = str(Path(config.output_dir) / "memory_bank")

    return config


def load_config(config_path: str) -> Config:
    """Load YAML configuration file.

    Args:
        config_path: Path to .yaml config file

    Returns:
        Config object with dot-accessible attributes
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config_dict = yaml.safe_load(f)

    config = Config(config_dict)
    config._config_path = os.path.abspath(config_path)

    # Auto-derive paths
    config = _derive_paths(config)

    return config
