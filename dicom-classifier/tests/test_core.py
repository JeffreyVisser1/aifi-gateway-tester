"""Unit tests. Draai met:  python -m pytest -q tests/

Rekenkundige onderdelen worden vergeleken met scikit-learn of met een handberekening.
"""
import os
import sys
import tempfile

import numpy as np
import pytest
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autolabel_totalseg import label_volumes, series_to_nifti, volumes_to_regions  # noqa: E402
from data import encode_regions, percentile_norm  # noqa: E402
from dicom_io import _to_gray, load_series_images, scan_series  # noqa: E402
from metrics import confusion_matrix, modality_report, region_report  # noqa: E402
from taxonomy import (MODALITY_CLASSES, REGIONS, header_to_regions, map_modality,  # noqa: E402
                      regions_to_cid4031)
from train import class_weights, group_split, masked_bce  # noqa: E402


# ---------------------------------------------------------------- metrics vs scikit-learn
def test_modality_metrics_match_sklearn():
    from sklearn.metrics import confusion_matrix as sk_cm
    from sklearn.metrics import precision_recall_fscore_support
    rng = np.random.default_rng(1)
    n = len(MODALITY_CLASSES)
    y = rng.integers(0, n - 2, 500)          # twee klassen komen niet voor in de waarheid
    p = np.where(rng.random(500) < 0.7, y, rng.integers(0, n, 500))
    rep = modality_report(y, p, MODALITY_CLASSES)
    labels = list(range(n))
    np.testing.assert_array_equal(np.array(rep["confusion_matrix"]), sk_cm(y, p, labels=labels))
    P, R, F, S = precision_recall_fscore_support(y, p, labels=labels, zero_division=0)
    for i, c in enumerate(MODALITY_CLASSES):
        assert rep["per_class"][c]["precision"] == pytest.approx(P[i])
        assert rep["per_class"][c]["recall"] == pytest.approx(R[i])
        assert rep["per_class"][c]["f1"] == pytest.approx(F[i])
        assert rep["per_class"][c]["support"] == S[i]
    present = S > 0  # macro-F1 alleen over klassen die in de waarheid voorkomen
    assert rep["macro_f1"] == pytest.approx(F[present].mean())
    assert rep["accuracy"] == pytest.approx((y == p).mean())


def test_region_metrics_match_sklearn_and_mask_unknown():
    from sklearn.metrics import f1_score
    rng = np.random.default_rng(2)
    R = len(REGIONS)
    y = (rng.random((200, R)) < 0.3).astype(float)
    y[:40] = -1  # onbekend: mag niet meetellen
    probs = np.clip(y + rng.normal(0, 0.4, y.shape), 0, 1)
    rep = region_report(y, probs, REGIONS, 0.5)
    t, pr = y[40:].astype(int), (probs[40:] >= 0.5).astype(int)
    f1 = f1_score(t, pr, average=None, zero_division=0)
    assert rep["n_labeled"] == 160
    for i, r in enumerate(REGIONS):
        assert rep["per_region"][r]["f1"] == pytest.approx(f1[i])
    assert rep["exact_match"] == pytest.approx((t == pr).all(axis=1).mean())


def test_confusion_matrix_small_by_hand():
    cm = confusion_matrix([0, 0, 1, 2, 2, 2], [0, 1, 1, 2, 0, 2], 3)
    assert cm.tolist() == [[1, 1, 0], [0, 1, 0], [1, 0, 2]]


# ---------------------------------------------------------------- loss en gewichten
def test_masked_bce_equals_manual():
    logits = torch.tensor([[0.5, -1.0, 2.0], [1.0, 1.0, 1.0]])
    target = torch.tensor([[1.0, 0.0, 1.0], [-1.0, -1.0, -1.0]])  # tweede rij onbekend
    sig = torch.sigmoid(logits[0])
    manual = -(torch.log(sig[0]) + torch.log(1 - sig[1]) + torch.log(sig[2])) / 3
    assert masked_bce(logits, target).item() == pytest.approx(manual.item(), rel=1e-6)
    assert masked_bce(logits[1:], target[1:]).item() == 0.0


def test_class_weights_by_hand():
    recs = [{"modality": "CT"}] * 16 + [{"modality": "MR"}] * 4
    w = class_weights(recs).numpy()
    # 1/sqrt(16)=0.25, 1/sqrt(4)=0.5, gemiddelde 0.375 -> 0.6667 en 1.3333; afwezige klassen 0
    assert w[MODALITY_CLASSES.index("CT")] == pytest.approx(0.25 / 0.375)
    assert w[MODALITY_CLASSES.index("MR")] == pytest.approx(0.5 / 0.375)
    assert w[MODALITY_CLASSES.index("US")] == 0.0


def test_group_split_keeps_sites_disjoint():
    recs = [{"series_uid": str(i), "site": f"S{i % 4}", "patient_hash": f"p{i}"} for i in range(40)]
    tr, va = group_split(recs, "site", 0.25, 0)
    assert {r["site"] for r in tr}.isdisjoint({r["site"] for r in va})
    assert len(tr) + len(va) == 40 and len(va) >= 10 and len(tr) > 0


# ---------------------------------------------------------------- preprocessing
def test_percentile_norm_invariant_to_rescale_slope_intercept():
    rng = np.random.default_rng(3)
    raw = rng.normal(1000, 300, (64, 64))
    for slope, intercept in [(1, -1024), (2.5, 7), (0.001, -3)]:
        np.testing.assert_allclose(percentile_norm(raw), percentile_norm(slope * raw + intercept), atol=1e-5)
    out = percentile_norm(raw)
    assert out.min() == 0.0 and out.max() == pytest.approx(1.0)


def test_percentile_norm_constant_image():
    assert not percentile_norm(np.full((8, 8), 5.0)).any()


def test_monochrome1_is_inverted_and_rgb_to_gray():
    class D:  # minimale stand-in voor een pydicom Dataset
        def __init__(self, **kw): self.__dict__.update(kw)
    arr = np.array([[0, 100], [200, 300]], dtype=np.uint16)
    out = _to_gray(arr, D(PhotometricInterpretation="MONOCHROME1", SamplesPerPixel=1))
    assert out.tolist() == [[300, 200], [100, 0]]
    rgb = np.stack([arr, arr * 0, arr * 0], axis=-1)
    out = _to_gray(rgb, D(PhotometricInterpretation="RGB", SamplesPerPixel=3))
    np.testing.assert_allclose(out, arr / 3.0)


def test_encode_regions():
    assert encode_regions([]).tolist() == [-1.0] * len(REGIONS)
    v = encode_regions(["chest", "abdomen"])
    assert v.sum().item() == 2 and v[REGIONS.index("chest")] == 1


# ---------------------------------------------------------------- taxonomie
@pytest.mark.parametrize("raw,expected", [
    ("CT", "CT"), ("CR", "DX"), ("DX", "DX"), ("RF", "XA"), ("SC", "OT"), ("SR", None), ("KO", None), ("", "OT"),
])
def test_map_modality(raw, expected):
    assert map_modality(raw) == expected


@pytest.mark.parametrize("text,expected", [
    ("CHEST", {"chest"}), ("THORAX", {"chest"}), ("ABDOMENPELVIS", {"abdomen", "pelvis"}),
    ("CHEST_TO_PELVIS", {"chest", "abdomen", "pelvis"}),
    ("CHESTABDPELVIS", {"chest", "abdomen", "pelvis"}), ("HOOFD", {"head"}), ("Knie links", {"lower_limb"}),
    ("LSPINE", {"spine"}), ("CSPINE", {"spine", "neck"}), ("MAMMA", {"breast"}), ("", set()),
    ("OBLONG", set()), ("WHOLEBODY", {"head", "neck", "chest", "abdomen", "pelvis"}),
])
def test_header_to_regions(text, expected):
    assert header_to_regions(text) == expected


def test_mg_defaults_to_breast():
    assert header_to_regions("", "MG") == {"breast"}


@pytest.mark.parametrize("regions,code", [
    ({"chest"}, "816094009"), ({"chest", "abdomen", "pelvis"}, "416775004"), ({"abdomen", "pelvis"}, "818982008"),
    ({"head", "neck", "chest", "abdomen", "pelvis"}, "38266002"), ({"chest", "upper_limb"}, "816094009"),
    (set(), None),
])
def test_regions_to_cid4031(regions, code):
    res = regions_to_cid4031(regions)
    assert (res[0] if res else None) == code


# ---------------------------------------------------------------- TotalSegmentator-koppeling (zonder het model)
def test_label_volumes_and_regions():
    seg = np.zeros((10, 10, 10), dtype=np.uint8)
    seg[:5] = 1   # 500 voxels 'liver'
    seg[5:7] = 2  # 200 voxels 'lung_upper_lobe_left'
    cmap = {1: "liver", 2: "lung_upper_lobe_left", 3: "brain"}
    vols = label_volumes(seg, voxel_mm3=8.0, class_map=cmap)  # 2x2x2 mm voxels
    assert vols == {"liver": pytest.approx(4.0), "lung_upper_lobe_left": pytest.approx(1.6)}
    assert volumes_to_regions({"liver": 1500, "lung_upper_lobe_left": 300}) == {"abdomen"}  # longbasis telt niet
    assert volumes_to_regions({"liver": 1500, "lung_lower_lobe_left": 900, "lung_lower_lobe_right": 900}) == \
        {"abdomen", "chest"}


# ---------------------------------------------------------------- externe gewichten (RadImageNet-formaat)
@pytest.mark.parametrize("fmt", ["state_dict_prefixed", "full_module"])
def test_load_external_weights(fmt):
    import torchvision
    from model import load_external_weights
    src = torchvision.models.resnet18(weights=None)
    path = os.path.join(tempfile.mkdtemp(), "w.pt")
    if fmt == "full_module":
        torch.save(src, path)
    else:
        torch.save({"state_dict": {f"module.{k}": v for k, v in src.state_dict().items()}}, path)
    dst = torchvision.models.resnet18(weights=None)
    assert load_external_weights(dst, path) == pytest.approx(1.0)
    assert torch.equal(dst.conv1.weight, src.conv1.weight)


# ---------------------------------------------------------------- DICOM I/O op synthetische data
@pytest.fixture(scope="module")
def synth_dir():
    from tests.make_synthetic import main as make
    d = tempfile.mkdtemp()
    make(["--out", d, "--per-class", "1"])
    return d


def test_scan_series_skips_non_images(synth_dir):
    series = scan_series(synth_dir)
    mods = sorted(s.modality_raw for s in series)
    assert "SR" not in mods and len(series) == 12


def test_load_series_images_always_k(synth_dir):
    for s in scan_series(synth_dir):
        for k in (1, 4, 9):
            imgs = load_series_images(s.files, k)
            assert len(imgs) == k and all(im.ndim == 2 for im in imgs)
        imgs = load_series_images(s.files, 5, np.random.default_rng(0))
        assert len(imgs) == 5


def test_series_to_nifti_geometry(synth_dir):
    import nibabel as nib
    ct = next(s for s in scan_series(synth_dir) if s.modality_raw == "CT")
    out = os.path.join(tempfile.mkdtemp(), "ct.nii.gz")
    series_to_nifti(ct.files, out)
    img = nib.load(out)
    assert img.shape == (256, 256, 12)
    np.testing.assert_allclose(img.header.get_zooms()[:3], (1.0, 1.0, 3.0))
