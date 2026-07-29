"""Repository structure invariants.

These are cheap guards against the ways a monorepo quietly rots: a package added
to disk but not to the workspace, an import package whose directory name drifts
from its distribution name, a layering contract that stops covering a package,
or a real credential pasted into the example environment file.

None of this needs the application to exist, so it runs from day one.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# distribution name -> (directory, import package name)
EXPECTED_MEMBERS: dict[str, tuple[str, str]] = {
    "rn-core": ("packages/core", "rn_core"),
    "rn-domain": ("packages/domain", "rn_domain"),
    "rn-persistence": ("packages/persistence", "rn_persistence"),
    "rn-providers": ("packages/providers", "rn_providers"),
    "rn-services": ("packages/services", "rn_services"),
    "rn-agent": ("packages/agent", "rn_agent"),
    "rn-orchestration": ("packages/orchestration", "rn_orchestration"),
    "rn-api": ("apps/api", "rn_api"),
    "rn-voice": ("apps/voice-gateway", "rn_voice"),
    "rn-worker": ("apps/worker", "rn_worker"),
}


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


@pytest.fixture(scope="module")
def root_config() -> dict[str, Any]:
    return _load_toml(REPO_ROOT / "pyproject.toml")


@pytest.mark.unit
@pytest.mark.parametrize(("dist_name", "spec"), sorted(EXPECTED_MEMBERS.items()))
def test_workspace_member_is_well_formed(dist_name: str, spec: tuple[str, str]) -> None:
    """Each member exists, declares the expected name, and ships a typed package."""
    directory, import_name = spec
    member_dir = REPO_ROOT / directory

    manifest = member_dir / "pyproject.toml"
    assert manifest.is_file(), f"{directory} is missing a pyproject.toml"

    config = _load_toml(manifest)
    assert config["project"]["name"] == dist_name, (
        f"{directory} declares name={config['project']['name']!r}, expected {dist_name!r}"
    )

    package_dir = member_dir / "src" / import_name
    assert (package_dir / "__init__.py").is_file(), (
        f"{directory} is missing src/{import_name}/__init__.py"
    )
    # Without py.typed, downstream packages silently lose type checking.
    assert (package_dir / "py.typed").is_file(), (
        f"{directory} is missing src/{import_name}/py.typed"
    )


@pytest.mark.unit
def test_every_member_is_registered_as_a_workspace_source(root_config: dict[str, Any]) -> None:
    """A member missing from [tool.uv.sources] resolves from PyPI instead of locally."""
    sources = root_config["tool"]["uv"]["sources"]
    missing = sorted(set(EXPECTED_MEMBERS) - set(sources))
    assert not missing, f"workspace members not declared in [tool.uv.sources]: {missing}"


@pytest.mark.unit
def test_no_package_escapes_the_layering_contracts(root_config: dict[str, Any]) -> None:
    """Every import package must appear in the layers contract.

    A package absent from the contract is unconstrained: it could import anything,
    in any direction, and import-linter would report success.
    """
    contracts = root_config["tool"]["importlinter"]["contracts"]
    layers_contract = next(c for c in contracts if c["type"] == "layers")

    covered = {name.strip() for layer in layers_contract["layers"] for name in layer.split("|")}
    expected = {import_name for _, import_name in EXPECTED_MEMBERS.values()}

    assert expected - covered == set(), (
        f"packages missing from the layers contract: {sorted(expected - covered)}"
    )
    assert covered - expected == set(), (
        f"layers contract names unknown packages: {sorted(covered - expected)}"
    )


@pytest.mark.unit
def test_importlinter_root_packages_match_the_workspace(root_config: dict[str, Any]) -> None:
    declared = set(root_config["tool"]["importlinter"]["root_packages"])
    expected = {import_name for _, import_name in EXPECTED_MEMBERS.values()}
    assert declared == expected


# Patterns that indicate a real credential rather than a placeholder. Kept narrow
# on purpose: a noisy secret scanner gets disabled, and a disabled one catches nothing.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),  # OpenAI-style key
    re.compile(r"sk_live_[A-Za-z0-9]{10,}"),  # live secret key
    re.compile(r"whsec_[A-Za-z0-9+/=]{20,}"),  # webhook signing secret
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\."),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),  # private key block
)


@pytest.mark.unit
def test_env_example_contains_no_real_credentials() -> None:
    """.env.example is committed, so a pasted credential here is a public leak."""
    example = REPO_ROOT / ".env.example"
    assert example.is_file(), ".env.example must exist — it documents the config surface"

    content = example.read_text(encoding="utf-8")
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(content)
        assert match is None, (
            f".env.example appears to contain a real credential matching {pattern.pattern!r}. "
            "Replace it with a placeholder and rotate the exposed value."
        )


@pytest.mark.unit
def test_env_files_are_ignored_by_git() -> None:
    """The one-character difference between .env and .env.example is a breach."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    stripped = [line.strip() for line in gitignore]
    assert ".env" in stripped, ".gitignore must ignore .env"
    assert "!.env.example" in stripped, ".gitignore must re-include .env.example"
