"""
Official 70 Barangays of the City of Maasin, Southern Leyte.
PSGC Locality Code: 086407000
"""

MAASIN_BARANGAYS = [
    "Abgao",
    "Asuncion",
    "Bactul I",
    "Bactul II",
    "Badiang",
    "Bagtican",
    "Basak",
    "Bato I",
    "Bato II",
    "Batuan",
    "Baugo",
    "Bilibol",
    "Bogo",
    "Cabadiangan",
    "Cabulihan",
    "Cagnituan",
    "Cambooc",
    "Cansirong",
    "Canturing",
    "Canyuom",
    "Combado",
    "Dongon",
    "Gawisan",
    "Guadalupe",
    "Hanginan",
    "Hantag",
    "Hinapu Daku",
    "Hinapu Gamay",
    "Ibarra",
    "Isagani",
    "Laboon",
    "Lanao",
    "Lib-og",
    "Libhu",
    "Libertad",
    "Lonoy",
    "Lunas",
    "Mahayahay",
    "Malapoc Norte",
    "Malapoc Sur",
    "Mambajao",
    "Manhilo",
    "Mantahan",
    "Maria Clara",
    "Matin-ao",
    "Nasaug",
    "Nati",
    "Nonok Norte",
    "Nonok Sur",
    "Panan-awan",
    "Pansaan",
    "Pasay",
    "Pinaskuhan",
    "Rizal",
    "San Agustin",
    "San Isidro",
    "San Jose",
    "San Rafael",
    "Santa Cruz",
    "Santa Rosa",
    "Santo Niño",
    "Santo Rosario",
    "Sisi",
    "Tagnipa",
    "Tam-is",
    "Tawid",
    "Tigbawan",
    "Tomoy-tomoy",
    "Tunga-tunga",
    "Zaragosa",
]

# Set for O(1) case-insensitive and exact validation
MAASIN_BARANGAYS_SET = {b.strip().lower(): b for b in MAASIN_BARANGAYS}


def validate_and_normalize_barangay(barangay_name: str | None) -> str:
    """
    Validate that a given barangay string matches one of the 70 official barangays of Maasin City.
    Returns the properly cased official name.
    Raises ValueError if invalid.
    """
    if not barangay_name or not barangay_name.strip():
        raise ValueError("Barangay name cannot be empty.")
    
    clean = barangay_name.strip().lower()
    if clean in MAASIN_BARANGAYS_SET:
        return MAASIN_BARANGAYS_SET[clean]
    
    raise ValueError(f"'{barangay_name}' is not a valid barangay in Maasin City.")


# Official 2020 PSA Census Population by Barangay (City of Maasin, Southern Leyte)
# Total Population: 87,446
MAASIN_BARANGAY_POPULATION = {
    "Abgao": 2510,
    "Acasia": 343,
    "Asuncion": 5063,
    "Bactul I": 552,
    "Bactul II": 581,
    "Badiang": 1264,
    "Bagtican": 675,
    "Basak": 780,
    "Bato I": 708,
    "Bato II": 515,
    "Batuan": 1098,
    "Baugo": 559,
    "Bilibol": 1095,
    "Bogo": 808,
    "Cabadiangan": 440,
    "Cabulihan": 1258,
    "Cagnituan": 356,
    "Cambooc": 767,
    "Cansirong": 963,
    "Canturing": 1138,
    "Canyuom": 639,
    "Combado": 5854,
    "Dongon": 1472,
    "Gawisan": 522,
    "Guadalupe": 1702,
    "Hanginan": 800,
    "Hantag": 788,
    "Hinapu Daku": 942,
    "Hinapu Gamay": 871,
    "Ibarra": 4346,
    "Isagani": 1289,
    "Laboon": 1604,
    "Lanao": 547,
    "Lib-og": 1580,
    "Libhu": 1785,
    "Libertad": 432,
    "Lonoy": 764,
    "Lunas": 1702,
    "Mahayahay": 1223,
    "Malapoc Norte": 927,
    "Malapoc Sur": 451,
    "Mambajao": 1948,
    "Manhilo": 1661,
    "Mantahan": 4106,
    "Maria Clara": 2406,
    "Matin-ao": 644,
    "Nasaug": 728,
    "Nati": 335,
    "Nonok Norte": 515,
    "Nonok Sur": 678,
    "Panan-awan": 1304,
    "Pansaan": 542,
    "Pasay": 1014,
    "Pinaskuhan": 552,
    "Pinascohan": 552,
    "Rizal": 2061,
    "San Agustin": 465,
    "San Isidro": 568,
    "San Jose": 1102,
    "San Rafael": 1862,
    "Santa Cruz": 871,
    "Santa Rosa": 228,
    "Santo Niño": 723,
    "Santo Rosario": 1016,
    "Sisi": 1189,
    "Soro-soro": 1189,
    "Tagnipa": 3266,
    "Tam-is": 1491,
    "Tawid": 932,
    "Tigbawan": 1082,
    "Tomoy-tomoy": 491,
    "Tunga-tunga": 1953,
    "Zaragosa": 350,
}

MAX_BARANGAY_POPULATION = 5854  # Combado
DEFAULT_BARANGAY_POPULATION = 1000

# Runtime population overrides (in-memory cache, persisted to Supabase)
_population_overrides: dict[str, int] = {}
_overrides_loaded: bool = False

import logging as _logging
_pop_logger = _logging.getLogger("population_overrides")


def _load_overrides_from_db() -> dict[str, int]:
    """Load population overrides from Supabase app_settings table."""
    global _population_overrides, _overrides_loaded, MAX_BARANGAY_POPULATION
    try:
        from core.database import get_supabase
        sb = get_supabase()
        res = sb.table("app_settings").select("value").eq("key", "population_overrides").limit(1).execute()
        if res.data and len(res.data) > 0:
            import json
            raw = res.data[0].get("value")
            if isinstance(raw, str):
                raw = json.loads(raw)
            if isinstance(raw, dict):
                _population_overrides = {k: int(v) for k, v in raw.items() if v is not None}
                merged = {**MAASIN_BARANGAY_POPULATION, **_population_overrides}
                MAX_BARANGAY_POPULATION = max(merged.values()) if merged else 5854
                _pop_logger.info(f"Loaded {len(_population_overrides)} population overrides from database.")
    except Exception as e:
        _pop_logger.warning(f"Could not load population overrides from DB (table may not exist yet): {e}")
    _overrides_loaded = True
    return _population_overrides


def _save_overrides_to_db(overrides: dict[str, int]) -> None:
    """Persist population overrides to Supabase app_settings table."""
    try:
        from core.database import get_supabase
        import json
        sb = get_supabase()
        sb.table("app_settings").upsert(
            {"key": "population_overrides", "value": json.dumps(overrides)},
            on_conflict="key"
        ).execute()
        _pop_logger.info(f"Saved {len(overrides)} population overrides to database.")
    except Exception as e:
        _pop_logger.error(f"Failed to save population overrides to DB: {e}")


def get_population_overrides() -> dict[str, int]:
    """Return the current population overrides dict (loads from DB on first call)."""
    global _overrides_loaded
    if not _overrides_loaded:
        _load_overrides_from_db()
    return dict(_population_overrides)


def set_population_overrides(overrides: dict[str, int]) -> None:
    """Replace the population overrides, update MAX, and persist to database."""
    global _population_overrides, MAX_BARANGAY_POPULATION
    _population_overrides = {k: int(v) for k, v in overrides.items() if v is not None}
    # Recalculate max from merged census + overrides
    merged = {**MAASIN_BARANGAY_POPULATION, **_population_overrides}
    MAX_BARANGAY_POPULATION = max(merged.values()) if merged else 5854
    # Persist to database
    _save_overrides_to_db(_population_overrides)


def get_barangay_population(barangay_name: str | None) -> int:
    """Return the population for a given Maasin barangay, checking overrides first."""
    if not barangay_name or not str(barangay_name).strip():
        return DEFAULT_BARANGAY_POPULATION
    
    clean = str(barangay_name).strip().lower()

    # Check overrides first (exact match)
    for name, pop in _population_overrides.items():
        if name.lower() == clean:
            return pop

    # Then check census defaults
    for name, pop in MAASIN_BARANGAY_POPULATION.items():
        if name.lower() == clean:
            return pop

    import re
    s = re.sub(r"\(.*?\)", "", clean)
    s = re.sub(r"\b(barangay|brgy\.?|bgy\.?|poblacion|pob\.?)\b", "", s)
    s = s.replace(" 1", " i").replace(" 2", " ii").replace("ñ", "n")
    s = re.sub(r"[^a-z0-9\- ]", " ", s)
    s = " ".join(s.split())

    # Check overrides with fuzzy match
    for name, pop in _population_overrides.items():
        n_clean = name.lower().replace("ñ", "n")
        if n_clean == s or n_clean in s or s in n_clean:
            return pop

    # Check census defaults with fuzzy match
    for name, pop in MAASIN_BARANGAY_POPULATION.items():
        n_clean = name.lower().replace("ñ", "n")
        if n_clean == s or n_clean in s or s in n_clean:
            return pop

    return DEFAULT_BARANGAY_POPULATION

