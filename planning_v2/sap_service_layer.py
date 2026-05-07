"""SAP Business One Service Layer helpers.

This mirrors the stable pattern from MinStock-3.0: authenticate once, create
or patch a saved SQLQueries object, execute its List endpoint, and follow
OData next links until all pages have been read.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any

import requests
import urllib3

from planning_v2.config import PlanningConfig


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class SapServiceLayer(AbstractContextManager["SapServiceLayer"]):
    def __init__(self, cfg: PlanningConfig) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def __enter__(self) -> "SapServiceLayer":
        self.login()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.logout()

    def login(self) -> None:
        missing = [
            name
            for name, value in {
                "SAP_URL": self.cfg.sap_url,
                "SAP_COMPANY": self.cfg.sap_company,
                "SAP_USER": self.cfg.sap_user,
                "SAP_PASSWORD": self.cfg.sap_password,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required SAP settings: {', '.join(missing)}")

        response = self.session.post(
            f"{self.cfg.sap_url}/Login",
            json={
                "CompanyDB": self.cfg.sap_company,
                "UserName": self.cfg.sap_user,
                "Password": self.cfg.sap_password,
            },
            verify=self.cfg.sap_verify_ssl,
        )
        response.raise_for_status()

    def logout(self) -> None:
        try:
            self.session.post(f"{self.cfg.sap_url}/Logout", verify=self.cfg.sap_verify_ssl)
        finally:
            self.session.close()

    def ensure_sql_query(self, sql_code: str, sql_text: str) -> None:
        query_url = f"{self.cfg.sap_url}/SQLQueries('{sql_code}')"
        payload = {"SqlCode": sql_code, "SqlName": sql_code, "SqlText": sql_text}

        response = self.session.get(query_url, verify=self.cfg.sap_verify_ssl)
        if response.status_code == 200:
            patch_response = self.session.patch(
                query_url,
                json={"SqlText": sql_text},
                verify=self.cfg.sap_verify_ssl,
            )
            patch_response.raise_for_status()
            return

        if response.status_code != 404:
            response.raise_for_status()

        create_response = self.session.post(
            f"{self.cfg.sap_url}/SQLQueries",
            json=payload,
            verify=self.cfg.sap_verify_ssl,
        )
        create_response.raise_for_status()

    def run_sql_query(self, sql_code: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        url: str | None = f"{self.cfg.sap_url}/SQLQueries('{sql_code}')/List"

        while url:
            response = self.session.get(
                url,
                headers={"Prefer": f"odata.maxpagesize={self.cfg.odata_max_page_size}"},
                verify=self.cfg.sap_verify_ssl,
            )
            response.raise_for_status()

            data = response.json()
            rows.extend(data.get("value", []))
            url = self._next_url(data)

        return rows

    def fetch_lookup(self, entity: str, select: Iterable[str]) -> list[dict[str, Any]]:
        select_clause = ",".join(select)
        url: str | None = f"{self.cfg.sap_url}/{entity}?$select={select_clause}"
        rows: list[dict[str, Any]] = []

        while url:
            response = self.session.get(url, verify=self.cfg.sap_verify_ssl)
            response.raise_for_status()

            data = response.json()
            rows.extend(data.get("value", []))
            url = self._next_url(data)

        return rows

    def _next_url(self, data: dict[str, Any]) -> str | None:
        next_link = data.get("odata.nextLink") or data.get("@odata.nextLink")
        if not next_link:
            return None
        if str(next_link).startswith("http"):
            return str(next_link)
        return f"{self.cfg.sap_url}/{next_link}"
