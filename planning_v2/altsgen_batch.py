"""Hosted Altsgen batch API helpers for Planning V2 part-type enrichment."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import dotenv_values

from planning_v2.config import get_config
from planning_v2.onboarding_csvs import build_template_part_types
from planning_v2.template_specs import template_columns


BATCH_API_MAX_PARTS = 500
ALTSGEN_PART_TYPE_EVIDENCE = "Altsgen_PartType_Evidence.csv"
ALTSGEN_BATCH_AUDIT = "Altsgen_Batch_Audit.jsonl"
ALTSGEN_RAW_RESULTS = "Altsgen_Raw_Results.jsonl"


@dataclass(frozen=True)
class AltsgenSettings:
    base_url: str
    api_key: str
    timeout_sec: float = 45.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat", "null", "<na>"} else text


def _humanize_slug(value: object) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    return " ".join(part for part in text.replace("-", "_").split("_") if part).title()


def _load_altsgen_settings(env_file: Path | None = None) -> AltsgenSettings:
    values: dict[str, str] = {}
    default_obsoletes_env = Path("C:/dev/Obsoletes/pipeline.env")
    for path in [default_obsoletes_env, env_file]:
        if path and path.exists():
            values.update({key: str(value) for key, value in dotenv_values(path).items() if value is not None})
    import os

    values.update(os.environ)
    base_url = _clean_text(values.get("ALTSGEN_BASE_URL")).rstrip("/")
    api_key = _clean_text(values.get("ALTSGEN_API_KEY"))
    timeout = float(_clean_text(values.get("ALTSGEN_TIMEOUT_SEC")) or "45")
    if not base_url:
        raise ValueError("ALTSGEN_BASE_URL is required. Set it in env or pass --env-file.")
    if not api_key:
        raise ValueError("ALTSGEN_API_KEY is required. Set it in env or pass --env-file.")
    return AltsgenSettings(base_url=base_url, api_key=api_key, timeout_sec=timeout)


def _altsgen_request(
    settings: AltsgenSettings,
    *,
    method: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    timeout_sec: float | None = None,
    retry_attempts: int = 3,
    retry_backoff_sec: float = 8.0,
) -> requests.Response:
    url = f"{settings.base_url}{endpoint}"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.api_key}"}
    last_error: Exception | None = None
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(1, max(retry_attempts, 1) + 1):
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                json=payload,
                timeout=timeout_sec if timeout_sec is not None else settings.timeout_sec,
                headers=headers,
            )
            if response.status_code in retryable_statuses and attempt < retry_attempts:
                time.sleep(retry_backoff_sec * attempt)
                continue
            response.raise_for_status()
            return response
        except (requests.ConnectTimeout, requests.ReadTimeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt >= retry_attempts:
                break
            time.sleep(retry_backoff_sec * attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Altsgen request failed for {endpoint}")


def _context_for_part(row: dict[str, Any]) -> str:
    parts = [
        _clean_text(row.get("description")),
        _clean_text(row.get("productClass")),
        _clean_text(row.get("productType")),
        "CoCre8 Fujitsu service spare",
    ]
    return " | ".join(part for part in parts if part)[:2000]


def load_co_cre8_parts(parts_csv: Path, *, limit: int) -> pd.DataFrame:
    if not parts_csv.exists():
        raise FileNotFoundError(str(parts_csv))
    parts = pd.read_csv(parts_csv, dtype=str, encoding="utf-8-sig").fillna("")
    if "PartNumber" not in parts.columns:
        raise ValueError(f"{parts_csv} must contain PartNumber")
    parts = parts[parts["PartNumber"].astype(str).str.strip().ne("")].copy()
    parts = parts.drop_duplicates(subset=["PartNumber"], keep="first").reset_index(drop=True)
    if limit > 0:
        parts = parts.head(limit)
    return parts


def _post_batch(
    settings: AltsgenSettings,
    rows: list[dict[str, Any]],
    *,
    force_refresh: bool,
    submit_timeout_sec: float,
) -> dict[str, Any]:
    payload = {
        "parts": [
            {
                "seed_pn": _clean_text(row.get("PartNumber")),
                "context": _context_for_part(row),
            }
            for row in rows
        ],
        "force_refresh": bool(force_refresh),
        "queue_full_run": False,
        "force_async": True,
    }
    response = _altsgen_request(
        settings,
        method="POST",
        endpoint="/api/v1/identify/batch",
        payload=payload,
        timeout_sec=max(settings.timeout_sec, submit_timeout_sec),
        retry_attempts=3,
    )
    return response.json()


def _poll_batch(settings: AltsgenSettings, batch_id: str) -> dict[str, Any]:
    response = _altsgen_request(
        settings,
        method="GET",
        endpoint=f"/api/v1/identify/batch/{batch_id}",
        timeout_sec=max(settings.timeout_sec, 30.0),
        retry_attempts=1,
    )
    return response.json()


def _confidence_from_attributes(attributes: Any) -> str:
    if not isinstance(attributes, dict):
        return ""
    confidences: list[int] = []
    for attribute in attributes.values():
        if not isinstance(attribute, dict):
            continue
        try:
            confidences.append(int(float(attribute.get("confidence"))))
        except (TypeError, ValueError):
            continue
    return str(max(confidences)) if confidences else ""


def _description_from_result(result: dict[str, Any], commodity_type: str) -> str:
    spec = result.get("spec_summary")
    if isinstance(spec, dict):
        description = _clean_text(spec.get("description"))
        if description:
            return description
    return _humanize_slug(commodity_type)


def _evidence_row(
    *,
    batch_id: str,
    source_row: dict[str, Any] | None,
    item: dict[str, Any],
) -> dict[str, Any]:
    status = _clean_text(item.get("status")).lower()
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    commodity_type = _clean_text(result.get("commodity_type"))
    attributes = result.get("attributes") or {}
    evidence = result.get("evidence") or []
    if not isinstance(attributes, dict):
        attributes = {}
    if not isinstance(evidence, list):
        evidence = []
    return {
        "PartNumber": _clean_text(item.get("seed_pn")),
        "description": _clean_text((source_row or {}).get("description")),
        "batchId": batch_id,
        "status": status,
        "partType": commodity_type,
        "partTypeDescription": _description_from_result(result, commodity_type),
        "isReworkable": "",
        "altsgenMode": "live_api" if status == "ok" else "",
        "altsgenConfidence": _confidence_from_attributes(attributes),
        "altsgenAttributeCount": len(attributes),
        "altsgenEvidenceCount": len(evidence),
        "error": _clean_text(item.get("error") or result.get("error")),
        "identifiedUtc": _utc_now(),
    }


def _append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_evidence(path: Path, rows: list[dict[str, Any]]) -> pd.DataFrame:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_frame = pd.DataFrame(rows)
    if path.exists():
        old_frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        frame = pd.concat([old_frame, new_frame], ignore_index=True, sort=False)
    else:
        frame = new_frame
    if frame.empty:
        frame = pd.DataFrame(columns=list(_evidence_row(batch_id="", source_row={}, item={}).keys()))
    frame = frame.drop_duplicates(subset=["PartNumber"], keep="last").reset_index(drop=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return frame


def run_altsgen_part_type_batch(
    *,
    parts_csv: Path,
    out_dir: Path,
    env_file: Path | None = None,
    limit: int = BATCH_API_MAX_PARTS,
    batch_size: int = BATCH_API_MAX_PARTS,
    poll_interval_sec: float = 20.0,
    poll_timeout_sec: float = 3600.0,
    submit_timeout_sec: float = 180.0,
    force_refresh: bool = False,
    resume_batch_id: str | None = None,
) -> dict[str, Any]:
    settings = _load_altsgen_settings(env_file)
    parts = load_co_cre8_parts(parts_csv, limit=limit)
    batch_size = min(max(batch_size, 1), BATCH_API_MAX_PARTS)
    rows = parts.to_dict(orient="records")
    chunks = [rows[index : index + batch_size] for index in range(0, len(rows), batch_size)]
    if not chunks and not resume_batch_id:
        raise ValueError("No parts selected for Altsgen batch.")

    review_dir = out_dir.parent / "review_evidence"
    audit_path = review_dir / ALTSGEN_BATCH_AUDIT
    raw_path = review_dir / ALTSGEN_RAW_RESULTS
    evidence_path = review_dir / ALTSGEN_PART_TYPE_EVIDENCE
    row_lookup = {_clean_text(row.get("PartNumber")): row for row in rows}

    submitted_batches: dict[str, dict[str, Any]] = {}
    audit_records: list[dict[str, Any]] = []
    if resume_batch_id:
        submitted_batches[_clean_text(resume_batch_id)] = {"row_lookup": row_lookup, "last_payload": {}}
    else:
        first_chunk = chunks[0]
        submitted = _post_batch(settings, first_chunk, force_refresh=force_refresh, submit_timeout_sec=submit_timeout_sec)
        batch_id = _clean_text(submitted.get("batch_id"))
        if not batch_id:
            raise RuntimeError(f"Altsgen batch response did not include batch_id: {submitted}")
        submitted_batches[batch_id] = {
            "row_lookup": {_clean_text(row.get("PartNumber")): row for row in first_chunk},
            "last_payload": submitted,
        }
        submit_record = {
            "observedUtc": _utc_now(),
            "batchId": batch_id,
            "status": _clean_text(submitted.get("status")).lower() or "submitted",
            "total": submitted.get("total"),
            "done": submitted.get("done"),
            "elapsedSeconds": submitted.get("elapsed_seconds"),
        }
        audit_records.append(submit_record)
        _append_jsonl(audit_path, [submit_record])
        print(f"Submitted Altsgen batch {batch_id} with {len(first_chunk)} parts.", flush=True)

    completed: set[str] = set()
    deadline = time.monotonic() + max(poll_timeout_sec, 0.0)
    evidence_rows: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []

    while len(completed) < len(submitted_batches) and time.monotonic() < deadline:
        for batch_id, meta in submitted_batches.items():
            if batch_id in completed:
                continue
            payload = meta["last_payload"] or _poll_batch(settings, batch_id)
            meta["last_payload"] = {}
            batch_status = _clean_text(payload.get("status")).lower()
            audit_record = {
                "observedUtc": _utc_now(),
                "batchId": batch_id,
                "status": batch_status,
                "total": payload.get("total"),
                "done": payload.get("done"),
                "elapsedSeconds": payload.get("elapsed_seconds"),
            }
            audit_records.append(audit_record)
            _append_jsonl(audit_path, [audit_record])
            for item in payload.get("results") or []:
                if not isinstance(item, dict):
                    continue
                raw_records.append({"batchId": batch_id, "observedUtc": _utc_now(), "item": item})
                evidence_rows.append(
                    _evidence_row(
                        batch_id=batch_id,
                        source_row=meta["row_lookup"].get(_clean_text(item.get("seed_pn"))),
                        item=item,
                    )
                )
            if batch_status in {"completed", "failed"}:
                completed.add(batch_id)
        if len(completed) < len(submitted_batches) and time.monotonic() < deadline:
            time.sleep(max(poll_interval_sec, 1.0))
            for batch_id in submitted_batches:
                if batch_id not in completed:
                    submitted_batches[batch_id]["last_payload"] = _poll_batch(settings, batch_id)

    _append_jsonl(raw_path, raw_records)
    evidence = _write_evidence(evidence_path, evidence_rows)

    cfg = get_config()
    part_type_columns = template_columns(cfg.samples_dir)["PartTypes"]
    part_types = build_template_part_types(evidence, part_type_columns)
    part_types_path = out_dir / "PartTypes.csv"
    part_types_path.parent.mkdir(parents=True, exist_ok=True)
    part_types.to_csv(part_types_path, index=False, encoding="utf-8-sig")

    statuses = evidence["status"].value_counts(dropna=False).astype(int).to_dict() if "status" in evidence.columns else {}
    report = {
        "generatedUtc": _utc_now(),
        "selectedParts": int(len(parts)),
        "submittedBatchIds": list(submitted_batches.keys()),
        "completedBatchIds": sorted(completed),
        "incompleteBatchIds": sorted(set(submitted_batches) - completed),
        "statusCountsAllEvidence": {str(key): int(value) for key, value in statuses.items()},
        "partTypeRows": int(len(part_types)),
        "partTypesCsv": str(part_types_path),
        "evidenceCsv": str(evidence_path),
        "batchAuditJsonl": str(audit_path),
        "rawResultsJsonl": str(raw_path),
    }
    report_path = review_dir / "Altsgen_PartType_Batch_Report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CoCre8 parts through hosted Altsgen batch API for PartTypes.")
    parser.add_argument("--parts-csv", type=Path, default=Path("data/output/onboarding_csvs/Parts.csv"))
    parser.add_argument("--out", type=Path, default=Path("data/output/onboarding_csvs"))
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=BATCH_API_MAX_PARTS)
    parser.add_argument("--batch-size", type=int, default=BATCH_API_MAX_PARTS)
    parser.add_argument("--poll-interval-sec", type=float, default=20.0)
    parser.add_argument("--poll-timeout-sec", type=float, default=3600.0)
    parser.add_argument("--submit-timeout-sec", type=float, default=180.0)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--resume-batch-id", default=None)
    args = parser.parse_args(argv)
    report = run_altsgen_part_type_batch(
        parts_csv=args.parts_csv,
        out_dir=args.out,
        env_file=args.env_file,
        limit=args.limit,
        batch_size=args.batch_size,
        poll_interval_sec=args.poll_interval_sec,
        poll_timeout_sec=args.poll_timeout_sec,
        submit_timeout_sec=args.submit_timeout_sec,
        force_refresh=args.force_refresh,
        resume_batch_id=args.resume_batch_id,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
