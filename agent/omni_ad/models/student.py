"""
PVT-v2 (Pyramid Vision Transformer) Student Model.

A pure Transformer student — unlike MobileViT (CNN+Transformer hybrid),
PVT-v2 is a fully Transformer-based architecture, qualifying for the
competition's "+5 Transformer architecture bonus".

Architecture: PVT-v2-B0 backbone → 1x1 Conv projection
Output: (B, H'xW', feature_dim)

Key advantages over MobileViT:
1. Pure Transformer  → +5 innovation bonus
2. Input-flexible    → accepts any resolution (no fixed pos embed)
3. Multi-scale       → hierarchical features like CNNs but all Transformer
4. Lightweight       → PVT-v2-B0: 3.4M params, ~36ms/img on RTX 4060

Reference:
  Wang et al., "PVT v2: Improved Baselines with Pyramid Vision
  Transformer", CVMJ 2022.
"""

import torch
import torch.nn as nn
import timm
from typing import Optional


class PVTStudent(nn.Module):
    """Pure Transformer student for feature distillation.

    Args:
        feature_dim:    Output feature dimension (must match teacher = 384)
        variant:        Model scale — "b0", "b1", "b2"
        pretrained:     Load ImageNet pretrained weights
        dropout:        Dropout rate
        num_classes:    Number of classes for Class Embedding (0 = disabled)
    """

    # Available PVT-v2 variants
    VARIANTS = {
        "b0": "pvt_v2_b0",       # 3.4M params, 256-dim internal
        "b1": "pvt_v2_b1",       # 13.5M params, 512-dim internal
        "b2": "pvt_v2_b2_li",    # 22.6M params, 512-dim internal
    }

    def __init__(
        self,
        feature_dim: int = 384,
        variant: str = "b0",
        pretrained: bool = True,
        dropout: float = 0.1,
        num_classes: int = 0,
    ):
        super().__init__()

        self.feature_dim = feature_dim
        self.variant = variant
        self.num_classes = num_classes

        if variant not in self.VARIANTS:
            raise ValueError(
                f"Unknown variant '{variant}'. "
                f"Choose from: {list(self.VARIANTS.keys())}"
            )

        model_name = self.VARIANTS[variant]

        try:
            self.backbone = timm.create_model(
                model_name,
                pretrained=pretrained,
                features_only=True,
                out_indices=[-2],  # Stage 3 (stride 16) → 28x28 @448px,
                                   # finer than stage 4's 14x14 for pixel-level F1
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to create PVT-v2 model '{model_name}'. "
                f"Error: {e}"
            )

        # Determine backbone output channels
        dummy = torch.randn(1, 3, 448, 448)
        with torch.no_grad():
            feats = self.backbone(dummy)
            backbone_dim = feats[-1].shape[1]
            self.spatial_hw = (feats[-1].shape[2], feats[-1].shape[3])

        # Projection to teacher feature dimension
        self.projection = nn.Sequential(
            nn.Conv2d(backbone_dim, feature_dim, kernel_size=1),
            nn.BatchNorm2d(feature_dim),
            nn.GELU(),
            nn.Dropout2d(dropout) if dropout > 0 else nn.Identity(),
        )

        # Class Embedding: learnable per-class vectors added to patch features.
        # Enables a single student to handle multiple classes by conditioning
        # the feature space on class identity. num_classes=0 disables it.
        if num_classes > 0:
            self.class_embedding = nn.Embedding(num_classes, feature_dim)
            nn.init.trunc_normal_(self.class_embedding.weight, std=0.02)
        else:
            self.class_embedding = None

        n_params = sum(p.numel() for p in self.parameters()) / 1e6
        emb_info = f", class_emb={num_classes}" if num_classes > 0 else ""
        print(
            f"[Student] PVT-v2-{variant}: "
            f"{n_params:.1f}M params, "
            f"backbone_dim={backbone_dim} → feature_dim={feature_dim}, "
            f"spatial={self.spatial_hw}{emb_info}"
        )

    def forward(self, x: torch.Tensor, class_idx: Optional[torch.Tensor] = None) -> torch.Tensor:
        """Extract features from input images.

        v4 维护：推理路径用 torch.no_grad 包裹，减少内存占用与计算开销

        Args:
            x:         (B, 3, H, W) normalized image tensor
            class_idx: (B,) class indices for Class Embedding conditioning.
                       If None and class_embedding is active, defaults to class 0.

        Returns:
            features: (B, H'xW', feature_dim) patch features

        对应文档: v4 变更记录 #8 - 推理路径 torch.no_grad
        """
        with torch.no_grad():
            self.eval()
            return self._forward_impl(x, class_idx)

    def _forward_impl(self, x: torch.Tensor, class_idx: Optional[torch.Tensor] = None) -> torch.Tensor:
        """forward 的实际实现（被 no_grad 包裹）"""
        feats = self.backbone(x)[-1]                # (B, C_bb, H', W')
        projected = self.projection(feats)           # (B, feature_dim, H', W')
        B, C, H, W = projected.shape
        projected = projected.flatten(2).transpose(1, 2)  # (B, H'*W', C)

        # Add class embedding to all patches (broadcast)
        if self.class_embedding is not None:
            if class_idx is None:
                # Fallback: use class 0 when no class info provided
                class_idx = torch.zeros(B, dtype=torch.long, device=x.device)
            cls_emb = self.class_embedding(class_idx)        # (B, feature_dim)
            projected = projected + cls_emb.unsqueeze(1)      # (B, N, feature_dim)

        return projected

    def get_spatial_dims(self, image_size: int = 448) -> tuple:
        """Get output spatial dimensions."""
        dummy = torch.randn(1, 3, image_size, image_size)
        with torch.no_grad():
            feats = self.backbone(dummy)[-1]
            return feats.shape[2], feats.shape[3]
