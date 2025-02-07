import hashlib
import os
from shutil import copyfileobj

import numpy as np
import requests
import torch
import vigra
import zarr

from elf.io import open_file
from skimage.measure import regionprops

from segment_anything import sam_model_registry, SamPredictor

try:
    import imageio.v2 as imageio
except ImportError:
    import imageio

try:
    from napari.utils import progress as tqdm
except ImportError:
    from tqdm import tqdm

MODEL_URLS = {
    "vit_h": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth",
    "vit_l": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_l_0b3195.pth",
    "vit_b": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth"
}
CHECKPOINT_FOLDER = os.environ.get("SAM_MODELS", os.path.expanduser("~/.sam_models"))
CHECKSUMS = {
    "vit_h": "a7bf3b02f3ebf1267aba913ff637d9a2d5c33d3173bb679e46d9f338c26f262e",
    "vit_l": "3adcc4315b642a4d2101128f611684e8734c41232a17c648ed1693702a49a622",
    "vit_b": "ec2df62732614e57411cdcf32a23ffdf28910380d03139ee0f4fcbe91eb8c912"
}


def _download(url, path, model_type):
    with requests.get(url, stream=True, verify=True) as r:
        if r.status_code != 200:
            r.raise_for_status()
            raise RuntimeError(f"Request to {url} returned status code {r.status_code}")
        file_size = int(r.headers.get("Content-Length", 0))
        desc = f"Download {url} to {path}"
        if file_size == 0:
            desc += " (unknown file size)"
        with tqdm.wrapattr(r.raw, "read", total=file_size, desc=desc) as r_raw, open(path, "wb") as f:
            copyfileobj(r_raw, f)

    # validate the checksum
    expected_checksum = CHECKSUMS[model_type]
    if expected_checksum is None:
        return
    with open(path, "rb") as f:
        file_ = f.read()
        checksum = hashlib.sha256(file_).hexdigest()
    if checksum != expected_checksum:
        raise RuntimeError(
            "The checksum of the download does not match the expected checksum."
            f"Expected: {expected_checksum}, got: {checksum}"
        )
    print("Download successful and checksums agree.")


def _get_checkpoint(model_type, checkpoint_path=None):
    if checkpoint_path is None:
        checkpoint_url = MODEL_URLS[model_type]
        checkpoint_name = checkpoint_url.split("/")[-1]
        checkpoint_path = os.path.join(CHECKPOINT_FOLDER, checkpoint_name)

        # download the checkpoint if necessary
        if not os.path.exists(checkpoint_path):
            os.makedirs(CHECKPOINT_FOLDER, exist_ok=True)
            _download(checkpoint_url, checkpoint_path, model_type)
    elif not os.path.exists(checkpoint_path):
        raise ValueError(f"The checkpoint path {checkpoint_path} that was passed does not exist.")

    return checkpoint_path


def get_sam_model(device=None, model_type="vit_h", checkpoint_path=None, return_sam=False):
    """Get the SegmentAnything Predictor.

    This function will download the required model checkpoint or load it from file if it
    was already downloaded. By default the models are downloaded to ~/.sam_models.
    This location can be changed by setting the environment variable SAM_MODELS.

    Arguments:
        device [str, torch.device] - the device for the model. If none is given will use GPU if available.
            (default: None)
        model_type [str] - the SegmentAnything model to use. (default: vit_h)
        checkpoint_path [str] - the path to the corresponding checkpoint if it is already present
            and not in the default model folder. (default: None)
        return_sam [bool] - return the sam model object as well as the predictor (default: False)
    """
    checkpoint = _get_checkpoint(model_type, checkpoint_path)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    sam = sam_model_registry[model_type](checkpoint=checkpoint)
    sam.to(device=device)
    predictor = SamPredictor(sam)
    if return_sam:
        return predictor, sam
    return predictor


def _to_image(input_):
    # we require the input to be uint8
    if input_.dtype != np.dtype("uint8"):
        # first normalize the input to [0, 1]
        input_ = input_.astype("float32") - input_.min()
        input_ = input_ / input_.max()
        # then bring to [0, 255] and cast to uint8
        input_ = (input_ * 255).astype("uint8")
    if input_.ndim == 2:
        image = np.concatenate([input_[..., None]] * 3, axis=-1)
    elif input_.ndim == 3 and input_.shape[-1] == 3:
        image = input_
    else:
        raise ValueError(f"Invalid input image of shape {input_.shape}. Expect either 2D grayscale or 3D RGB image.")
    return image


def _compute_2d(input_, predictor):
    image = _to_image(input_)
    predictor.set_image(image)
    features = predictor.get_image_embedding()
    original_size = predictor.original_size
    input_size = predictor.input_size
    image_embeddings = {
        "features": features.cpu().numpy(), "input_size": input_size, "original_size": original_size,
    }
    return image_embeddings


def _precompute_2d(input_, predictor, save_path):
    f = zarr.open(save_path, "a")

    if "input_size" in f.attrs:
        features = f["features"][:]
        original_size, input_size = f.attrs["original_size"], f.attrs["input_size"]
    else:
        image = _to_image(input_)
        predictor.set_image(image)
        features = predictor.get_image_embedding()
        original_size, input_size = predictor.original_size, predictor.input_size
        f.create_dataset("features", data=features.cpu().numpy(), chunks=features.shape)
        f.attrs["input_size"] = input_size
        f.attrs["original_size"] = original_size

    image_embeddings = {
        "features": features, "input_size": input_size, "original_size": original_size,
    }
    return image_embeddings


def _compute_3d(input_, predictor):
    features = []
    original_size, input_size = None, None

    for z_slice in tqdm(input_, desc="Precompute Image Embeddings"):
        predictor.reset_image()

        # preprocess the image
        image = np.concatenate([z_slice[..., None]] * 3, axis=-1)

        predictor.set_image(image)
        embedding = predictor.get_image_embedding()
        features.append(embedding[None])

        if original_size is None:
            original_size = predictor.original_size
        if input_size is None:
            input_size = predictor.input_size

    # concatenate across the z axis
    features = torch.cat(features)

    image_embeddings = {
        "features": features.cpu().numpy(), "input_size": input_size, "original_size": original_size,
    }
    return image_embeddings


def _precompute_3d(input_, predictor, save_path, lazy_loading):
    f = zarr.open(save_path, "a")

    if "input_size" in f.attrs:
        features = f["features"]
        original_size, input_size = f.attrs["original_size"], f.attrs["input_size"]

    else:
        features = f["features"] if "features" in f else None
        original_size, input_size = None, None

        for z, z_slice in tqdm(enumerate(input_), total=input_.shape[0], desc="Precompute Image Embeddings"):
            if features is not None:
                emb = features[z]
                if np.count_nonzero(emb) != 0:
                    continue

            predictor.reset_image()
            image = np.concatenate([z_slice[..., None]] * 3, axis=-1)
            predictor.set_image(image)
            embedding = predictor.get_image_embedding()

            original_size, input_size = predictor.original_size, predictor.input_size
            if features is None:
                shape = (input_.shape[0],) + embedding.shape
                chunks = (1,) + embedding.shape
                features = f.create_dataset("features", shape=shape, chunks=chunks, dtype="float32")
            features[z] = embedding.cpu().numpy()

        f.attrs["input_size"] = input_size
        f.attrs["original_size"] = original_size

    if not lazy_loading:
        features = features[:]

    image_embeddings = {
        "features": features, "input_size": input_size, "original_size": original_size,
    }
    return image_embeddings


def precompute_image_embeddings(predictor, input_, save_path=None, lazy_loading=False, ndim=None):
    """Compute the image embeddings (output of the encoder) for the input.

    If save_path is given the embeddings will be loaded/saved in a zarr container.

    Arguments:
        predictor - the SegmentAnything predictor
        input_ [np.ndarray] - the input. Can be 2D or 3D.
        save_path [str] - path to save the embeddings in a zarr container (default: None)
        lazy_loading [bool] - whether to load all embeddings into memory or return an
            object to load them on demand when required. This only has an effect if 'save_path'
            is given and if the input is 3D. (default: False)
        ndim [int] - the dimensionality of the data. If not given will be deduced from the input data. (default: None)
    """

    ndim = input_.ndim if ndim is None else ndim
    if ndim == 2:
        image_embeddings = _compute_2d(input_, predictor) if save_path is None else\
            _precompute_2d(input_, predictor, save_path)

    elif ndim == 3:
        image_embeddings = _compute_3d(input_, predictor) if save_path is None else\
            _precompute_3d(input_, predictor, save_path, lazy_loading)

    else:
        raise ValueError(f"Invalid dimesionality {input_.ndim}, expect 2 or 3 dim data.")

    return image_embeddings


def set_precomputed(predictor, image_embeddings, i=None):
    """Set the precomputed image embeddings.

    Arguments:
        predictor - the SegmentAnything predictor
        image_embeddings [dict] - the precomputed image embeddings.
            This object is returned by 'precomputed_image_embeddings'.
        i [int] - the index for the image embeddings for 3D data.
            Only needs to be passed for 3d data. (default: None)
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"
    features = image_embeddings["features"]

    assert features.ndim in (4, 5)
    if features.ndim == 5 and i is None:
        raise ValueError("The data is 3D so an index i is needed.")
    elif features.ndim == 4 and i is not None:
        raise ValueError("The data is 2D so an index is not needed.")

    if i is None:
        predictor.features = features.to(device) if torch.is_tensor(features) else \
            torch.from_numpy(features).to(device)
    else:
        predictor.features = features[i].to(device) if torch.is_tensor(features) else \
            torch.from_numpy(features[i]).to(device)
    predictor.original_size = image_embeddings["original_size"]
    predictor.input_size = image_embeddings["input_size"]
    predictor.is_image_set = True

    return predictor


def compute_iou(mask1, mask2):
    """Compute the intersection over union of two masks.
    """
    overlap = np.logical_and(mask1 == 1, mask2 == 1).sum()
    union = np.logical_or(mask1 == 1, mask2 == 1).sum()
    eps = 1e-7
    iou = float(overlap) / (float(union) + eps)
    return iou


def get_cell_center_coordinates(gt, mode="v"):
    """
    Returns the center coordinates of the foreground instances in the ground-truth
    """
    assert mode in ["p", "v"], "Choose either 'p' for regionprops or 'v' for vigra"

    properties = regionprops(gt)

    if mode == "p":
        center_coordinates = {prop.label: prop.centroid for prop in properties}
    elif mode == "v":
        center_coordinates = vigra.filters.eccentricityCenters(gt.astype('float32'))
        center_coordinates = {i: coord for i, coord in enumerate(center_coordinates) if i > 0}

    bbox_coordinates = {prop.label: prop.bbox for prop in properties}

    assert len(bbox_coordinates) == len(center_coordinates)
    return center_coordinates, bbox_coordinates


def load_image_data(path, ndim, key=None, lazy_loading=False):
    if key is None:
        image_data = imageio.imread(path) if ndim == 2 else imageio.volread(path)
    else:
        with open_file(path, mode="r") as f:
            image_data = f[key]
            if not lazy_loading:
                image_data = image_data[:]
    return image_data


# TODO enable passing options for get_sam
def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input_path", required=True)
    parser.add_argument("-o", "--output_path", required=True)
    parser.add_argument("-k", "--key")
    args = parser.parse_args()

    predictor = get_sam_model()
    with open_file(args.input_path, mode="r") as f:
        data = f[args.key]
        precompute_image_embeddings(predictor, data, save_path=args.output_path)


if __name__ == "__main__":
    main()
