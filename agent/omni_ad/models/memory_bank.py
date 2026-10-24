"""
Token Prototype Memory Bank.

Compresses millions of patch-level features from normal training images
into compact prototype sets via MiniBatchKMeans clustering. At inference,
anomaly scores are computed as distances to nearest prototypes.

Design:
- One set of prototypes per class (256 prototypes each by default)
- Cosine similarity for distance metric
- Top-K nearest neighbor aggregation for anomaly scoring
- Persistence via pickle for fast reload

Reference:
  Roth et al., "Towards Total Recall in Industrial Anomaly Detection",
  CVPR 2022. (PatchCore inspiration — extended to multi-class Omni-AD)
"""

import pickle
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.cluster import MiniBatchKMeans


class PrototypeMemoryBank:
    """Prototype-based memory bank for efficient anomaly scoring.

    Args:
        n_prototypes: Number of cluster centers per class (default: 256)
        feature_dim:  Feature vector dimensionality
        top_k:        Number of nearest neighbors for anomaly scoring
        temperature:  Temperature for cosine similarity scaling
        random_state: Seed for KMeans reproducibility
    """

    def __init__(
        self,
        n_prototypes: int = 256,
        feature_dim: int = 384,
        top_k: int = 5,
        temperature: float = 0.1,
        random_state: int = 42,
    ):
        self.n_prototypes = n_prototypes
        self.feature_dim = feature_dim
        self.top_k = top_k
        self.temperature = temperature
        self.random_state = random_state

        # prototypes[class_name] = torch.Tensor (K, feature_dim)
        self.prototypes: Dict[str, torch.Tensor] = {}

    # ----------------------------------------------------------
    # Building
    # ----------------------------------------------------------
    def build(
        self,
        features: Dict[str, torch.Tensor],
        kmeans_batch_size: int = 1024,
        kmeans_n_init: int = 3,
        kmeans_iters: int = 100,
        verbose: bool = True,
    ):
        """Build memory bank from extracted features.

        For each class, runs MiniBatchKMeans on patch features to
        produce n_prototypes cluster centers.

        Args:
            features: Dict[class_name → Tensor(N_patches, feature_dim)]
            kmeans_batch_size: MiniBatch size for KMeans
            kmeans_n_init:     Number of random initializations
            kmeans_iters:      Maximum KMeans iterations
            verbose:           Print progress

        Examples:
            >>> bank = PrototypeMemoryBank()
            >>> features = {"bottle": torch.randn(50000, 384)}
            >>> bank.build(features)
        """
        for class_name, feats in features.items():
            # Convert to numpy
            if isinstance(feats, torch.Tensor):
                feats_np = feats.detach().cpu().numpy()
            else:
                feats_np = feats

            n_proto = min(self.n_prototypes, len(feats_np))

            if verbose:
                print(f"  [{class_name}] Clustering {feats_np.shape[0]} "
                      f"features → {n_proto} prototypes...")

            kmeans = MiniBatchKMeans(
                n_clusters=n_proto,
                random_state=self.random_state,
                batch_size=min(kmeans_batch_size, len(feats_np)),
                n_init=kmeans_n_init,
                max_iter=kmeans_iters,
            )
            kmeans.fit(feats_np)

            centers = torch.from_numpy(kmeans.cluster_centers_).float()
            self.prototypes[class_name] = centers

            if verbose:
                print(f"  [{class_name}] Done. "
                      f"Prototypes shape: {centers.shape}")

    def build_single(
        self,
        class_name: str,
        features: torch.Tensor,
        **kwargs,
    ):
        """Build memory bank for a single class (convenience wrapper)."""
        self.build({class_name: features}, **kwargs)

    # ----------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------
    def save(self, path: str):
        """Save memory bank to disk (pickle format)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "prototypes": {k: v.cpu().numpy() for k, v in self.prototypes.items()},
            "n_prototypes": self.n_prototypes,
            "feature_dim": self.feature_dim,
            "top_k": self.top_k,
            "temperature": self.temperature,
            "random_state": self.random_state,
        }

        with open(path, "wb") as f:
            pickle.dump(data, f)

        print(f"[MemoryBank] Saved {len(self.prototypes)} classes to {path}")

    def load(self, path: str):
        """Load memory bank from disk."""
        with open(path, "rb") as f:
            data = pickle.load(f)

        self.prototypes = {
            k: torch.from_numpy(v).float()
            for k, v in data["prototypes"].items()
        }
        self.n_prototypes = data.get("n_prototypes", self.n_prototypes)
        self.feature_dim = data.get("feature_dim", self.feature_dim)
        self.top_k = data.get("top_k", self.top_k)
        self.temperature = data.get("temperature", self.temperature)

        print(f"[MemoryBank] Loaded {len(self.prototypes)} classes from {path}")

    # ----------------------------------------------------------
    # Inference
    # ----------------------------------------------------------
    def compute_similarity(
        self,
        query_features: torch.Tensor,
        class_name: str,
    ) -> torch.Tensor:
        """Compute cosine similarity between queries and prototypes.

        v4 维护：推理路径用 torch.no_grad 包裹

        Args:
            query_features: (B, N, feature_dim)
            class_name:     Which class prototypes to compare against

        Returns:
            similarity: (B, N, K) cosine similarity scores

        对应文档: v4 变更记录 #8 - 推理路径 torch.no_grad
        """
        with torch.no_grad():
            return self._compute_similarity_impl(query_features, class_name)

    def _compute_similarity_impl(
        self,
        query_features: torch.Tensor,
        class_name: str,
    ) -> torch.Tensor:
        """compute_similarity 的实际实现（被 no_grad 包裹）"""
        prototypes = self.prototypes[class_name].to(query_features.device)

        # L2 normalize
        query_norm = F.normalize(query_features, dim=-1)
        proto_norm = F.normalize(prototypes, dim=-1)

        # Cosine similarity with temperature scaling.
        # Without temperature, cosine values cluster in [-1, 1] → poor
        # discrimination between "a bit similar" and "very similar".
        # Dividing by a small τ (e.g. 0.1) stretches the dynamic range
        # to [-10, 10], giving sharper top-k separation and meaningful
        # anomaly scores.
        similarity = torch.matmul(query_norm, proto_norm.T)  # (B, N, K)
        similarity = similarity / self.temperature

        return similarity

    def compute_anomaly_score(
        self,
        query_features: torch.Tensor,
        class_name: str,
        aggregation: str = "topk_mean",
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Compute anomaly scores for query features.

        Higher score = more anomalous.
        Score = 1 - mean_cosine_similarity_to_top_k_nearest_prototypes

        Args:
            query_features: (B, N, D) patch features from student
            class_name:     Class for prototype lookup
            aggregation:   "max" or "mean" for image-level aggregation

        Returns:
            image_scores: (B,)  — image-level anomaly scores
            patch_scores: (B, N) — per-patch anomaly scores
        """
        similarity = self.compute_similarity(query_features, class_name)

        # Top-K nearest neighbors (cap at available prototypes)
        k = min(self.top_k, similarity.shape[-1])
        topk_sim, _ = similarity.topk(k, dim=-1)  # (B, N, K)

        # Patch scores: 1 - mean similarity (lower sim → higher anomaly)
        patch_scores = 1.0 - topk_sim.mean(dim=-1)  # (B, N)

        # Image-level score
        if aggregation == "max":
            # Maximum anomaly across all patches (sensitive to noise)
            image_scores = patch_scores.max(dim=1)[0]  # (B,)
        elif aggregation == "mean":
            # Mean anomaly (diluted by many normal patches)
            image_scores = patch_scores.mean(dim=1)  # (B,)
        elif aggregation == "topk_mean":
            # Mean of top-10% anomaly scores (robust, recommended)
            k_img = max(1, patch_scores.shape[1] // 10)
            image_scores = patch_scores.topk(k_img, dim=1)[0].mean(dim=1)  # (B,)
        else:
            raise ValueError(f"Unknown aggregation: {aggregation}")

        return image_scores, patch_scores

    # ----------------------------------------------------------
    # Utility
    # ----------------------------------------------------------
    @property
    def num_classes(self) -> int:
        return len(self.prototypes)

    @property
    def total_prototypes(self) -> int:
        return sum(v.shape[0] for v in self.prototypes.values())

    def has_class(self, class_name: str) -> bool:
        return class_name in self.prototypes

    def get_prototypes(self, class_name: str) -> torch.Tensor:
        """Get prototype tensor for a class (for contrastive loss)."""
        if class_name not in self.prototypes:
            raise KeyError(
                f"Class '{class_name}' not in memory bank. "
                f"Available: {list(self.prototypes.keys())}"
            )
        return self.prototypes[class_name]

    def __repr__(self) -> str:
        classes = list(self.prototypes.keys())
        return (
            f"PrototypeMemoryBank("
            f"classes={len(classes)}, "
            f"prototypes={self.total_prototypes}, "
            f"dim={self.feature_dim}, "
            f"top_k={self.top_k})"
        )
