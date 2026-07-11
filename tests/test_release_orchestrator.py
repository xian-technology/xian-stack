from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release_orchestrator as orchestrator


def repo_state(sha: str) -> orchestrator.RepoState:
    return orchestrator.RepoState(
        branch="main",
        clean=True,
        head_sha=sha,
        origin_sha=sha,
        ahead=0,
        behind=0,
    )


def check_run(
    *,
    name: str = "validate",
    status: str = "completed",
    conclusion: str | None = "success",
    url: str = "https://github.com/xian-technology/example/actions/runs/1",
    database_id: int = 1,
) -> orchestrator.GithubCheckRun:
    return orchestrator.GithubCheckRun(
        name=name,
        status=status,
        conclusion=conclusion,
        url=url,
        started_at="2026-07-11T08:00:00Z",
        completed_at="2026-07-11T08:01:00Z",
        database_id=database_id,
    )


def release_plan() -> orchestrator.ReleasePlan:
    return orchestrator.ReleasePlan(
        unit=orchestrator.UNITS["xian-js"],
        latest_tag="v0.2.0",
        latest_version="0.2.0",
        source_version="0.2.1",
        target_version="0.2.1",
        version_mode="prebumped",
        changed_files=["packages/client/src/index.ts"],
        reason="test plan",
    )


class ReleaseOrchestratorGithubCheckTests(unittest.TestCase):
    def test_ensure_github_checks_green_accepts_completed_green_checks(self) -> None:
        states = {
            "xian-configs": repo_state("a" * 40),
            "xian-js": repo_state("b" * 40),
        }
        checks = {
            "xian-configs": [check_run(name="validate")],
            "xian-js": [
                check_run(name="validate"),
                check_run(name="release", conclusion="skipped"),
            ],
        }

        with patch.object(
            orchestrator,
            "github_check_runs",
            side_effect=lambda repo_name, _sha: checks[repo_name],
        ):
            orchestrator.ensure_github_checks_green(states, [release_plan()])

    def test_ensure_github_checks_green_rejects_failed_checks(self) -> None:
        states = {
            "xian-configs": repo_state("a" * 40),
            "xian-js": repo_state("b" * 40),
        }
        checks = {
            "xian-configs": [check_run(name="validate")],
            "xian-js": [check_run(name="validate", conclusion="failure")],
        }

        with patch.object(
            orchestrator,
            "github_check_runs",
            side_effect=lambda repo_name, _sha: checks[repo_name],
        ):
            with self.assertRaisesRegex(orchestrator.ReleaseError, "xian-js.*validate"):
                orchestrator.ensure_github_checks_green(states, [release_plan()])

    def test_ensure_github_checks_green_rejects_missing_checks(self) -> None:
        states = {
            "xian-configs": repo_state("a" * 40),
            "xian-js": repo_state("b" * 40),
        }
        checks = {
            "xian-configs": [check_run(name="validate")],
            "xian-js": [],
        }

        with patch.object(
            orchestrator,
            "github_check_runs",
            side_effect=lambda repo_name, _sha: checks[repo_name],
        ):
            with self.assertRaisesRegex(orchestrator.ReleaseError, "no GitHub check runs"):
                orchestrator.ensure_github_checks_green(states, [release_plan()])

    def test_latest_check_runs_by_name_uses_latest_attempt(self) -> None:
        stale = check_run(conclusion="failure", database_id=1)
        latest = check_run(conclusion="success", database_id=2)

        self.assertEqual(orchestrator.latest_check_runs_by_name([stale, latest]), [latest])


if __name__ == "__main__":
    unittest.main()
