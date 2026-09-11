"""Public ``puffo-agent`` command-tree compatibility coverage."""

from puffo_agent.portal.cli import build_parser


def test_top_level_commands_remain_on_the_public_parser():
    parser = build_parser()
    command_action = next(action for action in parser._actions if action.dest == "cmd")

    assert set(command_action.choices) == {
        "agent",
        "autostart",
        "check-update",
        "config",
        "machine",
        "start",
        "status",
        "stop",
        "test",
        "version",
        "ws-local",
    }
