"""Label-taxonomie: modaliteitklassen, body-part-regio's en de koppeling naar DICOM CID 4031.

Alles wat je per ziekenhuis wilt bijstellen (synoniemen, drempels) staat in dit bestand.

Bron SNOMED-codes: DICOM PS3.16, CID 4031 "Common Anatomic Region" (versie 20250709),
https://dicom.nema.org/medical/dicom/current/output/chtml/part16/sect_CID_4031.html
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------------------
# Modaliteit
# --------------------------------------------------------------------------------------
# Modelklassen. Volgorde = index in de modaliteit-head. Niet wijzigen na training.
MODALITY_CLASSES = ["CT", "MR", "DX", "MG", "US", "NM", "PT", "XA", "OT"]

# DICOM Modality (0008,0060) -> modelklasse.
MODALITY_MAP = {
    "CT": "CT",
    "MR": "MR",
    "CR": "DX", "DX": "DX", "RG": "DX", "DR": "DX", "PX": "DX", "IO": "DX",
    "MG": "MG",
    "US": "US", "IVUS": "US",
    "NM": "NM",
    "PT": "PT",
    "XA": "XA", "RF": "XA",
    "SC": "OT", "OT": "OT", "XC": "OT", "ES": "OT", "OP": "OT",
}

# Modaliteiten zonder diagnostisch beeld: deze series worden overgeslagen.
NON_IMAGE_MODALITIES = {
    "SR", "KO", "PR", "SEG", "REG", "RTSTRUCT", "RTPLAN", "RTDOSE", "RTRECORD",
    "RTIMAGE", "DOC", "AU", "ECG", "HD", "PLAN", "FID", "RWV",
}


def map_modality(raw: str | None) -> str | None:
    """Header-modaliteit -> modelklasse. None = overslaan (geen beeld)."""
    raw = (raw or "").strip().upper()
    if raw in NON_IMAGE_MODALITIES:
        return None
    return MODALITY_MAP.get(raw, "OT")


# --------------------------------------------------------------------------------------
# Body part
# --------------------------------------------------------------------------------------
# Basisregio's voor de multi-label body-part-head. Een serie kan meerdere regio's
# bevatten (bv. thorax + abdomen); de combinatie wordt daarna op een CID 4031-code gezet.
REGIONS = ["head", "neck", "chest", "abdomen", "pelvis", "spine",
           "upper_limb", "lower_limb", "breast"]

# Combinatie van regio's -> (SNOMED CT-code, Code Meaning) uit CID 4031.
# De meest specifieke combinatie die past wint (zie regions_to_cid4031).
CID4031 = {
    frozenset({"head"}): ("69536005", "Head"),
    frozenset({"neck"}): ("45048000", "Neck"),
    frozenset({"head", "neck"}): ("774007", "Head and Neck"),
    frozenset({"chest"}): ("816094009", "Chest"),
    frozenset({"abdomen"}): ("818981001", "Abdomen"),
    frozenset({"pelvis"}): ("816092008", "Pelvis"),
    frozenset({"abdomen", "pelvis"}): ("818982008", "Abdomen and Pelvis"),
    frozenset({"chest", "abdomen"}): ("416550000", "Chest and Abdomen"),
    frozenset({"chest", "abdomen", "pelvis"}): ("416775004", "Chest, Abdomen and Pelvis"),
    frozenset({"neck", "chest"}): ("417437006", "Neck and Chest"),
    frozenset({"neck", "chest", "abdomen"}): ("416152001", "Neck, Chest and Abdomen"),
    frozenset({"neck", "chest", "abdomen", "pelvis"}): ("416319003", "Neck, Chest, Abdomen and Pelvis"),
    frozenset({"pelvis", "lower_limb"}): ("1231522001", "Pelvis and lower extremities"),
    frozenset({"spine"}): ("421060004", "Spine"),
    frozenset({"upper_limb"}): ("53120007", "Upper limb"),
    frozenset({"lower_limb"}): ("61685007", "Lower limb"),
    frozenset({"breast"}): ("76752008", "Breast"),
}
ENTIRE_BODY = ("38266002", "Entire body")


def regions_to_cid4031(regions: set[str]) -> tuple[str, str] | None:
    """Regio-set -> (SNOMED-code, betekenis). None als de set leeg is.

    Regels: hoofd t/m bekken aanwezig -> Entire body. Anders exacte match.
    Anders de grootste CID-combinatie die volledig binnen de set valt.
    """
    regions = frozenset(regions)
    if not regions:
        return None
    if {"head", "chest", "abdomen", "pelvis"} <= regions:
        return ENTIRE_BODY
    if regions in CID4031:
        return CID4031[regions]
    subsets = [k for k in CID4031 if k <= regions]
    if not subsets:
        return None
    best = max(subsets, key=len)
    return CID4031[best]


# Synoniemen voor de vrije-tekst-tag BodyPartExamined (0018,0015).
# Engelse DICOM Defined Terms + gangbare Nederlandse termen. Uitbreiden met wat je
# in je eigen data ziet: build_dataset.py zet alle onherkende waarden in review.csv.
# Patronen worden gematcht op de genormaliseerde tekst (hoofdletters, alleen A-Z0-9 en spaties).
_SYN = {
    "head": [r"HEAD", r"BRAIN", r"SKULL", r"CRANI", r"HOOFD", r"HERSEN", r"SCHEDEL",
             r"\bSINUS", r"\bFACE\b", r"GELAAT", r"ORBIT", r"\bIAC\b", r"\bEAR\b", r"\bOOR\b"],
    "neck": [r"NECK", r"\bHALS\b", r"THYROID", r"SCHILDKLIER", r"LARYNX", r"CAROTID"],
    "chest": [r"CHEST", r"THORAX", r"\bLUNG", r"\bLONG(EN)?\b", r"\bHEART\b", r"\bHART\b",
              r"CARDIAC", r"CORONAR", r"\bRIBS?\b", r"STERNUM", r"MEDIAST", r"\bTHX\b"],
    "abdomen": [r"ABDOM", r"\bBUIK\b", r"\bLIVER\b", r"\bLEVER\b", r"KIDNEY", r"\bNIER", r"PANCREA",
                r"\bMAAG\b", r"STOMACH", r"\bGALBLAAS", r"\bABD\b"],
    "pelvis": [r"PELVIS", r"BEKKEN", r"\bHIPS?\b", r"\bHEUP", r"BLADDER", r"BLAAS", r"PROSTA",
               r"UTERUS", r"\bSI\b", r"SACROIL", r"\bPELV\b"],
    "spine": [r"SPINE", r"WERVEL", r"\b[CTLS]SPINE", r"\b[CTL]WK\b", r"VERTEB", r"SACRUM", r"COCCYX",
              r"STUITJE"],
    "upper_limb": [r"SHOULDER", r"SCHOUDER", r"\bARM\b", r"ELBOW", r"ELLEBOOG", r"WRIST", r"\bPOLS\b",
                   r"\bHAND\b", r"FINGER", r"VINGER", r"THUMB", r"DUIM", r"HUMERUS", r"FOREARM",
                   r"ONDERARM", r"BOVENARM", r"CLAVIC", r"SCAPUL", r"UPPEREXM", r"\bUPPER EXTREMITY"],
    "lower_limb": [r"\bKNEE", r"\bKNIE", r"\bLEG\b", r"\bBEEN\b", r"ANKLE", r"\bENKEL", r"\bFOOT\b",
                   r"\bVOET", r"FEMUR", r"TIBIA", r"FIBULA", r"\bTOES?\b", r"\bTENEN\b", r"PATELLA",
                   r"CALCAN", r"HIEL", r"BOVENBEEN", r"ONDERBEEN", r"LOWEXM", r"\bLOWER EXTREMITY"],
    "breast": [r"BREAST", r"MAMMA", r"\bMAMMO"],
}
# Samengestelde DICOM Defined Terms die als één woord binnenkomen.
_COMBOS = {
    r"CHEST.?ABD(OMEN)?.?PELV": {"chest", "abdomen", "pelvis"},
    r"(CHEST|THORAX).?(TO|T M|TOT).?(PELV|BEKKEN)": {"chest", "abdomen", "pelvis"},
    r"\bCAP\b": {"chest", "abdomen", "pelvis"},
    r"THORAX.?ABD": {"chest", "abdomen"},
    r"ABD(OMEN)?.?PELV": {"abdomen", "pelvis"},
    r"HEAD.?NECK|HEADNECK|HOOFD.?HALS": {"head", "neck"},
    r"WHOLE.?BODY|WHOLEBODY|TOTAL.?BODY|TOTAAL.?LICHAAM|\bWB\b": {"head", "neck", "chest", "abdomen", "pelvis"},
    r"\bCSPINE\b": {"spine", "neck"},
}
_SYN_RE = {r: [re.compile(p) for p in pats] for r, pats in _SYN.items()}
_COMBO_RE = [(re.compile(p), s) for p, s in _COMBOS.items()]


def _normalise(text: str) -> str:
    t = (text or "").upper().replace("_", " ").replace("-", " ").replace("^", " ")
    return re.sub(r"[^A-Z0-9 ]", " ", t)


def header_to_regions(body_part_examined: str | None, modality_class: str | None = None,
                      extra_text: str | None = None) -> set[str]:
    """BodyPartExamined (+ optioneel extra tekst zoals StudyDescription) -> regio-set.

    Lege set betekent onbekend. Voor MG wordt 'breast' aangenomen als er niets is gevonden.
    """
    regions: set[str] = set()
    for text in (body_part_examined, extra_text):
        if not text:
            continue
        t = _normalise(text)
        for rx, s in _COMBO_RE:
            if rx.search(t):
                regions |= s
        for region, pats in _SYN_RE.items():
            if any(p.search(t) for p in pats):
                regions.add(region)
    if not regions and modality_class == "MG":
        regions.add("breast")
    return regions


# --------------------------------------------------------------------------------------
# TotalSegmentator-structuren -> regio's (alleen gebruikt door autolabel_totalseg.py)
# --------------------------------------------------------------------------------------
# Per regio: welke structuren tellen mee, en vanaf welk totaalvolume (ml) geldt de regio
# als 'in beeld'. De drempels zijn STARTWAARDEN zonder bron: kalibreer ze op je eigen data
# met review.csv. Ze bestaan omdat een abdomen-CT vaak longbases bevat en een thorax-CT
# vaak een stukje lever of humerus.
TOTALSEG_REGION_RULES = {
    "head": {"patterns": [r"^brain$", r"^skull$"], "min_ml": 300.0},
    "neck": {"patterns": [r"^thyroid_gland$", r"^common_carotid_artery", r"^vertebrae_C[3-7]$"], "min_ml": 15.0},
    "chest": {"patterns": [r"^lung", r"^heart$"], "min_ml": 1200.0},
    "abdomen": {"patterns": [r"^liver$", r"^spleen$", r"^kidney_(left|right)$", r"^pancreas$"], "min_ml": 600.0},
    "pelvis": {"patterns": [r"^urinary_bladder$", r"^hip_(left|right)$", r"^sacrum$"], "min_ml": 250.0},
    "lower_limb": {"patterns": [r"^femur_(left|right)$"], "min_ml": 400.0},
    "upper_limb": {"patterns": [r"^humerus_(left|right)$"], "min_ml": 250.0},
    # 'spine' en 'breast' worden NIET uit TotalSegmentator afgeleid: wervels staan op
    # vrijwel elke romp-CT, dus dat zou elke thorax/abdomen ook 'spine' maken.
}
