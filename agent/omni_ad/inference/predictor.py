"""
Anomaly Predictor — Inference Engine.

Loads a trained student model + memory bank and computes
anomaly scores, heatmaps, and predictions for test images.

Pipeline:
1. Student extracts patch features from test image
2. Memory bank computes cosine similarity to normal prototypes
3. Anomaly score = 1 - top-K similarity (higher = more anomalous)
4. Optional: upsample patch heatmap to original resolution
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import cv2

from agent.omni_ad.models.memory_bank import PrototypeMemoryBank
from agent.omni_ad.models.student import PVTStudent


class AnomalyPredictor:
    """Anomaly detection predictor for inference.

    Args:
        student:     Trained PVT-v2 student model
        memory_bank: Built prototype memory bank
        device:      "cuda" or "cpu"
        image_size:  Input image size (pixels, square)
        output_size: Output heatmap size (defaults to image_size)
    """

    def __init__(
        self,
        student: nn.Module,
        memory_bank: PrototypeMemoryBank,
        device: str = "cuda",
        image_size: int = 448,
        output_size: Optional[Tuple[int, int]] = None,
        students: Optional[List[nn.Module]] = None,
        memory_banks: Optional[List[PrototypeMemoryBank]] = None,
        class_to_idx: Optional[Dict[str, int]] = None,
    ):
        self.device = torch.device(device)
        self.image_size = image_size
        self.output_size = output_size or (image_size, image_size)
        self.class_to_idx = class_to_idx or {}  # class_name → class_id

        # Ensemble support: a list of (student, memory_bank) pairs whose
        # anomaly scores are averaged at inference. Single-model usage passes
        # `student`/`memory_bank`, wrapped into length-1 lists here so the
        # rest of the code has one uniform code path.
        if students is not None and memory_banks is not None:
            if len(students) != len(memory_banks):
                raise ValueError("students and memory_banks must have equal length")
            self.students = [s.to(self.device).eval() for s in students]
            self.memory_banks = list(memory_banks)
        else:
            self.students = [student.to(self.device).eval()]
            self.memory_banks = [memory_bank]

        # Backward-compatible handles (eval.py uses predictor.memory_bank.has_class)
        self.student = self.students[0]
        self.memory_bank = self.memory_banks[0]

    def _get_class_idx(self, class_name: str) -> Optional[torch.Tensor]:
        """Convert class name to class_idx tensor for Class Embedding.

        Returns None if class_to_idx is empty (no Class Embedding used).
        """
        if not self.class_to_idx:
            return None
        idx = self.class_to_idx.get(class_name, 0)
        return torch.tensor([idx], dtype=torch.long, device=self.device)

    @torch.no_grad()
    def predict(
        self,
        image: torch.Tensor,
        class_name: str,
        return_heatmap: bool = True,
        aggregation: str = "topk_mean",
    ) -> Dict[str, torch.Tensor]:
        """Predict anomaly for a single image.

        Args:
            image:        (1, 3, H, W) normalized tensor
            class_name:   Product class for prototype lookup
            return_heatmap: Generate anomaly heatmap
            aggregation:  "max" or "mean" for image-level score

        Returns:
            Dict with:
                image_score: (1,) anomaly score
                patch_scores: (1, N) per-patch scores
                heatmap: (H_out, W_out) anomaly heatmap [if return_heatmap]
        """
        self.student.eval()

        image = image.to(self.device)
        class_idx = self._get_class_idx(class_name)

        # Ensemble: average anomaly scores across all (student, memory_bank) pairs
        img_scores, patch_scores_list = [], []
        for stu, mb in zip(self.students, self.memory_banks):
            features = stu(image, class_idx)  # (1, N, D) — pass class_idx for Class Embedding
            isc, psc = mb.compute_anomaly_score(
                features, class_name, aggregation=aggregation
            )
            img_scores.append(isc)
            patch_scores_list.append(psc)

        image_score = torch.stack(img_scores, dim=0).mean(dim=0)
        patch_scores = torch.stack(patch_scores_list, dim=0).mean(dim=0)

        result = {
            "image_score": image_score.cpu(),
            "patch_scores": patch_scores.cpu(),
        }

        # 3. Generate heatmap
        if return_heatmap:
            result["heatmap"] = self._scores_to_heatmap(patch_scores)

        return result

    @torch.no_grad()
    def predict_batch(
        self,
        dataloader,
        class_name: str,
        return_heatmap: bool = True,
    ) -> List[Dict]:
        """Predict anomalies for a batch of test images.

        Args:
            dataloader: DataLoader yielding batches with keys:
                        image, path, label, type, mask (optional)
            class_name: Class name for memory bank lookup

        Returns:
            List of dicts, each containing:
                image_score, patch_scores, heatmap, path, label, type, mask
        """
        results = []
        class_idx = self._get_class_idx(class_name)

        for batch in dataloader:
            images = batch["image"].to(self.device)
            B = images.shape[0]

            # Broadcast class_idx to batch size for Class Embedding
            batch_class_idx = class_idx.expand(B) if class_idx is not None else None

            # Ensemble: average anomaly scores across all (student, memory_bank) pairs
            img_scores_list, patch_scores_list = [], []
            for stu, mb in zip(self.students, self.memory_banks):
                features = stu(images, batch_class_idx)  # (B, N, D)
                isc, psc = mb.compute_anomaly_score(features, class_name)
                img_scores_list.append(isc)
                patch_scores_list.append(psc)

            image_scores = torch.stack(img_scores_list, dim=0).mean(dim=0)
            patch_scores = torch.stack(patch_scores_list, dim=0).mean(dim=0)

            # Process each image
            for i in range(B):
                # Handle both list and single-element batch (default collate)
                path = batch["path"][i] if isinstance(batch["path"], (list, tuple)) else str(batch["path"])
                img_type = batch["type"][i] if isinstance(batch["type"], (list, tuple)) else str(batch["type"])

                result = {
                    "image_score": image_scores[i].cpu(),
                    "patch_scores": patch_scores[i].cpu(),
                    "path": path,
                    "label": batch["label"][i].cpu().item(),
                    "type": img_type,
                    "class_name": class_name,
                }

                if "mask" in batch:
                    result["mask"] = batch["mask"][i].cpu()

                if return_heatmap:
                    result["heatmap"] = self._scores_to_heatmap(
                        patch_scores[i:i+1]
                    )

                results.append(result)

        return results

    def _scores_to_heatmap(self, patch_scores: torch.Tensor) -> torch.Tensor:
        """Convert patch-level scores to image-resolution heatmap.

        Args:
            patch_scores: (1, N) or (N,) per-patch anomaly scores

        Returns:
            heatmap: (H_out, W_out) normalized [0, 1] anomaly map
        """
        if patch_scores.dim() == 1:
            patch_scores = patch_scores.unsqueeze(0)

        B, N = patch_scores.shape

        # Try square reshape
        side = int(np.sqrt(N))

        if side * side == N:
            # Perfect square: reshape to (B, 1, side, side) and upsample
            heatmap = patch_scores.reshape(B, 1, side, side)
        else:
            # Non-square: reshape as (B, 1, N, 1) and interpolate
            heatmap = patch_scores.reshape(B, 1, N, 1)

        # Upsample to output size
        heatmap = F.interpolate(
            heatmap,
            size=self.output_size,
            mode="bilinear",
            align_corners=False,
        )  # (B, 1, H_out, W_out)

        heatmap = heatmap.squeeze()  # (H_out, W_out)

        # Min-max normalize to [0, 1]
        h_min, h_max = heatmap.min(), heatmap.max()
        if h_max > h_min:
            heatmap = (heatmap - h_min) / (h_max - h_min)

        return heatmap

    @staticmethod
    def postprocess_heatmap(
        heatmap: torch.Tensor,
        gaussian_sigma: float = 4.0,
        min_area: int = 16,
    ) -> torch.Tensor:
        """Clean up heatmap with Gaussian smoothing + small-region removal.

        Args:
            heatmap:        (H, W) float tensor in [0, 1]
            gaussian_sigma: Sigma for Gaussian blur (higher = smoother)
            min_area:       Minimum connected component area in pixels.
                            Regions smaller than this are zeroed out.

        Returns:
            cleaned: (H, W) float tensor in [0, 1]
        """
        import cv2
        arr = heatmap.cpu().numpy().astype(np.float32)

        # 1. Gaussian smooth to reduce speckle noise
        arr = cv2.GaussianBlur(arr, (0, 0), gaussian_sigma)

        # 2. Threshold → binary mask for component analysis
        _, binary = cv2.threshold(arr, 0.3, 1.0, cv2.THRESH_BINARY)
        binary_u8 = (binary * 255).astype(np.uint8)

        # 3. Find connected components and remove small ones
        n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            binary_u8, connectivity=8
        )
        mask = np.zeros_like(arr)
        for label_id in range(1, n_labels):  # skip background (0)
            if stats[label_id, cv2.CC_STAT_AREA] >= min_area:
                mask[labels == label_id] = 1.0

        # 4. Apply mask: zero out small-region noise, keep large regions
        cleaned = arr * mask

        # 5. Morphological closing to fill small holes
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)

        # Re-normalize to [0, 1]
        c_min, c_max = cleaned.min(), cleaned.max()
        if c_max > c_min:
            cleaned = (cleaned - c_min) / (c_max - c_min)

        return torch.from_numpy(cleaned)

    @torch.no_grad()
    def predict_numpy(
        self,
        image_np: np.ndarray,
        class_name: str,
        norm_mean: Tuple[float, float, float] = (0.485, 0.456, 0.406),
        norm_std: Tuple[float, float, float] = (0.229, 0.224, 0.225),
        return_heatmap: bool = True,
    ) -> Dict[str, np.ndarray]:
        """Convenience method: predict from numpy array.

        Args:
            image_np:  (H, W, 3) uint8 [0, 255] or float32 [0, 1]
            class_name: Class name
            norm_mean:  Normalization mean
            norm_std:   Normalization std

        Returns:
            Dict with: image_score (float), heatmap (H, W) [optional]
        """
        # Preprocess
        if image_np.dtype == np.uint8:
            image_np = image_np.astype(np.float32) / 255.0

        image_np = cv2.resize(image_np, (self.image_size, self.image_size))
        image_np = (image_np - np.array(norm_mean)) / np.array(norm_std)

        image_tensor = torch.from_numpy(image_np).permute(2, 0, 1).unsqueeze(0).float()

        result = self.predict(
            image_tensor, class_name, return_heatmap=return_heatmap
        )

        # Convert to numpy
        output = {"image_score": result["image_score"].item()}
        if return_heatmap:
            output["heatmap"] = result["heatmap"].cpu().numpy()

        return output

    @torch.no_grad()
    def measure_latency(
        self,
        image: torch.Tensor,
        class_name: str = "bottle",
        n_warmup: int = 5,
        n_runs: int = 50,
    ) -> Dict[str, float]:
        """Measure single-image end-to-end inference latency (ms).

        Covers: feature extraction + memory-bank scoring + heatmap generation.

        Returns:
            Dict with mean / p50 / p95 / p99 latency in milliseconds.
        """
        import time
        image = image.to(self.device)

        # Warmup (first few calls are slower due to CUDA init / caching)
        for _ in range(n_warmup):
            self.predict(image, class_name, return_heatmap=True)
        if torch.cuda.is_available():
            torch.cuda.synchronize()

        times = []
        for _ in range(n_runs):
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            t0 = time.perf_counter()
            self.predict(image, class_name, return_heatmap=True)
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            times.append((time.perf_counter() - t0) * 1000)

        times.sort()
        n = len(times)
        return {
            "mean_ms": sum(times) / n,
            "p50_ms": times[n // 2],
            "p95_ms": times[int(n * 0.95)],
            "p99_ms": times[int(n * 0.99)],
            "n_runs": n,
        }


def load_predictor(
    config,
    checkpoint_path: str,
    memory_bank_path: str,
    device: str = "cuda",
    checkpoint_paths: Optional[List[str]] = None,
    memory_bank_paths: Optional[List[str]] = None,
    class_to_idx: Optional[Dict[str, int]] = None,
) -> AnomalyPredictor:
    """Factory function: load predictor from config + checkpoints.

    Args:
        config:            Config object
        checkpoint_path:   Path to student .pth (single-model fallback)
        memory_bank_path:  Path to memory_bank .pkl (single-model fallback)
        device:            "cuda" or "cpu"
        checkpoint_paths:  List of student .pth paths (ensemble mode)
        memory_bank_paths: List of memory_bank .pkl paths (ensemble mode)
        class_to_idx:      {class_name: class_id} mapping for Class Embedding

    Returns:
        AnomalyPredictor ready for inference (ensemble if multiple paths given)
    """
    if checkpoint_paths is None:
        checkpoint_paths = [checkpoint_path]
    if memory_bank_paths is None:
        memory_bank_paths = [memory_bank_path]
    if len(checkpoint_paths) != len(memory_bank_paths):
        raise ValueError("checkpoint_paths and memory_bank_paths must have equal length")

    students, memory_banks = [], []
    map_loc = device if str(device) != "cpu" else "cpu"

    for ckpt_p, mb_p in zip(checkpoint_paths, memory_bank_paths):
        # Load student — num_classes needed to reconstruct class_embedding
        num_classes = len(class_to_idx) if class_to_idx else 0
        student = PVTStudent(
            feature_dim=config.model.student.feature_dim,
            variant=config.model.student.variant,
            pretrained=False,
            dropout=0.0,
            num_classes=num_classes,
        )
        ckpt = torch.load(ckpt_p, map_location=map_loc)
        student.load_state_dict(ckpt["student_state_dict"])
        student.eval()

        # Load memory bank
        mb = PrototypeMemoryBank(
            n_prototypes=config.memory_bank.n_prototypes_per_class,
            feature_dim=config.model.student.feature_dim,
            top_k=config.memory_bank.top_k,
            temperature=config.memory_bank.temperature,
        )
        mb.load(mb_p)

        students.append(student)
        memory_banks.append(mb)

    # Create predictor (ensemble if more than one pair)
    predictor = AnomalyPredictor(
        student=students[0],
        memory_bank=memory_banks[0],
        device=device,
        image_size=config.data.image_size,
        students=students,
        memory_banks=memory_banks,
        class_to_idx=class_to_idx,
    )

    return predictor
