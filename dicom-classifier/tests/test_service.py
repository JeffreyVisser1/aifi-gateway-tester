"""Tests voor Windows-paden, het portable manifest, de engine (JSON-oordeel), het SR met het oordeel
en de DICOM-dienst end-to-end (C-STORE in, SR terug, doorsturen, HTTP-API).

Het model in deze tests is ongetraind (willekeurige gewichten): getest wordt de keten, niet de
kwaliteit van de voorspelling.
"""
import json
import ntpath
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pydicom
import pytest
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dicom_io  # noqa: E402
import dicom_service  # noqa: E402
from engine import Classifier, verdict  # noqa: E402
from model import SeriesClassifier  # noqa: E402
from taxonomy import MODALITY_CLASSES, REGIONS  # noqa: E402
from train import load_manifest, read_csv_rows  # noqa: E402


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def synth_dir():
    from tests.make_synthetic import main as make
    d = tempfile.mkdtemp()
    make(["--out", d, "--per-class", "1"])
    return d


@pytest.fixture(scope="module")
def ckpt():
    torch.manual_seed(0)
    model = SeriesClassifier(len(MODALITY_CLASSES), len(REGIONS), "resnet18", init="none")
    path = os.path.join(tempfile.mkdtemp(), "best.pt")
    torch.save({"state_dict": model.state_dict(), "epoch": 0,
                "config": {"encoder": "resnet18", "k": 2, "img_size": 64,
                           "modality_classes": MODALITY_CLASSES, "regions": REGIONS}}, path)
    return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- paden
def test_fs_path_is_noop_off_windows():
    long = "/data/" + "a" * 300 + "/x.dcm"
    assert dicom_io.fs_path(long) == long or os.name == "nt"


def test_fs_path_windows_long_and_unc(monkeypatch):
    monkeypatch.setattr(dicom_io, "_WIN", True)
    monkeypatch.setattr(dicom_io.os.path, "abspath", ntpath.abspath)
    short = "C:\\data\\x.dcm"
    assert dicom_io.fs_path(short) == short
    long = "C:\\data\\" + "\\".join(["map" + str(i) * 20 for i in range(15)]) + "\\x.dcm"
    assert len(long) > 260
    assert dicom_io.fs_path(long) == "\\\\?\\" + long
    unc = "\\\\server\\share\\" + "b" * 300
    assert dicom_io.fs_path(unc) == "\\\\?\\UNC\\server\\share\\" + "b" * 300
    for p in (long, unc):
        assert dicom_io.display_path(dicom_io.fs_path(p)) == p
    assert dicom_io.fs_path("\\\\?\\" + long) == "\\\\?\\" + long  # niet dubbel


def test_portable_paths_roundtrip(tmp_path):
    f = tmp_path / "studie" / "serie" / "001.dcm"
    rel = dicom_io.to_portable(str(f), str(tmp_path))
    assert rel == "studie/serie/001.dcm"
    assert dicom_io.from_portable(rel, str(tmp_path)) == str(f)
    assert dicom_io.from_portable("C:\\oud\\x.dcm", "/elders") == "C:\\oud\\x.dcm"  # oud manifest
    assert dicom_io.from_portable("/oud/x.dcm", "C:\\elders") == "/oud/x.dcm"


def test_manifest_from_other_machine_with_data_root(tmp_path):
    rec = {"series_uid": "1", "root": "C:\\export\\dicom", "files": ["a/1.dcm", "a/2.dcm"]}
    m = tmp_path / "manifest.jsonl"
    m.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    got = load_manifest(str(m), data_root=str(tmp_path / "dicom"))
    assert got[0]["files"] == [str(tmp_path / "dicom" / "a" / "1.dcm"), str(tmp_path / "dicom" / "a" / "2.dcm")]


def test_build_dataset_writes_portable_manifest(synth_dir, tmp_path):
    from build_dataset import main as build
    build(["--input", synth_dir, "--out", str(tmp_path)])
    recs = load_manifest(str(tmp_path / "manifest.jsonl"))
    raw = [json.loads(line) for line in open(tmp_path / "manifest.jsonl", encoding="utf-8")]
    assert all("\\" not in f and not os.path.isabs(f) for r in raw for f in r["files"])
    assert all(os.path.isfile(f) for r in recs for f in r["files"])
    # review.csv met BOM, zodat Excel op Windows de codering herkent
    assert (tmp_path / "review.csv").read_bytes()[:3] == b"\xef\xbb\xbf"


@pytest.mark.parametrize("enc,delim", [("utf-8-sig", ","), ("utf-8", ";"), ("cp1252", ";")])
def test_read_csv_rows_like_excel_saves_it(tmp_path, enc, delim):
    p = tmp_path / "review.csv"
    text = delim.join(["series_uid", "series_description", "corrected_regions"]) + "\r\n" + \
        delim.join(["1.2.3", "Thorax café", '"chest;abdomen"']) + "\r\n"
    p.write_bytes(text.encode(enc))
    rows = read_csv_rows(str(p))
    assert rows == [{"series_uid": "1.2.3", "series_description": "Thorax café",
                     "corrected_regions": "chest;abdomen"}]


# ---------------------------------------------------------------- engine en oordeel
def _r(status):
    return {"status": status}


@pytest.mark.parametrize("statuses,expected", [
    ([], "geen_beeld"),
    (["akkoord", "akkoord"], "akkoord"),
    (["akkoord", "lage_zekerheid"], "onzeker"),
    (["geen_header_bodypart", "akkoord"], "onzeker"),
    (["akkoord", "lage_zekerheid;afwijking_bodypart"], "afwijking"),
    (["afwijking_modaliteit"], "afwijking"),
])
def test_verdict(statuses, expected):
    assert verdict([_r(s) for s in statuses]) == expected


def test_infer_json_output(synth_dir, ckpt, tmp_path):
    from infer import main as infer
    out = tmp_path / "uitkomst.json"
    infer(["--ckpt", ckpt, "--input", synth_dir, "--out", str(out), "--format", "json", "--device", "cpu"])
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["verdict"] in ("akkoord", "onzeker", "afwijking")
    assert len(doc["series"]) == 12  # SR en README worden overgeslagen
    s = doc["series"][0]
    assert set(s["p_regions"]) == set(REGIONS) and 0 <= s["p_modality"] <= 1
    assert doc["classifier"]["encoder"] == "resnet18"


def test_infer_csv_unchanged_columns(synth_dir, ckpt, tmp_path):
    from engine import CSV_FIELDS
    from infer import main as infer
    out = tmp_path / "p.csv"
    infer(["--ckpt", ckpt, "--input", synth_dir, "--out", str(out), "--device", "cpu"])
    rows = read_csv_rows(str(out))
    assert len(rows) == 12 and list(rows[0]) == CSV_FIELDS


# ---------------------------------------------------------------- SR
def test_build_sr_is_valid_and_in_same_study(synth_dir, ckpt):
    clf = Classifier(ckpt, "cpu")
    study_dir = os.path.join(synth_dir, "CT_CHEST_0")
    doc = clf.classify_study(study_dir)
    files = sorted(os.path.join(study_dir, f) for f in os.listdir(study_dir))
    header = pydicom.dcmread(files[0], stop_before_pixels=True)
    ev = [(str(h.SeriesInstanceUID), str(h.SOPClassUID), str(h.SOPInstanceUID))
          for h in (pydicom.dcmread(f, stop_before_pixels=True) for f in files)]
    sr = dicom_service.build_sr(doc, header, ev, "AIFICLS")
    path = os.path.join(tempfile.mkdtemp(), "sr.dcm")
    sr.save_as(path, enforce_file_format=True)
    back = pydicom.dcmread(path)
    assert back.Modality == "SR" and back.SOPClassUID == dicom_service.BASIC_TEXT_SR
    assert back.StudyInstanceUID == header.StudyInstanceUID
    assert back.SeriesInstanceUID != header.SeriesInstanceUID
    assert back.PatientID == header.PatientID
    assert back.ContentSequence[0].TextValue == doc["verdict"]
    refs = back.CurrentRequestedProcedureEvidenceSequence[0].ReferencedSeriesSequence[0].ReferencedSOPSequence
    assert len(refs) == len(files)
    # zelfde studie -> zelfde SR-serie (tweede run komt niet in een nieuwe serie)
    assert dicom_service.build_sr(doc, header, ev, "X").SeriesInstanceUID == back.SeriesInstanceUID


# ---------------------------------------------------------------- dienst end-to-end
class Collector:
    """Minimale C-STORE SCP die alles bewaart wat binnenkomt."""

    def __init__(self, port, ae_title="COLLECT"):
        from pynetdicom import AE, ALL_TRANSFER_SYNTAXES, AllStoragePresentationContexts, evt
        self.received = []
        ae = AE(ae_title=ae_title)
        for cx in AllStoragePresentationContexts:
            ae.add_supported_context(cx.abstract_syntax, ALL_TRANSFER_SYNTAXES)

        def on_store(event):
            self.received.append(event.dataset)
            return 0x0000

        self.server = ae.start_server(("127.0.0.1", port), block=False,
                                      evt_handlers=[(evt.EVT_C_STORE, on_store)])

    def stop(self):
        self.server.shutdown()


def wait_until(cond, timeout=90.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


@pytest.fixture()
def service_env(synth_dir, ckpt, tmp_path):
    scp_port, sink_port, api_port = free_port(), free_port(), free_port()
    cfg_path = tmp_path / "classifier.yaml"
    cfg_path.write_text(f"""
model: {{checkpoint: '{ckpt}', device: cpu}}
scp: {{port: {scp_port}, aeTitle: AIFICLS, allowedCallingAeTitles: [TESTSCU]}}
study: {{completeAfterSeconds: 1}}
spool: {{minFreeMb: 1}}
resultTarget: {{enabled: true, port: {sink_port}, aeTitle: COLLECT, callingAeTitle: AIFICLS}}
forward: {{enabled: true, port: {sink_port}, aeTitle: COLLECT, callingAeTitle: AIFICLS,
          verdicts: [akkoord, onzeker, afwijking, geen_beeld]}}
api: {{port: {api_port}, token: geheim, allowedRoots: ['{synth_dir}']}}
retry: {{maxAttempts: 2, initialDelaySeconds: 1}}
""", encoding="utf-8")
    cfg = dicom_service.load_config(str(cfg_path))
    sink = Collector(sink_port)
    svc = dicom_service.Service(cfg, Classifier(ckpt, "cpu"))
    svc.start()
    yield cfg, svc, sink
    svc.stop()
    sink.stop()


def test_service_receives_classifies_returns_sr_and_forwards(service_env, synth_dir):
    cfg, svc, sink = service_env
    study = os.path.join(synth_dir, "CT_CHEST_0")
    files = sorted(os.path.join(study, f) for f in os.listdir(study))
    target = {"host": "127.0.0.1", "port": cfg["scp"]["port"], "aeTitle": "AIFICLS", "callingAeTitle": "TESTSCU"}
    assert dicom_service.send_datasets(cfg, target, files) == len(files)

    assert wait_until(lambda: len(sink.received) >= len(files) + 1), f"ontvangen: {len(sink.received)}"
    assert wait_until(lambda: not os.listdir(cfg["spool"]["dir"]))  # spool leeg na verwerking
    srs = [d for d in sink.received if d.Modality == "SR"]
    images = [d for d in sink.received if d.Modality != "SR"]
    study_uid = pydicom.dcmread(files[0], stop_before_pixels=True).StudyInstanceUID
    assert len(srs) == 1 and srs[0].StudyInstanceUID == study_uid
    assert len(images) == len(files)  # originelen doorgestuurd
    with open(os.path.join(cfg["results"]["dir"], "results.jsonl"), encoding="utf-8") as fh:
        docs = [json.loads(line) for line in fh]
    assert len(docs) == 1 and docs[0]["study_uid"] == study_uid and docs[0]["received_from"] == "TESTSCU"
    assert os.path.isfile(os.path.join(cfg["results"]["dir"], "results.csv"))


def test_service_rejects_unknown_calling_ae(service_env, synth_dir):
    cfg, svc, sink = service_env
    f = os.path.join(synth_dir, "DX_CHEST_0", "000.dcm")
    target = {"host": "127.0.0.1", "port": cfg["scp"]["port"], "aeTitle": "AIFICLS", "callingAeTitle": "ONBEKEND"}
    with pytest.raises(dicom_service.SendError):
        dicom_service.send_datasets(cfg, target, [f])


def test_service_holds_study_when_verdict_not_forwarded(service_env, synth_dir):
    cfg, svc, sink = service_env
    cfg["forward"]["verdicts"] = []  # niets doorsturen: alles naar held\
    cfg["resultTarget"]["enabled"] = False
    study = os.path.join(synth_dir, "MR_HEAD_0")
    files = sorted(os.path.join(study, f) for f in os.listdir(study))
    target = {"host": "127.0.0.1", "port": cfg["scp"]["port"], "aeTitle": "AIFICLS", "callingAeTitle": "TESTSCU"}
    dicom_service.send_datasets(cfg, target, files)
    study_uid = pydicom.dcmread(files[0], stop_before_pixels=True).StudyInstanceUID
    held = os.path.join(cfg["held"]["dir"], study_uid)
    assert wait_until(lambda: os.path.isdir(held) and not os.listdir(cfg["spool"]["dir"]))
    assert len(dicom_service.study_files(held)) == len(files)
    assert sink.received == []


def test_service_retries_then_moves_to_failed(service_env, synth_dir):
    cfg, svc, sink = service_env
    cfg["resultTarget"]["port"] = free_port()  # niemand luistert: SR versturen mislukt
    cfg["network"]["connectTimeoutSeconds"] = 1
    f = os.path.join(synth_dir, "DX_CHEST_0", "000.dcm")
    target = {"host": "127.0.0.1", "port": cfg["scp"]["port"], "aeTitle": "AIFICLS", "callingAeTitle": "TESTSCU"}
    dicom_service.send_datasets(cfg, target, [f])
    study_uid = pydicom.dcmread(f, stop_before_pixels=True).StudyInstanceUID
    failed = os.path.join(cfg["spool"]["failedDir"], study_uid)
    assert wait_until(lambda: os.path.isdir(failed), timeout=60)
    with open(os.path.join(failed, "state.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    assert state["attempts"] == 2 and "associatie" in state["last_error"]


def _post(port, body, token="geheim"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/classify", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Api-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_classify_token_and_allowed_roots(service_env, synth_dir, tmp_path):
    cfg, svc, sink = service_env
    port = cfg["api"]["port"]
    code, doc = _post(port, {"path": os.path.join(synth_dir, "US_ABDOMEN_0"), "route": "main"})
    assert code == 200 and doc["verdict"] in ("akkoord", "onzeker", "afwijking") and len(doc["series"]) == 1
    assert _post(port, {"path": synth_dir}, token="fout")[0] == 401
    outside = tmp_path / "elders"
    shutil.copytree(os.path.join(synth_dir, "US_ABDOMEN_0"), outside)
    assert _post(port, {"path": str(outside)})[0] == 403
    assert _post(port, {"path": str(tmp_path / "bestaat-niet")})[0] == 404
    assert _post(port, {"geen": "pad"})[0] == 400
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers={"X-Api-Token": "geheim"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert json.loads(r.read())["status"] == "ok"


def test_config_rejects_open_api_without_token(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("api: {bindAddress: 0.0.0.0}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="api.token"):
        dicom_service.load_config(str(p))


def test_init_config_is_loadable(tmp_path):
    p = tmp_path / "classifier.yaml"
    dicom_service.main(["--init-config", str(p)])
    cfg = dicom_service.load_config(str(p))
    assert cfg["scp"]["port"] == 11300 and os.path.isabs(cfg["spool"]["dir"])

