"""`hermes update` hands an externally supervised dashboard back to its supervisor.

A dashboard run by a SysV/s6/runit loop is invisible to the systemd and launchd probes, so the
update's end-of-run cleanup classified it as manual and respawned it detached from its recorded
argv. The detached copy then held the port, and the supervisor's own relaunch exited 75 on
"port in use" every few seconds, forever (80k restarts on one host). A backend that declares
``--external-supervisor`` is stopped and left for its supervisor to relaunch on the new code.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from hermes_cli import main_dashboard
from hermes_cli.dashboard_procs import _kill_stale_dashboard_processes
from hermes_cli.subcommands.dashboard import build_serve_parser


def _start_backend(*args: str) -> subprocess.Popen:
    """A real process whose /proc cmdline carries *args* after the ``-c`` source, the shape the PM
    launcher gives a ``hermes dashboard`` it execs."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "dashboard", "--port", "9119", *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run_update_cleanup(pid: int) -> list[list[list[str]]]:
    """Run the update's restart-managed cleanup against *pid* alone; return the respawn calls."""
    respawned: list[list[list[str]]] = []
    with patch.object(main_dashboard, "_restart_managed_dashboard_service", return_value=False), \
         patch.object(main_dashboard, "_find_stale_dashboard_pids", return_value=[pid]), \
         patch.object(main_dashboard, "_get_systemd_service_for_pid", return_value=None), \
         patch.object(main_dashboard, "_get_pid_cgroup_path", return_value=None), \
         patch.object(main_dashboard, "_respawn_dashboard_processes",
                      side_effect=lambda cmds: respawned.append(cmds) or []):
        result = _kill_stale_dashboard_processes(restart_managed=True)
    assert result["killed"] == [pid]
    return respawned


@pytest.mark.platforms("linux")
def test_externally_supervised_dashboard_is_stopped_not_respawned(capsys):
    proc = _start_backend("--external-supervisor")
    try:
        respawned = _run_update_cleanup(proc.pid)
        assert proc.wait(timeout=10) is not None
    finally:
        proc.kill()
        proc.wait()

    assert respawned == []
    out = capsys.readouterr().out
    assert f"PID {proc.pid} handed back to its external supervisor" in out
    assert "when you're ready" not in out


@pytest.mark.platforms("linux")
def test_manual_dashboard_is_still_respawned_from_its_argv():
    proc = _start_backend()
    try:
        respawned = _run_update_cleanup(proc.pid)
    finally:
        proc.kill()
        proc.wait()

    assert len(respawned) == 1
    (command,) = respawned[0]
    assert command[-3:] == ["dashboard", "--port", "9119"]


def test_serve_accepts_external_supervisor_declaration():
    assert build_serve_parser(cmd_dashboard=lambda _args: None).parse_args(
        ["--external-supervisor"]).external_supervisor is True
