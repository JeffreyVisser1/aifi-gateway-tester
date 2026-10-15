r"""DICOM-dienst: de classifier als DICOM-knooppunt in de beeldstroom, en als add-on-API.

  PACS / modaliteit / gateway --C-STORE--> dicom_service (AE AIFICLS:11300)
      1. ontvangen in spool\<StudyUID>\<SeriesUID>\<SOPUID>.dcm (eerst tijdelijke naam, dan hernoemen)
      2. na study.completeAfterSeconds zonder nieuwe beelden: classificeren (alleen pixels)
      3. uitkomst in results\results.jsonl + results\results.csv
      4. optioneel resultTarget: oordeel als Basic Text SR (nieuwe serie, zelfde studie) per C-STORE
         terug, bv. naar PACS/JiveX of naar de ontvanger van de AIFI Gateway Tester
      5. optioneel forward: de originele beelden door naar de volgende bestemming (store-and-forward),
         eventueel alleen bij bepaalde oordelen; de rest gaat naar held\ voor handmatige controle
      6. pas daarna wordt de studie uit de spool verwijderd. Mislukt stap 4 of 5, dan volgt een
         nieuwe poging met oplopende wachttijd; na retry.maxAttempts gaat de studie naar failed\.

  AIFI-gateway --HTTP POST /classify {"path": ...}--> dicom_service (alleen 127.0.0.1)
      add-on: de gateway laat een gepseudonimiseerde studie in zijn pool classificeren en beslist
      zelf wat hij met het oordeel doet (README, sectie "Add-on in de AIFI-gateway").

Gebruik:
  python dicom_service.py --init-config classifier.yaml     standaardconfiguratie schrijven
  python dicom_service.py --config classifier.yaml --check  configuratie en model controleren
  python dicom_service.py --config classifier.yaml          starten (stoppen met Ctrl+C)
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pydicom
import yaml
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

log = logging.getLogger("dicom_service")

BASIC_TEXT_SR = "1.2.840.10008.5.1.4.1.1.88.11"
UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")

DEFAULT_CONFIG = r"""# dicom-classifier - DICOM-dienst
# Relatieve paden zijn relatief aan de map van dit bestand.

model:
  checkpoint: runs/v1/best.pt       # uit train.py
  device: auto                      # auto | cpu | cuda
  tauModality: 0.7                  # minimale zekerheid modaliteit
  tauBodyPart: 0.5                  # drempel per body-part-regio

# DICOM-ontvanger (C-STORE SCP + C-ECHO)
scp:
  enabled: true
  bindAddress: 0.0.0.0
  port: 11300
  aeTitle: AIFICLS
  allowedCallingAeTitles: []        # bv. [PACS, AIFITEST]; leeg = iedereen (niet aanbevolen)
  maxPduSize: 0                     # 0 = onbeperkt

study:
  completeAfterSeconds: 10          # stilte na het laatste beeld voordat de studie verwerkt wordt
  workers: 1                        # studies tegelijk (het model zelf rekent er een tegelijk)

spool:
  dir: spool                        # ontvangen beelden, tot ze verwerkt (en doorgestuurd) zijn
  failedDir: failed                 # studies die na retry.maxAttempts niet verwerkt konden worden
  minFreeMb: 1024                   # minder vrije ruimte: C-STORE weigeren (status A700)

results:
  dir: results                      # results.jsonl + results.csv (vertrouwelijk: bevat UID's)

# Oordeel als Basic Text SR terugsturen (nieuwe serie in dezelfde studie)
resultTarget:
  enabled: false
  host: 127.0.0.1
  port: 11200
  aeTitle: AIFITEST
  callingAeTitle: AIFICLS
  onlyVerdicts: []                  # leeg = altijd; bv. [afwijking, onzeker]

# Originele beelden doorsturen na classificatie (store-and-forward)
forward:
  enabled: false
  host: 127.0.0.1
  port: 11112
  aeTitle: AIFIGW
  callingAeTitle: AIFICLS
  verdicts: [akkoord, onzeker, afwijking, geen_beeld]   # andere oordelen gaan naar held\

held:
  dir: held                         # niet doorgestuurde studies, voor handmatige controle

# HTTP-API voor de add-on van de AIFI-gateway
api:
  enabled: true
  bindAddress: 127.0.0.1            # alleen lokaal; de gateway draait op dezelfde server
  port: 8765
  token: ""                         # optioneel: header X-Api-Token moet dit bevatten
  allowedRoots: []                  # mappen die geclassificeerd mogen worden, bv. [C:\aifi-gateway\pool]

retry:
  maxAttempts: 10
  initialDelaySeconds: 30           # verdubbelt per poging ...
  maxDelaySeconds: 3600             # ... tot maximaal een uur

network:
  connectTimeoutSeconds: 10
  acseTimeoutSeconds: 30
  dimseTimeoutSeconds: 120

logging:
  dir: logs
  level: INFO
"""


# ---------------------------------------------------------------- configuratie
def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        user = yaml.safe_load(fh) or {}
    cfg = _merge(yaml.safe_load(DEFAULT_CONFIG), user)
    base = os.path.dirname(os.path.abspath(path))
    for section, key in (("model", "checkpoint"), ("spool", "dir"), ("spool", "failedDir"), ("results", "dir"),
                         ("held", "dir"), ("logging", "dir")):
        p = cfg[section][key]
        cfg[section][key] = p if os.path.isabs(p) else os.path.join(base, p)
    cfg["api"]["allowedRoots"] = [os.path.realpath(r if os.path.isabs(r) else os.path.join(base, r))
                                  for r in cfg["api"]["allowedRoots"] or []]
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    errors = []
    for name in ("scp", "resultTarget", "forward", "api"):
        port = cfg[name].get("port")
        if not isinstance(port, int) or not 0 < port < 65536:
            errors.append(f"{name}.port moet een getal tussen 1 en 65535 zijn")
    for name in ("scp", "resultTarget", "forward"):
        for key in ("aeTitle", "callingAeTitle"):
            ae = cfg[name].get(key)
            if ae is not None and not (0 < len(str(ae)) <= 16 and "\\" not in str(ae)):
                errors.append(f"{name}.{key}: AE-titel van 1 tot 16 tekens")
    if cfg["api"]["enabled"] and cfg["api"]["bindAddress"] not in ("127.0.0.1", "localhost", "::1") \
            and not cfg["api"]["token"]:
        errors.append("api.bindAddress is niet lokaal: zet dan ook api.token")
    if cfg["api"]["enabled"] and cfg["api"]["bindAddress"] not in ("127.0.0.1", "localhost", "::1") \
            and not cfg["api"]["allowedRoots"]:
        errors.append("api.bindAddress is niet lokaal: zet dan ook api.allowedRoots")
    if not cfg["scp"]["enabled"] and not cfg["api"]["enabled"]:
        errors.append("scp en api staan allebei uit: er is niets te doen")
    if errors:
        raise ValueError("Ongeldige configuratie:\n  - " + "\n  - ".join(errors))


def setup_logging(cfg: dict) -> None:
    os.makedirs(cfg["logging"]["dir"], exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(os.path.join(cfg["logging"]["dir"], "dicom_service.log"),
                                              maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(cfg["logging"]["level"])
    root.handlers[:] = [fh, sh]
    logging.getLogger("pynetdicom").setLevel(logging.WARNING)


def ref(uid: str) -> str:
    """Korte, niet-terugrekenbare verwijzing naar een UID voor de log."""
    return "#" + hashlib.sha256((uid or "").encode()).hexdigest()[:10]


# ---------------------------------------------------------------- bestanden
def write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def rmtree_retry(path: str, attempts: int = 5) -> None:
    """Windows: een virusscanner of indexer houdt een net geschreven bestand soms kort open."""
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(0.5 * (i + 1))


def study_files(study_dir: str) -> list[str]:
    out = []
    for dirpath, _, names in os.walk(study_dir):
        out += [os.path.join(dirpath, n) for n in names if n.endswith(".dcm")]
    return sorted(out)


# ---------------------------------------------------------------- SR met het oordeel
_SR_TITLE = ("18748-4", "LN", "Diagnostic imaging report")
_CODES = {
    "verdict": ("CLS001", "99AIFI", "Classificatie-oordeel"),
    "series": ("CLS002", "99AIFI", "Geclassificeerde serie"),
    "summary": ("CLS003", "99AIFI", "Samenvatting"),
    "status": ("CLS004", "99AIFI", "Status"),
    "region": ("123014", "DCM", "Target Region"),
    "series_uid": ("112002", "DCM", "Series Instance UID"),
}


def _code(value: str, scheme: str, meaning: str) -> Dataset:
    c = Dataset()
    c.CodeValue, c.CodingSchemeDesignator, c.CodeMeaning = value, scheme, meaning[:64]
    return c


def _item(value_type: str, concept: tuple, **kw) -> Dataset:
    it = Dataset()
    it.RelationshipType = "CONTAINS"
    it.ValueType = value_type
    it.ConceptNameCodeSequence = [_code(*concept)]
    if value_type == "TEXT":
        it.TextValue = kw["text"]
    elif value_type == "UIDREF":
        it.UID = kw["uid"]
    elif value_type == "CODE":
        it.ConceptCodeSequence = [_code(*kw["code"])]
    elif value_type == "CONTAINER":
        it.ContinuityOfContent = "SEPARATE"
        it.ContentSequence = kw["children"]
    return it


def summary_line(r: dict) -> str:
    regions = ", ".join(r["pred_regions"]) or "onzeker"
    hdr = f"{r['header_modality_raw'] or '-'} / {r['header_body_part_raw'] or '-'}"
    return (f"Pixels: {r['pred_modality']} ({r['p_modality']:.0%}), {regions}"
            + (f" = {r['pred_cid4031_meaning']}" if r["pred_cid4031_meaning"] else "")
            + f". Header: {hdr}.")


def build_sr(doc: dict, study_header: Dataset, evidence: list[tuple[str, str, str]], station: str) -> Dataset:
    """Basic Text SR in dezelfde studie, met het oordeel en per serie de uitkomst.

    evidence: (SeriesInstanceUID, SOPClassUID, SOPInstanceUID) van de geclassificeerde beelden.
    """
    now = datetime.now()
    study_uid = str(study_header.StudyInstanceUID)
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = BASIC_TEXT_SR
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.SOPClassUID, ds.SOPInstanceUID = BASIC_TEXT_SR, ds.file_meta.MediaStorageSOPInstanceUID
    # Patient en studie: overgenomen, zodat het SR bij de juiste studie terechtkomt.
    for kw in ("PatientName", "PatientID", "PatientBirthDate", "PatientSex", "StudyDate", "StudyTime",
               "ReferringPhysicianName", "StudyID", "AccessionNumber", "StudyDescription", "IssuerOfPatientID"):
        setattr(ds, kw, getattr(study_header, kw, "") or "")
    ds.StudyInstanceUID = study_uid
    ds.Modality = "SR"
    # Vaste serie per studie: een tweede run komt in dezelfde serie in plaats van een nieuwe.
    ds.SeriesInstanceUID = generate_uid(entropy_srcs=[study_uid, "dicom-classifier-sr"])
    ds.SeriesNumber = 9901
    ds.SeriesDescription = "AI classificatie modaliteit/body part"
    ds.SeriesDate, ds.SeriesTime = now.strftime("%Y%m%d"), now.strftime("%H%M%S")
    ds.ReferencedPerformedProcedureStepSequence = []
    ds.Manufacturer = "AIFI dicom-classifier"
    ds.SoftwareVersions = doc["classifier"].get("version", "")
    ds.StationName = station[:16]
    ds.InstanceNumber = 1
    ds.ContentDate, ds.ContentTime = now.strftime("%Y%m%d"), now.strftime("%H%M%S")
    ds.InstanceCreationDate, ds.InstanceCreationTime = ds.ContentDate, ds.ContentTime
    ds.CompletionFlag, ds.VerificationFlag = "COMPLETE", "UNVERIFIED"
    ds.PerformedProcedureCodeSequence = []
    by_series: dict[str, list] = {}
    for series_uid, cls, sop in evidence:
        by_series.setdefault(series_uid, []).append((cls, sop))
    ev_study = Dataset()
    ev_study.StudyInstanceUID = study_uid
    ev_study.ReferencedSeriesSequence = []
    for series_uid, sops in by_series.items():
        s = Dataset()
        s.SeriesInstanceUID = series_uid
        s.ReferencedSOPSequence = []
        for cls, sop in sops:
            r = Dataset()
            r.ReferencedSOPClassUID, r.ReferencedSOPInstanceUID = cls, sop
            s.ReferencedSOPSequence.append(r)
        ev_study.ReferencedSeriesSequence.append(s)
    ds.CurrentRequestedProcedureEvidenceSequence = [ev_study]
    # Inhoud
    ds.ValueType = "CONTAINER"
    ds.ConceptNameCodeSequence = [_code(*_SR_TITLE)]
    ds.ContinuityOfContent = "SEPARATE"
    content = [_item("TEXT", _CODES["verdict"], text=doc["verdict"])]
    for r in doc["series"]:
        children = [_item("UIDREF", _CODES["series_uid"], uid=r["series_uid"]),
                    _item("TEXT", _CODES["summary"], text=summary_line(r)),
                    _item("TEXT", _CODES["status"], text=r["status"])]
        if r["pred_cid4031_code"]:
            children.append(_item("CODE", _CODES["region"],
                                  code=(r["pred_cid4031_code"], "SCT", r["pred_cid4031_meaning"])))
        content.append(_item("CONTAINER", _CODES["series"], children=children))
    ds.ContentSequence = content
    return ds


# ---------------------------------------------------------------- versturen (C-STORE SCU)
class SendError(Exception):
    pass


def make_ae(cfg: dict, calling: str):
    from pynetdicom import AE
    ae = AE(ae_title=calling)
    net = cfg["network"]
    ae.connection_timeout = net["connectTimeoutSeconds"]
    ae.acse_timeout = net["acseTimeoutSeconds"]
    ae.dimse_timeout = net["dimseTimeoutSeconds"]
    ae.network_timeout = net["dimseTimeoutSeconds"]
    return ae


def send_datasets(cfg: dict, target: dict, items: list) -> int:
    """C-STORE van bestanden (pad) of datasets naar target over één associatie. Geeft aantal terug."""
    from pydicom.uid import ImplicitVRLittleEndian
    if not items:
        return 0
    contexts: dict[str, set] = {}
    for it in items:
        ds = it if isinstance(it, Dataset) else pydicom.dcmread(it, stop_before_pixels=True)
        cls = str(ds.file_meta.MediaStorageSOPClassUID)
        ts = str(ds.file_meta.TransferSyntaxUID)
        contexts.setdefault(cls, set()).add(ts)
    if len(contexts) > 120:
        raise SendError(f"te veel verschillende SOP-klassen ({len(contexts)}) voor één associatie")
    ae = make_ae(cfg, target["callingAeTitle"])
    for cls, tss in contexts.items():
        ae.add_requested_context(cls, sorted(tss | {ExplicitVRLittleEndian, ImplicitVRLittleEndian}))
    assoc = ae.associate(target["host"], int(target["port"]), ae_title=target["aeTitle"])
    if not assoc.is_established:
        raise SendError(f"geen associatie met {target['aeTitle']}@{target['host']}:{target['port']}")
    n = 0
    try:
        for it in items:
            status = assoc.send_c_store(it)
            code = getattr(status, "Status", None)
            if code is None:
                raise SendError("geen antwoord op C-STORE (time-out of verbinding verbroken)")
            if code != 0x0000 and not (0xB000 <= code <= 0xBFFF):   # Bxxx = waarschuwing, telt als gelukt
                raise SendError(f"C-STORE geweigerd met status 0x{code:04X}")
            n += 1
    finally:
        assoc.release()
    return n


# ---------------------------------------------------------------- de dienst
class Service:
    def __init__(self, cfg: dict, classifier):
        self.cfg, self.clf = cfg, classifier
        self.spool = cfg["spool"]["dir"]
        self.failed_dir = cfg["spool"]["failedDir"]
        for d in (self.spool, self.failed_dir, cfg["results"]["dir"], cfg["held"]["dir"]):
            os.makedirs(d, exist_ok=True)
        self.last_arrival: dict[str, float] = {}
        self.calling: dict[str, str] = {}
        self.running: set[str] = set()
        self.lock = threading.Lock()
        self.results_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max(1, int(cfg["study"]["workers"])),
                                       thread_name_prefix="study")
        self.stop_event = threading.Event()
        self.scp_server = None
        self.http_server = None
        self.stats = {"received": 0, "studies": 0, "failed": 0}

    # ------------------------------------------------------------ ontvangen
    def handle_store(self, event):
        try:
            ds = event.dataset
            study, series, sop = (str(getattr(ds, k, "") or "") for k in
                                  ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"))
        except Exception as exc:
            log.warning("Onleesbaar object van %s: %s", event.assoc.requestor.ae_title, exc)
            return 0xC210  # cannot understand
        if not all(UID_RE.match(u) and len(u) <= 64 for u in (study, series, sop)):
            log.warning("Object zonder geldige Study/Series/SOP Instance UID geweigerd (van %s)",
                        event.assoc.requestor.ae_title)
            return 0xC211
        free_mb = shutil.disk_usage(self.spool).free // (1024 * 1024)
        if free_mb < self.cfg["spool"]["minFreeMb"]:
            log.error("Schijf bijna vol (%d MB vrij): C-STORE geweigerd", free_mb)
            return 0xA700
        d = os.path.join(self.spool, study, series)
        try:
            os.makedirs(d, exist_ok=True)
            write_atomic(os.path.join(d, sop + ".dcm"), event.encoded_dataset(include_meta=True))
        except OSError as exc:
            log.error("Opslaan mislukt: %s", exc)
            return 0xA700
        with self.lock:
            first = study not in self.last_arrival
            self.last_arrival[study] = time.monotonic()
            self.calling[study] = str(event.assoc.requestor.ae_title).strip()
            self.stats["received"] += 1
        if first:
            log.info("Studie %s: ontvangen van %s", ref(study), self.calling[study])
        return 0x0000

    def start_scp(self):
        from pynetdicom import ALL_TRANSFER_SYNTAXES, AllStoragePresentationContexts, evt
        from pynetdicom.sop_class import Verification
        c = self.cfg["scp"]
        ae = make_ae(self.cfg, c["aeTitle"])
        ae.require_called_aet = True
        if c["allowedCallingAeTitles"]:
            ae.require_calling_aet = [str(a) for a in c["allowedCallingAeTitles"]]
        if c["maxPduSize"]:
            ae.maximum_pdu_size = int(c["maxPduSize"])
        for cx in AllStoragePresentationContexts:
            ae.add_supported_context(cx.abstract_syntax, ALL_TRANSFER_SYNTAXES)
        ae.add_supported_context(Verification)
        self.scp_server = ae.start_server((c["bindAddress"], int(c["port"])), block=False,
                                          evt_handlers=[(evt.EVT_C_STORE, self.handle_store)])
        log.info("DICOM-ontvanger: AE %s op %s:%s", c["aeTitle"], c["bindAddress"], c["port"])

    # ------------------------------------------------------------ plannen
    def recover(self):
        """Na een herstart: alles wat nog in de spool staat opnieuw inplannen."""
        n = 0
        for name in os.listdir(self.spool):
            if UID_RE.match(name) and os.path.isdir(os.path.join(self.spool, name)):
                self.last_arrival.setdefault(name, 0.0)
                n += 1
        if n:
            log.info("%d studie(s) uit de spool hervat", n)

    def state_path(self, study: str) -> str:
        return os.path.join(self.spool, study, "state.json")

    def read_state(self, study: str) -> dict:
        try:
            with open(self.state_path(study), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"attempts": 0, "next_at": 0}

    def write_state(self, study: str, state: dict) -> None:
        write_atomic(self.state_path(study), json.dumps(state).encode())

    def tick(self):
        now = time.monotonic()
        quiet = float(self.cfg["study"]["completeAfterSeconds"])
        due = []
        with self.lock:
            for study, t in list(self.last_arrival.items()):
                if study in self.running or now - t < quiet:
                    continue
                if self.read_state(study).get("next_at", 0) > time.time():
                    continue
                self.running.add(study)
                due.append(study)
        for study in due:
            self.pool.submit(self.process_safely, study)

    def loop(self):
        while not self.stop_event.wait(1.0):
            try:
                self.tick()
            except Exception:
                log.exception("Planner")

    # ------------------------------------------------------------ verwerken
    def process_safely(self, study: str):
        try:
            self.process(study)
        except Exception as exc:
            self.failed_attempt(study, f"{type(exc).__name__}: {exc}")
        finally:
            with self.lock:
                self.running.discard(study)

    def process(self, study: str):
        study_dir = os.path.join(self.spool, study)
        files = study_files(study_dir)
        with self.lock:
            started = self.last_arrival.get(study, 0.0)
        if not files:
            rmtree_retry(study_dir)
            with self.lock:
                self.last_arrival.pop(study, None)
            return
        t0 = time.monotonic()
        doc = self.clf.classify_study(study_dir)
        doc.update(study_uid=study, received_from=self.calling.get(study, ""), n_instances=len(files),
                   processed_at=datetime.now().isoformat(timespec="seconds"))
        log.info("Studie %s: %d beeld(en), %d beeldserie(s), oordeel %s (%.1f s)", ref(study), len(files),
                 len(doc["series"]), doc["verdict"], time.monotonic() - t0)
        state = self.read_state(study)
        if not state.get("recorded"):
            self.record(doc)
            state["recorded"] = True
            self.write_state(study, state)

        rt = self.cfg["resultTarget"]
        if rt["enabled"] and doc["series"] and not state.get("sr_sent") and \
                (not rt["onlyVerdicts"] or doc["verdict"] in rt["onlyVerdicts"]):
            header = pydicom.dcmread(files[0], stop_before_pixels=True)
            evidence = []
            for f in files:
                h = pydicom.dcmread(f, stop_before_pixels=True,
                                    specific_tags=["SeriesInstanceUID", "SOPClassUID", "SOPInstanceUID"])
                evidence.append((str(h.SeriesInstanceUID), str(h.SOPClassUID), str(h.SOPInstanceUID)))
            sr = build_sr(doc, header, evidence, self.cfg["scp"]["aeTitle"])
            send_datasets(self.cfg, rt, [sr])
            state["sr_sent"] = True
            self.write_state(study, state)
            log.info("Studie %s: SR met oordeel verstuurd naar %s", ref(study), rt["aeTitle"])

        fw = self.cfg["forward"]
        target_dir = None
        if fw["enabled"]:
            if doc["verdict"] in fw["verdicts"]:
                n = send_datasets(self.cfg, fw, files)
                log.info("Studie %s: %d beeld(en) doorgestuurd naar %s", ref(study), n, fw["aeTitle"])
            else:
                target_dir = os.path.join(self.cfg["held"]["dir"], study)

        with self.lock:
            more = self.last_arrival.get(study, 0.0) > started
        if more:
            # Er kwamen beelden bij tijdens de verwerking: de hele studie nog een keer (UID's zijn gelijk,
            # dus de ontvanger overschrijft wat hij al had). Het SR en de registratie worden herhaald.
            self.write_state(study, {"attempts": 0, "next_at": 0})
            log.info("Studie %s: nieuwe beelden tijdens verwerking, wordt opnieuw verwerkt", ref(study))
            return
        if target_dir:
            if os.path.exists(target_dir):
                rmtree_retry(target_dir)
            shutil.move(study_dir, target_dir)
            log.warning("Studie %s: oordeel %s, niet doorgestuurd; staat in %s", ref(study), doc["verdict"],
                        target_dir)
        else:
            rmtree_retry(study_dir)
        with self.lock:
            self.last_arrival.pop(study, None)
            self.calling.pop(study, None)
            self.stats["studies"] += 1

    def failed_attempt(self, study: str, message: str):
        state = self.read_state(study)
        state["attempts"] = state.get("attempts", 0) + 1
        state["last_error"] = message
        r = self.cfg["retry"]
        if r["maxAttempts"] and state["attempts"] >= r["maxAttempts"]:
            dest = os.path.join(self.failed_dir, study)
            if os.path.exists(dest):
                rmtree_retry(dest)
            self.write_state(study, state)
            shutil.move(os.path.join(self.spool, study), dest)
            with self.lock:
                self.last_arrival.pop(study, None)
                self.stats["failed"] += 1
            log.error("Studie %s: MISLUKT na %d pogingen, verplaatst naar %s: %s", ref(study),
                      state["attempts"], dest, message)
            return
        delay = min(r["maxDelaySeconds"], r["initialDelaySeconds"] * 2 ** (state["attempts"] - 1))
        state["next_at"] = time.time() + delay
        self.write_state(study, state)
        log.warning("Studie %s: poging %d mislukt, nieuwe poging over %d s: %s", ref(study),
                    state["attempts"], delay, message)

    def record(self, doc: dict):
        from engine import CSV_FIELDS, to_csv_row
        d = self.cfg["results"]["dir"]
        with self.results_lock:
            with open(os.path.join(d, "results.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
            path = os.path.join(d, "results.csv")
            fields = ["processed_at", "study_uid", "verdict"] + CSV_FIELDS
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore", delimiter=";")
                if new:
                    w.writeheader()
                for r in doc["series"] or [{}]:
                    row = to_csv_row(r) if r else {}
                    row.update(processed_at=doc["processed_at"], study_uid=doc["study_uid"], verdict=doc["verdict"])
                    w.writerow(row)

    # ------------------------------------------------------------ HTTP-API
    def path_allowed(self, path: str) -> bool:
        roots = self.cfg["api"]["allowedRoots"]
        if not roots:
            return True
        real = os.path.realpath(path)
        for root in roots:
            try:
                if os.path.commonpath([real, root]) == root:
                    return True
            except ValueError:  # andere schijf (Windows)
                continue
        return False

    def start_api(self):
        service = self
        c = self.cfg["api"]

        class Handler(BaseHTTPRequestHandler):
            server_version = "dicom-classifier"

            def log_message(self, fmt, *args):
                log.debug("api %s " + fmt, self.client_address[0], *args)

            def reply(self, code: int, body: dict):
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorised(self) -> bool:
                token = c["token"]
                if token and self.headers.get("X-Api-Token", "") != token:
                    self.reply(401, {"error": "ongeldige of ontbrekende X-Api-Token"})
                    return False
                return True

            def do_GET(self):
                if not self.authorised():
                    return
                if self.path == "/health":
                    self.reply(200, {"status": "ok", "classifier": service.clf.info, "stats": service.stats})
                else:
                    self.reply(404, {"error": "onbekend pad"})

            def do_POST(self):
                if not self.authorised():
                    return
                if self.path != "/classify":
                    self.reply(404, {"error": "onbekend pad"})
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if n > 1_000_000:
                        raise ValueError("verzoek te groot")
                    req = json.loads(self.rfile.read(n) or b"{}")
                    path = req["path"]
                except (ValueError, KeyError, TypeError) as exc:
                    self.reply(400, {"error": f"verwacht JSON {{\"path\": ...}}: {exc}"})
                    return
                if not os.path.isdir(path):
                    self.reply(404, {"error": "map bestaat niet"})
                    return
                if not service.path_allowed(path):
                    log.warning("API: map buiten api.allowedRoots geweigerd")
                    self.reply(403, {"error": "map valt buiten api.allowedRoots"})
                    return
                t0 = time.monotonic()
                try:
                    doc = service.clf.classify_study(path)
                except Exception as exc:
                    log.exception("API: classificatie mislukt")
                    self.reply(500, {"error": f"{type(exc).__name__}: {exc}"})
                    return
                doc["elapsed_seconds"] = round(time.monotonic() - t0, 2)
                log.info("API: %s %s -> %s (%d serie(s), %.1f s)", req.get("route", "-"),
                         ref(req.get("studyInstanceUid", "")), doc["verdict"], len(doc["series"]),
                         doc["elapsed_seconds"])
                self.reply(200, doc)

        self.http_server = ThreadingHTTPServer((c["bindAddress"], int(c["port"])), Handler)
        threading.Thread(target=self.http_server.serve_forever, name="api", daemon=True).start()
        log.info("HTTP-API: http://%s:%s/classify", c["bindAddress"], c["port"])

    # ------------------------------------------------------------ starten / stoppen
    def start(self):
        if self.cfg["scp"]["enabled"]:
            self.recover()
            self.start_scp()
            threading.Thread(target=self.loop, name="planner", daemon=True).start()
        if self.cfg["api"]["enabled"]:
            self.start_api()

    def stop(self):
        self.stop_event.set()
        if self.scp_server:
            self.scp_server.shutdown()
        if self.http_server:
            self.http_server.shutdown()
        self.pool.shutdown(wait=True)
        log.info("Gestopt. Ontvangen: %d beeld(en), verwerkt: %d studie(s), mislukt: %d",
                 self.stats["received"], self.stats["studies"], self.stats["failed"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="classifier.yaml")
    ap.add_argument("--init-config", metavar="PAD", help="standaardconfiguratie schrijven en stoppen")
    ap.add_argument("--check", action="store_true", help="configuratie en model controleren en stoppen")
    args = ap.parse_args(argv)
    if args.init_config:
        if os.path.exists(args.init_config):
            sys.exit(f"{args.init_config} bestaat al; niet overschreven")
        with open(args.init_config, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_CONFIG)
        print(f"{args.init_config} geschreven. Vul model.checkpoint in en start met --config {args.init_config}")
        return
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.exit(f"Configuratie {args.config}: {exc}")
    setup_logging(cfg)
    if not os.path.isfile(cfg["model"]["checkpoint"]):
        sys.exit(f"Model niet gevonden: {cfg['model']['checkpoint']} (model.checkpoint)")
    from engine import Classifier
    m = cfg["model"]
    clf = Classifier(m["checkpoint"], m["device"], m["tauModality"], m["tauBodyPart"])
    if args.check:
        print("Configuratie en model in orde.")
        return
    service = Service(cfg, clf)
    service.start()
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: done.set())
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl+Break en het sluiten van het consolevenster
        signal.signal(signal.SIGBREAK, lambda *_: done.set())
    log.info("dicom-classifier dienst gestart (Ctrl+C om te stoppen)")
    while not done.wait(0.5):
        pass
    service.stop()


if __name__ == "__main__":
    main()
