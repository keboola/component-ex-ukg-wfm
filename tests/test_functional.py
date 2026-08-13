"""Functional tests for component using VCR cassettes."""

from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, VCRTestDataDir, get_test_cases

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")

# _stream_and_write_table logs one diagnostic line per non-empty resource with real wall-clock
# timings (fetch/process+write/phase2 seconds). Those numbers are never reproducible between the
# moment a cassette was recorded and any later replay, so they must be normalized away before the
# VCR log comparison — same rationale as the framework's own UUID/epoch-timestamp normalizers.
_TIMING_LOG_NORMALIZER = (
    r"\(fetch \d+\.\d+s, process\+write \d+\.\d+s, phase2 \d+\.\d+s\)",
    "(fetch <T>s, process+write <T>s, phase2 <T>s)",
)


class _TimingNormalizedTestDataDir(VCRTestDataDir):
    """VCRTestDataDir with the timing log line normalized before comparison."""

    def _setup_vcr(self):
        super()._setup_vcr()
        if self.vcr_recorder is not None:
            self.vcr_recorder.log_normalizers = [
                *(self.vcr_recorder.log_normalizers or []),
                _TIMING_LOG_NORMALIZER,
            ]


@pytest.mark.parametrize("test_name", get_test_cases(FUNCTIONAL_DIR))
def test_functional(test_name):
    """Run a single VCR functional test case."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
        test_data_dir_class=_TimingNormalizedTestDataDir,
    )
    tester.run()
