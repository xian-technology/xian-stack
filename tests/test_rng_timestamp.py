"""The network-side RNG oracle needs no node or third-party runtime packages."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "localnet_rng_checks.py"
sys.path.insert(0, str(SCRIPT.parent))


def test_oracle_runs_with_only_standard_library():
    probe = """
import importlib.util
import sys
class StandardLibraryOnly:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] not in sys.stdlib_module_names:
            raise ImportError('third-party import forbidden: ' + fullname)
sys.meta_path.insert(0, StandardLibraryOnly())
spec = importlib.util.spec_from_file_location('rng_oracle', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
block = {'block_id': {'hash': 'AB' * 32}, 'block': {'header': {
    'height': '17', 'chain_id': 'local-rng-audit',
    'time': '2026-11-03T12:34:56.123456789Z'}}}
print(module.transaction_random_value(block, 'signature-one'))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", probe, str(SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    # Known stream from the original protobuf-based oracle and native execution.
    assert json.loads(result.stdout) == 10042468269563374662


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("1970-01-01T00:00:00Z", 0),
        ("1970-01-01T00:00:00.000000001Z", 1),
        ("1969-12-31T23:59:59.999999999Z", -1),
        ("1969-12-31T23:59:59.1Z", -900000000),
        ("1970-01-01T01:30:00.000000001+01:30", 1),
        ("1969-12-31T22:30:00.000000001-01:30", 1),
        ("1970-01-01T00:00:00-00:00", 0),
        ("2026-11-03T12:34:56.123456789Z", 1793709296123456789),
        ("2000-02-29T00:00:00Z", 951782400000000000),
    ]
    + [
        (f"1970-01-01T00:00:00.{('123456789')[:n]}Z", int(("123456789")[:n].ljust(9, "0")))
        for n in range(1, 10)
    ],
)
def test_timestamp_nanoseconds(timestamp, expected):
    from localnet_rng_checks import timestamp_nanoseconds

    assert timestamp_nanoseconds(timestamp) == expected


@pytest.mark.parametrize(
    "timestamp",
    [
        "1970-01-01T00:00:00.1234567890Z",
        "1970-01-01T00:00:00.Z",
        "1970-01-01T00:00:00",
        "1970-01-01 00:00:00Z",
        "1970-01-01T00:00:00Z\n",
        "1970-01-01T00:00:00+24:00",
        "1970-01-01T00:00:00+00:60",
        "1970-01-01T00:00:00+0100",
        "1970-01-01T00:00:00+01:00:01",
        "1970-01-01T24:00:00Z",
        "1970-01-01T00:00:60Z",
        "2026-02-29T00:00:00Z",
        "0000-01-01T00:00:00Z",
        "1970-01-01T00:00:00.١Z",
        "",
        None,
    ],
)
def test_timestamp_rejects_invalid_input(timestamp):
    from localnet_rng_checks import timestamp_nanoseconds

    with pytest.raises((TypeError, ValueError)):
        timestamp_nanoseconds(timestamp)
