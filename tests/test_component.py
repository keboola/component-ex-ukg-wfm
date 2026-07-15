import os
import unittest
from unittest import mock

from freezegun import freeze_time


class TestComponent(unittest.TestCase):
    @freeze_time("2026-01-01")
    @mock.patch.dict(os.environ, {"KBC_DATADIR": "./non-existing-dir"})
    def test_run_no_cfg_fails(self):
        from component import Component
        with self.assertRaises(ValueError):
            Component().run()


if __name__ == "__main__":
    unittest.main()
