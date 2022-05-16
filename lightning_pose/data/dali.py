"""Data pipelines based on efficient video reading by nvidia dali package."""

import cv2
from nvidia.dali import pipeline_def
import nvidia.dali.fn as fn
from nvidia.dali.pipeline import Pipeline
from nvidia.dali.plugin.pytorch import DALIGenericIterator
import nvidia.dali.types as types
import torch
from typeguard import typechecked
from typing import List, Optional, Union
from torchtyping import TensorType

from lightning_pose.data import _IMAGENET_MEAN, _IMAGENET_STD

_DALI_DEVICE = "gpu" if torch.cuda.is_available() else "cpu"


# cannot typecheck due to way pipeline_def decorator consumes additional args
@pipeline_def
def video_pipe(
    filenames: Union[List[str], str],
    resize_dims: Optional[List[int]] = None,
    random_shuffle: bool = False,
    seed: int = 123456,
    sequence_length: int = 16,
    pad_sequences: bool = True,
    initial_fill: int = 16,
    normalization_mean: List[float] = _IMAGENET_MEAN,
    normalization_std: List[float] = _IMAGENET_STD,
    device: str = _DALI_DEVICE,
    name: str = "reader",
    # arguments consumed by decorator:
    # batch_size,
    # num_threads,
    # device_id
) -> Pipeline:
    """Generic video reader pipeline that loads videos, resizes, and normalizes.

    Args:
        filenames: list of absolute paths of video files to feed through
            pipeline
        resize_dims: [height, width] to resize raw frames
        random_shuffle: True to grab random batches of frames from videos;
            False to sequential read
        seed: random seed when `random_shuffle` is True
        sequence_length: number of frames to load per sequence
        pad_sequences: allows creation of incomplete sequences if there is an
            insufficient number of frames at the very end of the video
        initial_fill: size of the buffer that is used for random shuffling
        normalization_mean: mean values in (0, 1) to subtract from each channel
        normalization_std: standard deviation values to subtract from each
            channel
        device: "cpu" | "gpu"
        name: pipeline name, used to string together DataNode elements

    Returns:
        pipeline object to be fed to DALIGenericIterator

    """
    video = fn.readers.video(
        device=device,
        filenames=filenames,
        random_shuffle=random_shuffle,
        seed=seed,
        sequence_length=sequence_length,
        pad_sequences=pad_sequences,
        initial_fill=initial_fill,
        normalized=False,
        name=name,
        dtype=types.DALIDataType.FLOAT,
    )
    if resize_dims:
        video = fn.resize(video, size=resize_dims)
    # video pixel range is [0, 255]; transform it to [0, 1].
    # happens naturally in the torchvision transform to tensor.
    video = video / 255.0
    # permute dimensions and normalize to imagenet statistics
    transform = fn.crop_mirror_normalize(
        video, output_layout="FCHW", mean=normalization_mean, std=normalization_std,
    )
    return transform


class LightningWrapper(DALIGenericIterator):
    """wrapper around a DALI pipeline to get batches for ptl."""

    def __init__(self, *kargs, **kwargs):

        # collect number of batches computed outside of class
        self.num_batches = kwargs.pop("num_batches", 1)

        # call parent
        super().__init__(*kargs, **kwargs)

    def __len__(self):
        return self.num_batches

    def __next__(self):
        out = super().__next__()
        return torch.tensor(
            out[0]["x"][0, :, :, :, :],  # should be (sequence_length, 3, H, W)
            dtype=torch.float,
        )  # careful: only valid for one sequence, i.e., batch size of 1.


# TODO: first and last inds here will be less reliable due to the repetitions
# either fix post-hoc, or do something else
def get_context_from_seq(
    img_seq: TensorType["sequence_length", 3, "image_height", "image_width"],
    context_length: int,
) -> TensorType["sequence_length", "context_length", 3, "image_height", "image_width"]:
    pass
    # our goal is to extract 5-frame sequences from this sequence
    img_shape = img_seq.shape[1:]
    seq_len = img_seq.shape[0]
    train_seq = torch.zeros(
        (seq_len, context_length, *img_shape), device=img_seq.device
    )
    # define pads: start pad repeats the zeroth image twice. end pad repeats the last image twice.
    # this is to give padding for the first and last frames of the sequence
    pad_start = torch.tile(img_seq[0].unsqueeze(0), (2, 1, 1, 1))
    pad_end = torch.tile(img_seq[-1].unsqueeze(0), (2, 1, 1, 1))
    # pad the sequence
    padded_seq = torch.cat((pad_start, img_seq, pad_end), dim=0)
    # padded_seq = torch.cat((two_pad, img_seq, two_pad), dim=0)
    for i in range(seq_len):
        # extract 5-frame sequences from the padded sequence
        train_seq[i] = padded_seq[i : i + context_length]
    return train_seq

