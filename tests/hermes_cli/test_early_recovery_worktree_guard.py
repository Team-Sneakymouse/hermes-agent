"""A test checkout must not probe recovery records in its shared Git metadata."""

from hermes_cli import _early_recovery as recovery


def test_test_checkout_skips_recovery_before_git_metadata_io(monkeypatch):
    root = recovery._project_root()

    def refuse_git_metadata(_root):
        raise AssertionError("test checkout must not inspect shared recovery metadata")

    monkeypatch.setattr(recovery, "interrupted_pull_marker", refuse_git_metadata)
    assert recovery.restore_interrupted_pull(root) is False
