import math
from dataclasses import dataclass

import numpy as np
import torch
import torch.nn.functional as F
from diff_gaussian_rasterization import (
    GaussianRasterizationSettings,
    GaussianRasterizer,
)

import threestudio
from threestudio.models.background.base import BaseBackground
from threestudio.models.geometry.base import BaseGeometry
from threestudio.models.materials.base import BaseMaterial
from threestudio.models.renderers.base import Rasterizer
from threestudio.utils.typing import *


@threestudio.register("dynamic-gaussian-rasterizer")
class DynamicGaussianRasterizer(Rasterizer):
    @dataclass
    class Config(Rasterizer.Config):
        debug: bool = False
        invert_bg_prob: float = 1
        near: float = 0.01
        far: float = 100.0

    cfg: Config

    def configure(
        self,
        geometry: BaseGeometry,
        material: BaseMaterial,
        background: BaseBackground,
    ) -> None:
        super().configure(geometry, material, background)

    def forward(
        self,
        viewpoint_camera,
        moment,
        bg_color: torch.Tensor,
        scaling_modifier=1.0,
        override_color=None,
    ) -> Dict[str, Any]:
        """
        Render the scene.

        Background tensor (bg_color) must be on GPU!
        """

        if self.training:
            invert_bg_color = np.random.rand() > self.cfg.invert_bg_prob
        else:
            invert_bg_color = False

        bg_color = bg_color if not invert_bg_color else (1.0 - bg_color)

        dynamic_gaussian = self.geometry.dynamic_flow

        pc = self.geometry.gaussian
        # Create zero tensor. We will use it to make pytorch return gradients of the 2D (screen-space) means
        screenspace_points = (
            torch.zeros_like(
                pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda"
            )
            + 0
        )
        try:
            screenspace_points.retain_grad()
        except:
            pass

        # Set up rasterization configuration
        tanfovx = math.tan(viewpoint_camera.FoVx * 0.5)
        tanfovy = math.tan(viewpoint_camera.FoVy * 0.5)

        raster_settings = GaussianRasterizationSettings(
            image_height=int(viewpoint_camera.image_height),
            image_width=int(viewpoint_camera.image_width),
            tanfovx=tanfovx,
            tanfovy=tanfovy,
            bg=bg_color,
            scale_modifier=scaling_modifier,
            viewmatrix=viewpoint_camera.world_view_transform,
            projmatrix=viewpoint_camera.full_proj_transform,
            sh_degree=pc.active_sh_degree,
            campos=viewpoint_camera.camera_center,
            prefiltered=False,
            debug=False,
        )

        rasterizer = GaussianRasterizer(raster_settings=raster_settings)

        means3D = pc.get_xyz
        means2D = screenspace_points
        opacity = pc.get_opacity

        # If precomputed 3d covariance is provided, use it. If not, then it will be computed from
        # scaling / rotation by the rasterizer.
        scales = None
        rotations = None
        cov3D_precomp = None
        scales = pc.get_scaling
        rotations = pc._rotation

        if moment.item() > 1e-6:
            dynamic_feature = dynamic_gaussian(means3D, moment.item())
            dynamic_means3D = dynamic_feature["features"][..., :3]
            dynamic_rotations = dynamic_feature["features"][..., 3:]
        else:
            dynamic_means3D = torch.zeros_like(means3D)
            dynamic_rotations = torch.zeros_like(rotations)
        rotations = self.geometry.gaussian.rotation_activation(
            rotations + dynamic_rotations
        )
        means3D = means3D + dynamic_means3D

        # If precomputed colors are provided, use them. Otherwise, if it is desired to precompute colors
        # from SHs in Python, do it. If not, then SH -> RGB conversion will be done by rasterizer.
        shs = None
        colors_precomp = None
        if override_color is None:
            shs = pc.get_features
        else:
            colors_precomp = override_color

        # Rasterize visible Gaussians to image, obtain their radii (on screen).
        rendered_image, radii = rasterizer(
            means3D=means3D,
            means2D=means2D,
            shs=shs,
            colors_precomp=colors_precomp,
            opacities=opacity,
            scales=scales,
            rotations=rotations,
            cov3D_precomp=cov3D_precomp,
        )

        refine_image = self.geometry.refine_net(rendered_image.unsqueeze(0).detach())
        return {
            "render": rendered_image,
            "refine": refine_image[0],
            "viewspace_points": screenspace_points,
            "visibility_filter": radii > 0,
            "radii": radii,
            "bg_color": bg_color,
        }