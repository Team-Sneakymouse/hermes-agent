"""Tests for explicit ownership by a wrapped external gateway supervisor."""

from types import SimpleNamespace

import pytest

from hermes_cli import gateway


def _clear_native_supervisor_markers(monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")


def test_external_marker_identifies_supervisor_process(monkeypatch):
    _clear_native_supervisor_markers(monkeypatch)
    monkeypatch.setenv(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")

    assert gateway._running_under_gateway_supervisor() is True


def test_gateway_run_external_supervisor_flag_marks_process(monkeypatch):
    monkeypatch.delenv(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    monkeypatch.setattr(
        gateway, "_maybe_redirect_run_to_s6_supervision", lambda _args: False
    )
    observed = []
    monkeypatch.setattr(
        gateway,
        "run_gateway",
        lambda *_args, **_kwargs: observed.append(
            gateway.os.environ.get(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV)
        ),
    )

    gateway._gateway_command_inner(
        SimpleNamespace(gateway_command="run", external_supervisor=True)
    )

    assert observed == ["1"]


def test_update_hands_external_supervisor_gateway_back_without_watcher(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_argv",
        lambda _pid: [
            "python",
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
            "--external-supervisor",
        ],
    )
    monkeypatch.setattr(
        gateway,
        "launch_detached_profile_gateway_restart",
        lambda *_args: pytest.fail("detached watcher must not be launched"),
    )

    assert gateway._prepare_profile_gateway_update_restart("work", 1234) == (
        "external-supervisor"
    )


def test_update_hands_generated_launchd_inner_argv_back_without_watcher(monkeypatch):
    """New launchd ProgramArguments put --external-supervisor on the grandchild."""
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_argv",
        lambda _pid: [
            "/usr/bin/python3",
            "-m",
            "hermes_cli.main",
            "--profile",
            "work",
            "gateway",
            "run",
            "--replace",
            "--external-supervisor",
        ],
    )
    monkeypatch.setattr(
        gateway,
        "launch_detached_profile_gateway_restart",
        lambda *_args: pytest.fail("detached watcher must not be launched"),
    )

    assert gateway._prepare_profile_gateway_update_restart("work", 1234) == (
        "external-supervisor"
    )


def test_update_follows_wrapper_upgrade_of_stale_plist_argv(monkeypatch):
    """stderr_timestamp upgrades stale inner argv so update sees the flag."""
    from hermes_cli.stderr_timestamp import _prepare_child_command

    stale = [
        "/usr/bin/python3",
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
        "--replace",
    ]
    upgraded = _prepare_child_command(
        stale, {"XPC_SERVICE_NAME": "ai.hermes.gateway-work"}
    )
    assert upgraded[-1] == "--external-supervisor"

    monkeypatch.setattr(gateway, "_capture_gateway_argv", lambda _pid: upgraded)
    monkeypatch.setattr(
        gateway,
        "launch_detached_profile_gateway_restart",
        lambda *_args: pytest.fail("detached watcher must not be launched"),
    )

    assert gateway._prepare_profile_gateway_update_restart("work", 1234) == (
        "external-supervisor"
    )


# A PM-managed venv shim re-execs the gateway as ``python -I -c "...runpy..."``, so the supervisor's
# ``--external-supervisor`` survives only inside the ``-c`` source, never as an argv element.
_RUNPY_SHIM_ARGV = [
    "/home/u/.hermes/tools/python-3.14/bin/python3", "-I", "-c",
    "import sys, runpy; sys.argv = ['/home/u/.hermes/hermes-agent/hermes_cli/main.py', 'gateway', 'run',"
    " '--external-supervisor']; runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)",
]


def test_update_hands_back_a_gateway_that_declares_its_external_supervisor(monkeypatch):
    """The gateway's own ``identify`` answer decides, not the argv shape a launcher shim left behind."""
    from gateway import control_socket

    monkeypatch.setattr(gateway, "_capture_gateway_argv", lambda _pid: list(_RUNPY_SHIM_ARGV))
    monkeypatch.setattr(control_socket, "identify_gateway",
                        lambda _home: {"pid": 1234, "supervisor": "external", "launch_supervisor": "external"})
    for name in ("launch_detached_profile_gateway_restart", "launch_detached_gateway_restart_by_cmdline"):
        monkeypatch.setattr(gateway, name, lambda *_args: pytest.fail("detached watcher must not be launched"))

    assert gateway._prepare_profile_gateway_update_restart("default", 1234) == "external-supervisor"


def test_update_relaunches_a_shim_launched_gateway_that_declares_no_supervisor(monkeypatch):
    from gateway import control_socket

    monkeypatch.setattr(gateway, "_capture_gateway_argv", lambda _pid: list(_RUNPY_SHIM_ARGV[:3]) + ["pass"])
    monkeypatch.setattr(control_socket, "identify_gateway",
                        lambda _home: {"pid": 1234, "supervisor": "none", "launch_supervisor": "manual"})
    launched = []
    monkeypatch.setattr(gateway, "launch_detached_profile_gateway_restart",
                        lambda profile, pid: launched.append((profile, pid)) or True)

    assert gateway._prepare_profile_gateway_update_restart("default", 1234) == "detached"
    assert launched == [("default", 1234)]


def test_update_still_uses_detached_watcher_without_supervisor_flag(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_argv",
        lambda _pid: [
            "python",
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
            "--replace",
        ],
    )
    launched = []
    monkeypatch.setattr(
        gateway,
        "launch_detached_profile_gateway_restart",
        lambda profile, pid: launched.append((profile, pid)) or True,
    )

    assert gateway._prepare_profile_gateway_update_restart("work", 1234) == "detached"
    assert launched == [("work", 1234)]


