import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
from localnet_benchmark_checks import compare_baseline, summarize_samples


def samples(values=(100, 105, 95)):
    return [
        dict(
            ok=True,
            scenario="transfer_fanout",
            preparation_mode="manual",
            operations=1000,
            workload_tps=value,
            successful_transactions=1000,
            workload_transactions=1000,
        )
        for value in values
    ]


def report(values=(100, 105, 95), config=None):
    return dict(
        schema_version=1,
        ok=True,
        configuration=config or {"nodes": 5},
        measurements=summarize_samples(samples(values)),
    )


def test_uses_median_and_keeps_modes_separate():
    runs = samples((100, 1000, 95)) + [dict(r, preparation_mode="automatic") for r in samples()]
    measurements = summarize_samples(runs)
    assert measurements["transfer_fanout/manual/1000"]["median_tps"] == 100
    assert measurements["transfer_fanout/automatic/1000"]["sample_count"] == 3


def test_regression_gate_accepts_boundary_and_rejects_drop():
    reference = report()
    assert compare_baseline(report((80, 80, 80)), reference, 20)["ok"]
    result = compare_baseline(report((79, 79, 79)), reference, 20)
    assert not result["ok"]
    assert result["comparisons"][0]["change_percent"] == -21


@pytest.mark.parametrize("change", ["configuration", "missing", "few", "failed", "schema"])
def test_invalid_comparisons_fail_closed(change):
    candidate, baseline = report(), report()
    if change == "configuration":
        candidate["configuration"]["nodes"] = 4
    elif change == "missing":
        candidate["measurements"] = {}
    elif change == "few":
        baseline["measurements"]["transfer_fanout/manual/1000"]["sample_count"] = 1
    elif change == "failed":
        candidate["ok"] = False
    else:
        baseline["schema_version"] = 0
    with pytest.raises(ValueError):
        compare_baseline(candidate, baseline, 20)


@pytest.mark.parametrize("value", [0, float("nan"), float("inf"), -1])
def test_invalid_measurements_are_rejected(value):
    with pytest.raises(ValueError):
        summarize_samples(samples((value,)))


def test_failed_or_partial_work_is_not_counted_as_throughput():
    run = samples()[0]
    run["successful_transactions"] -= 1
    with pytest.raises(ValueError):
        summarize_samples([run])


def test_missing_or_extra_transactions_fail():
    for count in (999, 1001):
        run = samples()[0]
        run["workload_transactions"] = run["successful_transactions"] = count
        with pytest.raises(ValueError):
            summarize_samples([run])


def test_failed_sample_fails_even_with_successful_count():
    run = samples()[0]
    run["ok"] = False
    with pytest.raises(ValueError):
        summarize_samples([run])


@pytest.mark.parametrize("tolerance", [-1, 100, float("nan"), float("inf")])
def test_invalid_tolerance_is_rejected(tolerance):
    with pytest.raises(ValueError):
        compare_baseline(report(), report(), tolerance)


def test_faster_candidate_passes_and_reports_both_values():
    result = compare_baseline(report((120, 120, 120)), report(), 0)
    assert result == {
        "ok": True,
        "comparisons": [
            {
                "measurement": "transfer_fanout/manual/1000",
                "baseline_tps": 100,
                "candidate_tps": 120,
                "change_percent": 20,
                "ok": True,
            }
        ],
    }


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan")])
def test_invalid_reference_median_fails(bad):
    baseline = report()
    baseline["measurements"]["transfer_fanout/manual/1000"]["median_tps"] = bad
    with pytest.raises(ValueError):
        compare_baseline(report(), baseline, 25)
