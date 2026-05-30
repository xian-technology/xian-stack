import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_pyproject() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        return tomllib.load(pyproject_file)


def test_project_uses_shared_python_and_tooling_policy() -> None:
    pyproject = load_pyproject()

    assert pyproject["project"]["requires-python"] == ">=3.14"
    assert "ruff>=0.15.12,<0.16" in pyproject["dependency-groups"]["dev"]
    assert pyproject["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert pyproject["tool"]["ruff"]["line-length"] == 100
    assert pyproject["tool"]["ruff"]["lint"]["select"] == ["E", "F", "I"]
