"""
Mock-based (datadir) tests for the UKG Pro WFM extractor.

Each fixture under tests/mock/ has:
  source/data/config.json        -- component config
  source/data/in/state.json      -- (optional) input state
  expected/data/out/tables/      -- expected output tables
  mock/*.json                    -- HTTP mock definitions (see below)
  expected-code                  -- (optional) expected exit code (default 0)

Mock file format (one endpoint per file). URL matching only — request bodies vary by
employee chunk / date window, so mocks match on method + URL:
  {
    "url":         "<full url>",
    "method":      "GET" | "POST",
    "status_code": 200,
    "json":        <response body>,
    "headers":     {"Header-Name": "value"}  (optional)
  }

For ordered responses on the SAME url (multi_read cacheKey paging, async poll sequences),
use the "responses" key — each element is served in turn:
  {
    "url":       "<url>",
    "method":    "POST",
    "responses": [
      {"status_code": 200, "json": {"records": [...], "cacheKey": "K", "count": 2}},
      {"status_code": 200, "json": {"records": [...]}}
    ]
  }
"""

import json
import os
from os import path
from pathlib import Path

import requests_mock as requests_mock_lib
from keboola.datadirtest import DataDirTester, TestDataDir


class MockedTestDataDir(TestDataDir):
    """TestDataDir subclass that mounts HTTP mocks from mock/*.json before running."""

    def run_component(self):
        mock_dir = path.join(self.orig_dir, "mock")
        expected_code = self._load_expected_code()

        with requests_mock_lib.Mocker() as mocker:
            if path.isdir(mock_dir):
                self._register_mocks(mocker, mock_dir)

            if expected_code is not None:
                try:
                    super().run_component()
                    actual_code = 0
                except SystemExit as exc:
                    actual_code = int(exc.code) if exc.code is not None else 0
                self.assertEqual(
                    actual_code,
                    expected_code,
                    f"Expected exit code {expected_code}, got {actual_code}",
                )
            else:
                super().run_component()

    def compare_source_and_expected(self):
        """Skip output file comparison for failure cases (expected-code set)."""
        if self._load_expected_code() is not None:
            self.run_component()
        else:
            super().compare_source_and_expected()

    def _load_expected_code(self) -> int | None:
        code_file = path.join(self.orig_dir, "expected-code")
        if path.isfile(code_file):
            return int(Path(code_file).read_text().strip())
        return None

    def _register_mocks(self, mocker: requests_mock_lib.Mocker, mock_dir: str) -> None:
        for fname in sorted(os.listdir(mock_dir)):
            if not fname.endswith(".json"):
                continue
            fpath = path.join(mock_dir, fname)
            with open(fpath) as fh:
                spec = json.load(fh)

            method = spec.get("method", "GET").upper()
            url = spec["url"]

            if "responses" in spec:
                mocker.register_uri(method, url, [self._response_kwargs(r) for r in spec["responses"]])
            else:
                mocker.register_uri(method, url, **self._response_kwargs(spec))

    @staticmethod
    def _response_kwargs(spec: dict) -> dict:
        """Build requests_mock response kwargs. A "text" body (e.g. CSV) takes precedence over json."""
        kwargs: dict = {"status_code": spec.get("status_code", 200), "headers": spec.get("headers", {})}
        if "text" in spec:
            kwargs["text"] = spec["text"]
        else:
            kwargs["json"] = spec.get("json")
        return kwargs


def test_mock():
    DataDirTester(
        data_dir=str(Path(__file__).parent / "mock"),
        test_data_dir_class=MockedTestDataDir,
    ).run()
