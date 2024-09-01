from dataclasses import dataclass, field
import itertools
import logging as log
from typing import Optional, Union, List, Dict, Sequence, Iterable, Collection, Callable

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import einops

from threestudio.utils.base import Updateable
from threestudio.utils.ops import chunk_batch, scale_tensor
from threestudio.models.networks import CompositeEncoding
from threestudio.utils.typing import *

def contract_to_unisphere_triplane(
    x: Float[Tensor, "... 3"], bbox: Float[Tensor, "2 3"], unbounded: bool = False
) -> Float[Tensor, "... 3"]:
    if unbounded:
        x = scale_tensor(x, bbox, (0, 1))
        x = x * 2 - 1  # aabb is at [-1, 1]
        mag = x.norm(dim=-1, keepdim=True)
        mask = mag.squeeze(-1) > 1
        x[mask] = (2 - 1 / mag[mask]) * (x[mask] / mag[mask])
        x = x / 4 + 0.5  # [-inf, inf] is at [0, 1]
    else:
        x = scale_tensor(x, bbox, (-1, 1))
    return x

def grid_sample_wrapper(grid: torch.Tensor, coords: torch.Tensor, align_corners: bool = True) -> torch.Tensor:
    grid_dim = coords.shape[-1]

    if grid.dim() == grid_dim + 1:
        # no batch dimension present, need to add it
        grid = grid.unsqueeze(0)
    if coords.dim() == 2:
        coords = coords.unsqueeze(0)

    if grid_dim == 2 or grid_dim == 3:
        grid_sampler = F.grid_sample
    else:
        raise NotImplementedError(f"Grid-sample was called with {grid_dim}D data but is only "
                                  f"implemented for 2 and 3D data.")

    coords = coords.view([coords.shape[0]] + [1] * (grid_dim - 1) + list(coords.shape[1:]))
    B, feature_dim = grid.shape[:2]
    n = coords.shape[-2]
    interp = grid_sampler(
        grid,  # [B, feature_dim, reso, ...]
        coords,  # [B, 1, ..., n, grid_dim]
        align_corners=align_corners,
        mode='bilinear', padding_mode='border')
    interp = interp.view(B, feature_dim, n).transpose(-1, -2)  # [B, n, feature_dim]
    interp = interp.squeeze()  # [B?, n, feature_dim?]
    return interp

class KPlane(nn.Module, Updateable):
    def __init__(self, in_channels: int, config: dict):
        super().__init__()
        self.grid_config = config.grid_config
        self.multiscale_res_multipliers = config.multiscale_res or [1]
        if config.concat_features_across_scales:
            self.n_output_dims = self.grid_config.output_coordinate_dim * len(config.multiscale_res)
        else:
            self.n_output_dims = self.grid_config.output_coordinate_dim
        self.n_input_dims = in_channels
        self.concat_features = config.concat_features_across_scales

        self.grids = nn.ModuleList()
        self.feature_dim = 0
        for res in self.multiscale_res_multipliers:
            # initialize coordinate grid
            config = self.grid_config.copy()
            # Resolution fix: multi-res only on spatial planes
            config["resolution"] = [
                r * res for r in config["resolution"][:3]
            ] + config["resolution"][3:]
                
            gp = self._init_grid_param(
                grid_nd=config["grid_dimensions"],
                in_dim=config["input_coordinate_dim"],
                out_dim=config["output_coordinate_dim"],
                reso=config["resolution"],
            )

            self.grids.append(gp)
        print(f"Initialized model grids: {self.grids}")
    
    def forward(self, x):
        return self._interpolate_ms_feature(
            x, ms_grids=self.grids,
            grid_dimensions=self.grid_config["grid_dimensions"],
            concat_features=self.concat_features, num_levels=None 
        )

    def _init_grid_param(self,
                        grid_nd: int,
                        in_dim: int,
                        out_dim: int,
                        reso,
                        a: float = 0.1,
                        b: float = 0.5,
                        n_components: int = 1):
            assert in_dim == len(reso), "Resolution must have same number of elements as input-dimension"
            has_time_planes = in_dim == 4
            assert grid_nd <= in_dim
            coo_combs = list(itertools.combinations(range(in_dim), grid_nd))
            grid_coefs = nn.ParameterList()
            for ci, coo_comb in enumerate(coo_combs):
                new_grid_coef = nn.Parameter(torch.empty(
                    [n_components, out_dim] + [reso[cc] for cc in coo_comb[::-1]]
                ))
                if has_time_planes and 3 in coo_comb:  # Initialize time planes to 1
                    nn.init.ones_(new_grid_coef)
                else:
                    nn.init.uniform_(new_grid_coef, a=a, b=b)
                grid_coefs.append(new_grid_coef)

            return grid_coefs

    def _interpolate_ms_feature(self, 
                                pts: Float[Tensor, "... 3"],
                                ms_grids: Collection[Iterable[nn.Module]],
                                grid_dimensions: int,
                                concat_features: bool,
                                num_levels: Optional[int],
                                ) -> torch.Tensor:
            coo_combs = list(itertools.combinations(
                range(pts.shape[-1]), grid_dimensions)
            )
            if num_levels is None:
                num_levels = len(ms_grids)
            multi_scale_interp = [] if concat_features else 0.
            grid: nn.ParameterList
            for scale_id, grid in enumerate(ms_grids[:num_levels]):
                interp_space = 1.
                for ci, coo_comb in enumerate(coo_combs):
                    # interpolate in plane
                    feature_dim = grid[ci].shape[1]  # shape of grid[ci]: 1, out_dim, *reso
                    interp_out_plane = (
                        grid_sample_wrapper(grid[ci], pts[..., coo_comb])
                        .view(-1, feature_dim)
                    )
                    # compute product over planes
                    interp_space = interp_space * interp_out_plane

                # combine over scales
                if concat_features:
                    multi_scale_interp.append(interp_space)
                else:
                    multi_scale_interp = multi_scale_interp + interp_space

            if concat_features:
                multi_scale_interp = torch.cat(multi_scale_interp, dim=-1)
            return multi_scale_interp
    
    def get_params(self):
        field_params = {k: v for k, v in self.grids.named_parameters(prefix="grids")}
        return {
            "field": list(field_params.values()),
        }

def get_kplane(n_input_dims: int, config) -> nn.Module: 
    encoding = KPlane(n_input_dims, config)
    encoding = CompositeEncoding(
        encoding,
        include_xyz=config.get("include_xyz", False),
        xyz_scale=1.0,
        xyz_offset=0.0
    ) # hard coded for triplane
    return encoding