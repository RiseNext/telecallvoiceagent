"""`rn_agent` imports no orchestration framework and no vendor SDK — at runtime.

`lint-imports` asserts the *static* version of this and is the primary guard; it is
also what the Phase-2 definition of done names ("lint-imports confirms rn_agent
imports no LangChain and no rn_persistence"). This file is the runtime complement,
because two things the static graph cannot see would still hurt:

* a **deferred import** — `import langchain` inside a function body executes at call
  time, and a module-level graph misses it;
* a **transitive** pull — a dependency of a dependency dragging `langsmith` in, which
  is exactly the hazard `langchain-core` presents, since it hard-depends on it.

**SQLAlchemy is deliberately not in the forbidden list here.** `rn_agent` may not
*import* `rn_persistence` — that is the contract, and `lint-imports` enforces it —
but `rn_agent` legitimately depends on `rn_services`, whose `__init__` constructs
repository-backed services, so the ORM is loaded transitively. The architecture
already accepts that: the import contract states plainly that "rn_voice depends on
rn_services, so SQLAlchemy and the Postgres driver are present in the gateway image.
What is prevented is the gateway opening a session of its own." Asserting that the
module is absent from `sys.modules` would be a stricter rule than the architecture
makes, invented here rather than decided in an ADR — so it is not asserted.

What *is* asserted about persistence is the thing that matters and is real: no
`rn_agent` module imports `rn_persistence` or SQLAlchemy directly. That check lives in
`lint-imports`, where a violation names the offending module.

Run in a subprocess deliberately: by the time this file executes, the pytest session
has already imported half the workspace, so an in-process `sys.modules` check would
prove nothing.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytestmark = [pytest.mark.unit]

#: Modules that must not be loaded by importing `rn_agent`.
#:
#: Every one of these is genuinely absent from the agent layer's dependency tree, so
#: a hit here means a real regression — a new import, a deferred one, or a dependency
#: that grew a tail. See the module docstring for why SQLAlchemy is not on the list.
_FORBIDDEN = (
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph",
    "langsmith",
    "openai",
    "boto3",
    "aioboto3",
    "clerk_backend_api",
    "svix",
    "fastapi",
    "starlette",
    "taskiq",
    "redis",
)


def _modules_after_importing(target: str) -> set[str]:
    """Import `target` in a fresh interpreter and report what came with it."""
    script = textwrap.dedent(
        f"""
        import sys, json
        import {target}
        print(json.dumps(sorted(sys.modules)))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(__import__("json").loads(completed.stdout))


def test_importing_rn_agent_loads_no_framework_or_vendor_sdk() -> None:
    loaded = _modules_after_importing("rn_agent")
    offenders = sorted(name for name in _FORBIDDEN if name in loaded)
    assert not offenders, (
        f"importing rn_agent pulled in {offenders}. "
        "Tool schemas come from plain Pydantic; the LangChain adapter belongs in "
        "rn_orchestration. See ADR-004."
    )


def test_no_rn_agent_module_imports_persistence_directly() -> None:
    """The Phase-2 gate: `rn_agent` imports no `rn_persistence`, no SQLAlchemy.

    Checked by reading the source rather than `sys.modules`, because the transitive
    load through `rn_services` is legitimate (see the module docstring) while a
    *direct* import is a contract violation. `lint-imports` is the authoritative
    check; this repeats it in the test suite so a failure shows up in the same run as
    everything else, and names the file.
    """
    import ast
    import pathlib

    import rn_agent

    root = pathlib.Path(next(iter(rn_agent.__path__)))
    forbidden_roots = {"rn_persistence", "sqlalchemy", "asyncpg", "psycopg", "alembic"}
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue
            offenders.extend(
                f"{path.relative_to(root)}:{node.lineno} -> {name}"
                for name in names
                if name.split(".")[0] in forbidden_roots
            )

    assert not offenders, "rn_agent must reach domain data only through rn_services:\n" + "\n".join(
        offenders
    )


def test_importing_the_tool_registry_alone_is_also_clean() -> None:
    """The narrower path the voice gateway will take at session open."""
    loaded = _modules_after_importing("rn_agent.tools")
    offenders = sorted(name for name in _FORBIDDEN if name in loaded)
    assert not offenders, f"importing rn_agent.tools pulled in {offenders}"


def test_the_llm_seam_pulls_in_no_vendor_sdk() -> None:
    """`rn_providers.llm` is a protocol and some frozen dataclasses. Phase 2 ships no
    vendor adapter, and this is what says so in a way that cannot drift."""
    loaded = _modules_after_importing("rn_providers.llm")
    assert "openai" not in loaded


def test_the_fake_llm_provider_pulls_in_no_vendor_sdk() -> None:
    loaded = _modules_after_importing("rn_providers.fakes")
    offenders = sorted(name for name in ("openai", "httpx", "websockets") if name in loaded)
    assert not offenders, f"the LLM fake pulled in {offenders}"


def test_the_services_contract_module_imports_no_persistence() -> None:
    """`rn_services.contracts` is the seam the agent layer names.

    It holds protocols and DTOs only. A repository import here would mean the agent
    layer's dependency was on an implementation rather than on a capability — which is
    what would make the agent layer untestable without a database.

    Source-level, like the `rn_agent` check above: importing any `rn_services`
    submodule runs the package `__init__`, which legitimately constructs
    repository-backed services, so `sys.modules` cannot answer this question.
    """
    import ast
    import pathlib

    from rn_services import contracts

    source = pathlib.Path(str(contracts.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"rn_persistence", "sqlalchemy", "asyncpg"}, sorted(imported)
