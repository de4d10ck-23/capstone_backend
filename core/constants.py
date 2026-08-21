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
