# Copyright 2022 The Plenoptix Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Data parser for friends dataset"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from rich.console import Console

import nerfactory.configs.base as cfg
from nerfactory.cameras.cameras import Cameras, CameraType
from nerfactory.datamanagers.dataparsers.base import DataParser
from nerfactory.datamanagers.structs import DatasetInputs, SceneBounds, Semantics
from nerfactory.utils.io import get_absolute_path, load_from_json

CONSOLE = Console()


def get_semantics_and_masks(image_idx: int, semantics: Semantics):
    """function to process additional semantics and mask information

    Args:
        image_idx: specific image index to work with
        semantics: semantics data
    """
    # handle mask
    person_index = semantics.thing_classes.index("person")
    thing_image_filename = semantics.thing_filenames[image_idx]
    pil_image = Image.open(thing_image_filename)
    thing_semantics = torch.from_numpy(np.array(pil_image, dtype="int32"))[..., None]
    mask = (thing_semantics != person_index).to(torch.float32)  # 1 where valid
    # handle semantics
    stuff_image_filename = semantics.stuff_filenames[image_idx]
    pil_image = Image.open(stuff_image_filename)
    stuff_semantics = torch.from_numpy(np.array(pil_image, dtype="int32"))[..., None]
    return {"mask": mask, "semantics": stuff_semantics}


@dataclass
class Friends(DataParser):
    """Friends Dataset"""

    config: cfg.FriendsDataParserConfig

    @classmethod
    def _get_aabb_and_transform(cls, basedir):
        """Returns the aabb and pointcloud transform from the threejs.json file.

        Args:
            basedir: base directory to load from
        """
        filename = basedir / "threejs.json"
        assert filename.exists()
        data = load_from_json(filename)

        # point cloud transformation
        transposed_point_cloud_transform = np.array(data["object"]["children"][0]["matrix"]).reshape(4, 4).T
        assert transposed_point_cloud_transform[3, 3] == 1.0

        # bbox transformation
        bbox_transform = np.array(data["object"]["children"][1]["matrix"]).reshape(4, 4).T
        w, h, d = data["geometries"][1]["width"], data["geometries"][1]["height"], data["geometries"][1]["depth"]
        temp = np.array([w, h, d]) / 2.0
        bbox = np.array([-temp, temp])
        bbox = np.concatenate([bbox, np.ones_like(bbox[:, 0:1])], axis=1)
        bbox = (bbox_transform @ bbox.T).T[:, 0:3]

        aabb = bbox  # rename to aabb because it's an axis-aligned bounding box
        return torch.from_numpy(aabb).float(), torch.from_numpy(transposed_point_cloud_transform).float()

    def _generate_dataset_inputs(self, split="train"):  # pylint: disable=unused-argument

        abs_dir = get_absolute_path(self.config.data_directory)

        cameras_json = load_from_json(abs_dir / "cameras.json")
        frames = cameras_json["frames"]
        bbox = torch.tensor(cameras_json["bbox"])

        image_filenames = []
        fx = []
        fy = []
        cx = []
        cy = []
        camera_to_worlds = []
        for frame in frames:
            # unpack data
            image_filename = abs_dir / "images" / frame["image_name"]
            intrinsics = torch.tensor(frame["intrinsics"])
            camtoworld = torch.tensor(frame["camtoworld"])[:3]
            # append data
            image_filenames.append(image_filename)
            fx.append(intrinsics[0, 0])
            fy.append(intrinsics[1, 1])
            cx.append(intrinsics[0, 2])
            cy.append(intrinsics[1, 2])
            camera_to_worlds.append(camtoworld)
        fx = torch.stack(fx)
        fy = torch.stack(fy)
        cx = torch.stack(cx)
        cy = torch.stack(cy)
        camera_to_worlds = torch.stack(camera_to_worlds)

        # rotate the cameras and box 90 degrees about the x axis to put the z axis up
        rotation = torch.tensor([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=torch.float32)
        camera_to_worlds[:, :3] = rotation @ camera_to_worlds[:, :3]
        bbox = (rotation @ bbox.T).T

        # -- set the bounding box ---
        scene_bounds = SceneBounds(aabb=bbox)
        # # for shifting and rescale accoding to scene bounds
        # box_center = scene_bounds_original.get_center()
        # box_scale_factor = 5.0 / scene_bounds_original.get_diagonal_length()  # the target diagonal length
        # scene_bounds = scene_bounds_original.get_centered_and_scaled_scene_bounds(box_scale_factor)

        # --- semantics ---
        semantics = None
        if self.config.include_semantics:
            thing_filenames = [
                Path(str(image_filename).replace("/images/", "/segmentations/thing/").replace(".jpg", ".png"))
                for image_filename in image_filenames
            ]
            stuff_filenames = [
                Path(str(image_filename).replace("/images/", "/segmentations/stuff/").replace(".jpg", ".png"))
                for image_filename in image_filenames
            ]
            panoptic_classes = load_from_json(abs_dir / "panoptic_classes.json")
            stuff_classes = panoptic_classes["stuff"]
            stuff_colors = torch.tensor(panoptic_classes["stuff_colors"], dtype=torch.float32) / 255.0
            thing_classes = panoptic_classes["thing"]
            thing_colors = torch.tensor(panoptic_classes["thing_colors"], dtype=torch.float32) / 255.0
            semantics = Semantics(
                stuff_classes=stuff_classes,
                stuff_colors=stuff_colors,
                stuff_filenames=stuff_filenames,
                thing_classes=thing_classes,
                thing_colors=thing_colors,
                thing_filenames=thing_filenames,
            )

        assert torch.all(cx[0] == cx), "Not all cameras have the same cx. Our Cameras class does not support this."
        assert torch.all(cy[0] == cy), "Not all cameras have the same cy. Our Cameras class does not support this."

        cameras = Cameras(
            fx=fx,
            fy=fy,
            cx=float(cx[0]),
            cy=float(cy[0]),
            camera_to_worlds=camera_to_worlds,
            camera_type=CameraType.PERSPECTIVE,
        )

        dataset_inputs = DatasetInputs(
            image_filenames=image_filenames,
            cameras=cameras,
            scene_bounds=scene_bounds,
            additional_inputs={"semantics": {"func": get_semantics_and_masks, "kwargs": {"semantics": semantics}}},
        )
        return dataset_inputs
