import os
import unittest
from pathlib import Path
from unittest import mock

import requests_mock
from freezegun import freeze_time

_VALID_DATADIR = str(
    Path(__file__).parent / "mock" / "business_structure_full" / "source" / "data"
)
_HOST = "https://acme.prd.mykronos.com"
_AUTH_URL = f"{_HOST}/api/authentication/access_token"


class TestComponent(unittest.TestCase):
    @freeze_time("2026-01-01")
    @mock.patch.dict(os.environ, {"KBC_DATADIR": "./non-existing-dir"})
    def test_run_no_cfg_fails(self):
        from component import Component
        with self.assertRaises(ValueError):
            Component().run()


class TestConnection(unittest.TestCase):
    @mock.patch.dict(os.environ, {"KBC_DATADIR": _VALID_DATADIR})
    def test_connection_success(self):
        from component import Component
        with requests_mock.Mocker() as m:
            m.post(_AUTH_URL, json={"access_token": "T", "refresh_token": "R", "expires_in": 3600})
            result = Component().test_connection()
        self.assertEqual(result, {"status": "success"})

    @mock.patch.dict(os.environ, {"KBC_DATADIR": _VALID_DATADIR})
    def test_connection_failure_exits_1(self):
        # A bad-credentials token response surfaces as a UserException, which the
        # @sync_action wrapper converts into exit(1) (user error, not exit 2).
        from component import Component
        with requests_mock.Mocker() as m:
            m.post(_AUTH_URL, status_code=401, json={"error": "bad creds"})
            with self.assertRaises(SystemExit) as ctx:
                Component().test_connection()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
