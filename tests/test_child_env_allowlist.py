"""Harness child environments are built from an allowlist, not a deny-list."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from puffo_agent.agent.harness.support.child_env import (
    PROVIDER_CREDENTIAL_ENV_NAMES,
    build_child_environment,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_AMBIENT = {
    "PATH": "/usr/bin",
    "HOME": "/home/op",
    "LC_ALL": "C.UTF-8",
    "XDG_CONFIG_HOME": "/home/op/.config",
    "HTTPS_PROXY": "http://proxy:8080",
    "NODE_EXTRA_CA_CERTS": "/etc/ca.pem",
    "OPENAI_API_KEY": "ambient-openai",
    "ANTHROPIC_API_KEY": "ambient-anthropic",
    "AWS_SECRET_ACCESS_KEY": "ambient-aws",
    "SOME_INTERNAL_TOKEN": "not-on-any-list",
}


@pytest.mark.parametrize("name", sorted(PROVIDER_CREDENTIAL_ENV_NAMES))
def test_ambient_provider_credentials_are_never_inherited(name):
    env = build_child_environment(source={**_AMBIENT, name: "ambient"})
    assert name not in env


@pytest.mark.parametrize("name", sorted(PROVIDER_CREDENTIAL_ENV_NAMES))
def test_an_override_cannot_reintroduce_a_provider_credential(name):
    """The ordering the Claude path already had, now enforced for everyone.

    Stripping only before merging overrides would let operator config smuggle
    an ambient key back in. The strip therefore runs after the merge.
    """
    env = build_child_environment(source=_AMBIENT, overrides={name: "smuggled"})
    assert name not in env


def test_controlled_injection_is_the_one_permitted_path():
    env = build_child_environment(
        source=_AMBIENT,
        overrides={"OPENAI_API_KEY": "smuggled"},
        controlled={"OPENAI_API_KEY": "controlled"},
    )
    assert env["OPENAI_API_KEY"] == "controlled"


def test_unlisted_ambient_variables_are_dropped():
    """The allowlist property: an unnamed secret does not pass by default.

    This is what a deny-list cannot give. SOME_INTERNAL_TOKEN is on no list
    and must still not reach the child.
    """
    env = build_child_environment(source=_AMBIENT)
    assert "SOME_INTERNAL_TOKEN" not in env


def test_operational_variables_survive():
    """Guards the other failure mode: an allowlist so tight it breaks agents.

    Dropping proxy or CA settings strands every agent behind a corporate
    proxy, and presents as "the harness is broken".
    """
    env = build_child_environment(source=_AMBIENT)
    for name in ("PATH", "HOME", "HTTPS_PROXY", "NODE_EXTRA_CA_CERTS"):
        assert env[name] == _AMBIENT[name], name


def test_open_ended_prefixes_survive():
    env = build_child_environment(source=_AMBIENT)
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["XDG_CONFIG_HOME"] == "/home/op/.config"


def test_extra_allowed_admits_a_runtime_specific_name():
    env = build_child_environment(
        source={**_AMBIENT, "CODEX_HOME": "/agents/a/.codex"},
        extra_allowed=("CODEX_HOME",),
    )
    assert env["CODEX_HOME"] == "/agents/a/.codex"


def _import_bindings(tree: ast.AST) -> tuple[set[str], set[str], set[str], set[str]]:
    """Resolve the simple aliases relevant to the environment boundary."""
    os_modules: set[str] = set()
    environ_names: set[str] = set()
    asyncio_modules: set[str] = set()
    subprocess_functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_modules.add(alias.asname or alias.name)
                elif alias.name == "asyncio":
                    asyncio_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "environ":
                    environ_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "asyncio":
            for alias in node.names:
                if alias.name in {"create_subprocess_exec", "create_subprocess_shell"}:
                    subprocess_functions.add(alias.asname or alias.name)
    return os_modules, environ_names, asyncio_modules, subprocess_functions


def _ambient_env_reads(path) -> list[int]:
    """Line numbers where a module rebuilds the ambient child environment.

    Catches os.environ.copy() / dict(os.environ) / {**os.environ, ...} and
    SDK-owned default_environment(), whose contents can drift independently
    of Puffo's credential contract.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    os_modules, environ_names, _, _ = _import_bindings(tree)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            value = node.value
            if isinstance(value, ast.Name) and value.id in os_modules:
                offenders.append(node.lineno)
        if isinstance(node, ast.Name) and node.id in environ_names:
            offenders.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "default_environment"
        ):
            offenders.append(node.lineno)
    return offenders


def _implicit_subprocess_envs(
    path, *, exempt_functions: frozenset[str] = frozenset()
) -> list[int]:
    """Find asyncio subprocesses that inherit the ambient environment.

    ``env`` omitted and ``env=None`` have the same runtime behavior. Driver
    child processes must instead consume the already-sanitized RuntimeSpec
    environment. Narrow function exemptions are reserved for host utilities.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    _, _, asyncio_modules, subprocess_functions = _import_bindings(tree)

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.functions: list[str] = []
            self.offenders: list[int] = []

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_Call(self, node: ast.Call) -> None:
            is_subprocess = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"create_subprocess_exec", "create_subprocess_shell"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in asyncio_modules
            ) or (
                isinstance(node.func, ast.Name) and node.func.id in subprocess_functions
            )
            function = self.functions[-1] if self.functions else None
            if is_subprocess and function not in exempt_functions:
                env = next(
                    (kw.value for kw in node.keywords if kw.arg == "env"),
                    None,
                )
                if env is None or (isinstance(env, ast.Constant) and env.value is None):
                    self.offenders.append(node.lineno)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    return visitor.offenders


# Modules allowed to read the ambient environment, and why. This is the whole
# harness package rather than a list of drivers on purpose: a hand-written
# inclusion list makes every module nobody thought of exempt by default, which
# is the same shape as the deny-list this allowlist replaced. docker_runtime
# was in exactly that position -- it reads os.environ and was never on the old
# six-module list.
AMBIENT_ENV_EXEMPTIONS = {
    "src/puffo_agent/agent/harness/support/child_env.py": "the allowlist boundary itself; it is what reads ambient",
    "src/puffo_agent/agent/harness/runtime/docker_runtime.py": "os.environ there is the host docker *client* env, not the agent "
    "child's: the container's environment is set by `docker exec -e` "
    "flags, and API keys travel by name so they stay out of argv",
}

# These functions launch host maintenance tools, not agent/provider children.
# Keep exemptions function-scoped so another subprocess added to the same
# module does not silently inherit the host environment.
IMPLICIT_SUBPROCESS_ENV_EXEMPTIONS = {
    "src/puffo_agent/agent/harness/support/subprocess_io.py": {
        "signal_process_tree": "Windows taskkill is a host process-tree utility",
    },
    "src/puffo_agent/agent/harness/runtime/docker_support.py": {
        "_build_image": "docker build is a host container-management command",
        "run_cmd": "docker inspection commands run on the host",
    },
}


def _harness_modules() -> list[str]:
    return [
        str(path.relative_to(_REPO_ROOT))
        for path in sorted((_REPO_ROOT / "src/puffo_agent/agent/harness").rglob("*.py"))
    ]


def test_the_ambient_scan_actually_walks_the_harness_package():
    """A directory walk that matches nothing would pass in silence."""
    modules = _harness_modules()

    assert "src/puffo_agent/agent/harness/runtime/local_runtime.py" in modules
    assert "src/puffo_agent/agent/harness/drivers/pi.py" in modules
    assert len(modules) > 10


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef f():\n    return dict(os.environ)\n",
        "from os import environ\ndef f():\n    return dict(environ)\n",
        "from os import environ as host_env\ndef f():\n    return dict(host_env)\n",
    ],
)
def test_ambient_scan_catches_import_shapes(tmp_path, source):
    path = tmp_path / "module.py"
    path.write_text(source, encoding="utf-8")

    assert _ambient_env_reads(path)


@pytest.mark.parametrize(
    ("call", "is_offender"),
    [
        ('asyncio.create_subprocess_exec("agent")', True),
        ('asyncio.create_subprocess_exec("agent", env=None)', True),
        ('asyncio.create_subprocess_exec("agent", env=spec.environment)', False),
    ],
)
def test_subprocess_scan_requires_explicit_sanitized_env(tmp_path, call, is_offender):
    path = tmp_path / "driver.py"
    path.write_text(
        f"import asyncio\nasync def spawn(spec):\n    await {call}\n",
        encoding="utf-8",
    )

    assert bool(_implicit_subprocess_envs(path)) is is_offender


@pytest.mark.parametrize("relpath", sorted(AMBIENT_ENV_EXEMPTIONS))
def test_every_ambient_exemption_still_reads_ambient(relpath):
    """An exemption for a module that stopped reading ambient is stale."""
    assert _ambient_env_reads(_REPO_ROOT / relpath), (
        f"{relpath} is exempted from the ambient-environment rule but no "
        "longer reads os.environ; drop the exemption."
    )


@pytest.mark.parametrize(
    ("relpath", "function"),
    [
        (relpath, function)
        for relpath, functions in sorted(IMPLICIT_SUBPROCESS_ENV_EXEMPTIONS.items())
        for function in sorted(functions)
    ],
)
def test_every_implicit_subprocess_exemption_is_still_needed(relpath, function):
    path = _REPO_ROOT / relpath
    exemptions = frozenset(IMPLICIT_SUBPROCESS_ENV_EXEMPTIONS[relpath])

    assert not _implicit_subprocess_envs(path, exempt_functions=exemptions)
    assert _implicit_subprocess_envs(path, exempt_functions=exemptions - {function}), (
        f"{relpath}:{function} no longer implicitly inherits the host env; "
        "drop the stale exemption."
    )


@pytest.mark.parametrize("relpath", _harness_modules())
def test_harness_child_environment_boundary_never_rereads_ambient(relpath):
    """Spec construction and real spawn must share one allowlist boundary.

    Sanitizing a RuntimeSpec is ineffective if a Driver merges ``os.environ``
    back at spawn. SDK-owned default allowlists are also forbidden here: their
    contents can drift independently of Puffo's credential contract.
    """
    path = _REPO_ROOT / relpath
    offenders = [] if relpath in AMBIENT_ENV_EXEMPTIONS else _ambient_env_reads(path)
    subprocess_offenders = _implicit_subprocess_envs(
        path,
        exempt_functions=frozenset(IMPLICIT_SUBPROCESS_ENV_EXEMPTIONS.get(relpath, {})),
    )

    assert not offenders and not subprocess_offenders, (
        f"{relpath} rebuilds or implicitly inherits ambient child env at "
        f"line(s) {sorted(offenders + subprocess_offenders)}; "
        "build the child environment with child_env.build_child_environment "
        "once, then pass RuntimeSpec.environment through unchanged."
    )
