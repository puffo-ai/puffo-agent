"""Worker tasks must not die unclaimed: spawn attaches an exception-logging
done-callback, and no bare ensure_future/create_task remains in the tree."""

import ast
import asyncio
import inspect
import logging
from pathlib import Path

import pytest

from puffo_agent.agent.directory_cache import warm_member_caches
from puffo_agent.tasks import spawn

_SRC = Path(__file__).resolve().parent.parent / "src" / "puffo_agent"
_HELPER = _SRC / "tasks.py"
_SPAWNERS = {"ensure_future", "create_task"}


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_clean_completion_logs_nothing(caplog):
    async def ok():
        return 7

    with caplog.at_level(logging.DEBUG, logger="puffo_agent.tasks"):
        task = spawn(ok(), name="ok")
        assert await task == 7
        await _settle()

    assert caplog.records == []


@pytest.mark.asyncio
async def test_unclaimed_exception_is_logged_with_name_and_traceback(caplog):
    async def boom():
        raise ValueError("message backup key has invalid length")

    with caplog.at_level(logging.ERROR, logger="puffo_agent.tasks"):
        spawn(boom(), name="reminder_sync.run")
        await _settle()

    records = _errors(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.getMessage() == "worker task died: reminder_sync.run"
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], ValueError)
    assert "message backup key has invalid length" in str(record.exc_info[1])


@pytest.mark.asyncio
async def test_pre_built_task_is_renamed_before_reporting_once(caplog):
    async def boom():
        raise ValueError("prebuilt")

    with caplog.at_level(logging.ERROR, logger="puffo_agent.tasks"):
        task = asyncio.get_running_loop().create_task(boom(), name="original")
        returned = spawn(task, name="renamed")
        returned_again = spawn(task, name="renamed-again")
        await _settle()

    assert returned is task
    assert returned_again is task
    assert task.get_name() == "renamed-again"
    assert len(_errors(caplog)) == 1
    assert _errors(caplog)[0].getMessage() == "worker task died: renamed-again"


@pytest.mark.asyncio
async def test_cancelled_task_is_not_reported(caplog):
    started = asyncio.Event()

    async def sleeper():
        started.set()
        await asyncio.sleep(3600)

    with caplog.at_level(logging.DEBUG, logger="puffo_agent.tasks"):
        task = spawn(sleeper(), name="sleeper")
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _settle()

    assert caplog.records == []


@pytest.mark.asyncio
async def test_returned_task_is_named_and_awaitable():
    async def ok():
        return "value"

    task = spawn(ok(), name="named")
    assert isinstance(task, asyncio.Task)
    assert task.get_name() == "named"
    assert await task == "value"


@pytest.mark.asyncio
async def test_spawn_without_name_still_reports(caplog):
    async def boom():
        raise RuntimeError("nameless")

    with caplog.at_level(logging.ERROR, logger="puffo_agent.tasks"):
        spawn(boom())
        await _settle()

    records = _errors(caplog)
    assert len(records) == 1
    assert isinstance(records[0].exc_info[1], RuntimeError)


@pytest.mark.asyncio
async def test_plain_future_is_reported_without_set_name(caplog):
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    with caplog.at_level(logging.ERROR, logger="puffo_agent.tasks"):
        returned = spawn(future, name="ignored-on-future")
        assert returned is future
        future.set_exception(OSError("disk"))
        await _settle()

    records = _errors(caplog)
    assert len(records) == 1
    assert isinstance(records[0].exc_info[1], OSError)


@pytest.mark.asyncio
async def test_existing_done_callback_still_runs(caplog):
    tasks: set[asyncio.Future] = set()

    async def boom():
        raise ValueError("both callbacks")

    with caplog.at_level(logging.ERROR, logger="puffo_agent.tasks"):
        task = spawn(boom(), name="housekept")
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        await _settle()

    assert tasks == set()
    assert len(_errors(caplog)) == 1


@pytest.mark.asyncio
async def test_owned_companion_is_still_reported_once(caplog):
    async def boom():
        raise ValueError("claimed")

    with caplog.at_level(logging.ERROR, logger="puffo_agent.tasks"):
        task = spawn(boom(), name="claimed")
        with pytest.raises(ValueError):
            await task
        await _settle()

    records = _errors(caplog)
    assert len(records) == 1
    assert records[0].getMessage() == "worker task died: claimed"
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], ValueError)


def test_failure_reporting_cannot_be_disabled():
    assert "report_failure" not in inspect.signature(spawn).parameters
    # Supervised paths can add another authoritative record for the same
    # exception, so incident counters must not equate ERROR lines with faults.
    doc = inspect.getdoc(spawn)
    assert doc is not None
    assert "deduplicate by exception" in doc


@pytest.mark.asyncio
async def test_start_services_shaped_failure_surfaces(caplog):
    """Prof-Puffo shape: bring-up spawns a service task nobody awaits."""
    ready = asyncio.Event()

    async def prepare_reminder_sync():
        raise ValueError("message backup key has invalid length")

    async def start_services():
        spawn(prepare_reminder_sync(), name="reminder_sync.run")
        ready.set()

    with caplog.at_level(logging.ERROR, logger="puffo_agent.tasks"):
        await start_services()
        await ready.wait()
        await _settle()

    records = _errors(caplog)
    assert len(records) == 1
    assert "reminder_sync.run" in records[0].getMessage()


@pytest.mark.asyncio
async def test_cache_warm_reports_both_failed_siblings_once(caplog):
    class Http:
        async def get(self, path):
            assert path == "/spaces"
            return {"spaces": [{"space_id": "sp_1", "name": "One"}]}

    async def get_members(_space_id):
        raise ValueError("members failed")

    async def warm_channels(_space_id):
        await asyncio.sleep(0)
        raise RuntimeError("channels failed")

    async def fetch_profiles(_slugs):
        raise AssertionError("no profiles should be fetched")

    with caplog.at_level(logging.ERROR, logger="puffo_agent.tasks"):
        await warm_member_caches(
            http=Http(),
            log=logging.getLogger("test.directory_cache"),
            space_name_cache={},
            profile_cache={},
            get_members=get_members,
            warm_channels=warm_channels,
            fetch_profiles=fetch_profiles,
        )
        await _settle()

    records = _errors(caplog)
    assert len(records) == 2
    assert {type(record.exc_info[1]) for record in records} == {ValueError, RuntimeError}


def test_spawn_without_running_loop_raises():
    async def never():
        return None

    coro = never()
    try:
        with pytest.raises(RuntimeError):
            spawn(coro, name="no-loop")
    finally:
        coro.close()


def _bare_spawn_sites(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    asyncio_modules: set[str] = set()
    direct_spawners: set[str] = set()
    task_group_factories: set[str] = set()
    task_group_owners: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio":
                    asyncio_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "asyncio":
            for alias in node.names:
                local_name = alias.asname or alias.name
                if alias.name in _SPAWNERS:
                    direct_spawners.add(local_name)
                elif alias.name == "TaskGroup":
                    task_group_factories.add(local_name)

    def is_task_group_factory(expr: ast.expr) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in task_group_factories
        return (
            isinstance(expr, ast.Attribute)
            and expr.attr == "TaskGroup"
            and isinstance(expr.value, ast.Name)
            and expr.value.id in asyncio_modules
        )

    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            context = item.context_expr
            if (
                isinstance(context, ast.Call)
                and is_task_group_factory(context.func)
                and isinstance(item.optional_vars, ast.Name)
            ):
                task_group_owners.add(item.optional_vars.id)

    def is_loop_owner(expr: ast.expr) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in asyncio_modules or "loop" in expr.id.lower()
        if isinstance(expr, ast.Attribute):
            return "loop" in expr.attr.lower()
        if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Attribute):
            return False
        return (
            expr.func.attr in {"get_running_loop", "get_event_loop"}
            and isinstance(expr.func.value, ast.Name)
            and expr.func.value.id in asyncio_modules
        )

    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in direct_spawners:
            hits.append(f"{path}:{node.lineno} name.{func.id}")
        elif isinstance(func, ast.Attribute) and func.attr == "ensure_future":
            if isinstance(func.value, ast.Name) and func.value.id in asyncio_modules:
                hits.append(f"{path}:{node.lineno} attribute.{func.attr}")
        elif isinstance(func, ast.Attribute) and func.attr == "create_task":
            if isinstance(func.value, ast.Name) and func.value.id in task_group_owners:
                continue
            if is_loop_owner(func.value):
                hits.append(f"{path}:{node.lineno} attribute.{func.attr}")
    return hits


def test_no_bare_task_spawn_remains_in_tree():
    sources = sorted(p for p in _SRC.rglob("*.py") if p != _HELPER)
    assert sources, "source sweep found no files"
    hits = [site for path in sources for site in _bare_spawn_sites(path)]
    assert hits == []


def test_helper_owns_both_low_level_spawn_shapes():
    hits = _bare_spawn_sites(_HELPER)
    assert len(hits) == 2
    assert any("attribute.create_task" in hit for hit in hits)
    assert any("attribute.ensure_future" in hit for hit in hits)


@pytest.mark.parametrize(
    "source",
    [
        "import asyncio\nasyncio.get_running_loop().create_task(work())",
        "import asyncio\nloop = asyncio.get_running_loop()\nloop.create_task(work())",
        "import asyncio\nself._loop.create_task(work())",
        "from asyncio import create_task\ncreate_task(work())",
        "from asyncio import create_task as schedule\nschedule(work())",
        "import asyncio as aio\naio.ensure_future(work())",
    ],
)
def test_fleet_lint_detects_indirect_spawn_shapes(tmp_path, source):
    path = tmp_path / "indirect.py"
    path.write_text(source, encoding="utf-8")
    assert len(_bare_spawn_sites(path)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "import asyncio\nasync with asyncio.TaskGroup() as tg:\n    tg.create_task(work())",
        "from asyncio import TaskGroup as Group\nasync with Group() as owner:\n    owner.create_task(work())",
        "domain.create_task(record())",
    ],
)
def test_fleet_lint_allows_supervised_and_unrelated_create_task(tmp_path, source):
    path = tmp_path / "owned.py"
    path.write_text(source, encoding="utf-8")
    assert _bare_spawn_sites(path) == []
