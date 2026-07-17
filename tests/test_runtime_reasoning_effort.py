

def test_heartbeat_runtime_info_reports_inference_level():
    from types import SimpleNamespace

    from puffo_agent.portal.worker import Worker

    w = Worker.__new__(Worker)
    w.agent_cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            kind="cli-local", provider="openai", harness="codex",
            model="gpt-5.5", inference_level="high",
        ),
    )
    assert w._runtime_info()["inference_level"] == "high"
