"""Environment-driven configuration for PlanningV2."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _load_env() -> None:
    if Path("planning.env").exists():
        load_dotenv("planning.env", override=True)
    else:
        load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid integer value for {name}: {raw}")
        sys.exit(1)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default

    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False

    print(f"Invalid boolean value for {name}: {raw}")
    sys.exit(1)


@dataclass(frozen=True)
class PlanningConfig:
    raw_dir: Path
    stage_dir: Path
    output_dir: Path
    reference_dir: Path
    samples_dir: Path
    cc8_source_root: Path
    exco_source_dir: Path
    exco_output_dir: Path
    minstock3_dir: Path
    sap_url: str
    sap_company: str
    sap_user: str
    sap_password: str
    sap_verify_ssl: bool
    sql_query_code: str
    odata_max_page_size: int
    group_min: int
    group_max: int

    def ensure_directories(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


def get_config() -> PlanningConfig:
    _load_env()

    return PlanningConfig(
        raw_dir=Path(os.getenv("RAW_DIR", "data/raw")),
        stage_dir=Path(os.getenv("STAGE_DIR", "data/stage")),
        output_dir=Path(os.getenv("OUTPUT_DIR", "data/output")),
        reference_dir=Path(os.getenv("REFERENCE_DIR", "Reference")),
        samples_dir=Path(os.getenv("SAMPLES_DIR", "Samples")),
        cc8_source_root=Path(os.getenv("CC8_SOURCE_ROOT", "C:/dev/cc8")),
        exco_source_dir=Path(os.getenv("EXCO_SOURCE_DIR", "C:/dev/cc8/Exco/source_data")),
        exco_output_dir=Path(os.getenv("EXCO_OUTPUT_DIR", "C:/dev/cc8/Exco/output/csv")),
        minstock3_dir=Path(os.getenv("MINSTOCK3_DIR", "C:/dev/cc8/MinStock3")),
        sap_url=(os.getenv("SAP_URL") or "").rstrip("/"),
        sap_company=os.getenv("SAP_COMPANY") or "",
        sap_user=os.getenv("SAP_USER") or "",
        sap_password=os.getenv("SAP_PASSWORD") or "",
        sap_verify_ssl=_env_bool("SAP_VERIFY_SSL", False),
        sql_query_code=os.getenv("SQL_QUERY_CODE", "CC8_PLANNING_V2_STOCK_RAW"),
        odata_max_page_size=_env_int("ODATA_MAX_PAGE_SIZE", 500),
        group_min=_env_int("GROUP_MIN", 105),
        group_max=_env_int("GROUP_MAX", 106),
    )
