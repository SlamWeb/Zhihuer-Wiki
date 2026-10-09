import json
from pathlib import Path

from zhihuer_wiki.cli import main


SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "demo-raw"


def test_manual_build_prepares_full_jobs_and_status(tmp_path, capsys):
    output = tmp_path / "build"
    assert main(["build", "demo-author", "--raw-dir", str(SAMPLES), "--out", str(output), "--agent", "manual"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["jobs"] and result["jobs"][0]["stage"] == "extraction"
    assert not (output / "wiki").exists()
    assert main(["status", "--workspace", str(output)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["documents"] == 4 and status["state"] == "pending"
    assert main(["build", "demo-author", "--raw-dir", str(SAMPLES), "--out", str(output), "--agent", "manual"]) == 1
    assert main(["validate", "--workspace", str(output)]) == 1


def test_invalid_profile_is_rejected_before_creating_output(tmp_path):
    output = tmp_path / "build"
    assert main(["build", "https://evil.example/people/test", "--out", str(output), "--agent", "manual"]) == 1
    assert not output.exists()


def test_missing_workspace_reports_failure(tmp_path):
    assert main(["next", "--workspace", str(tmp_path / "missing")]) == 1


def test_runner_rechecks_already_completed_workspace(tmp_path, monkeypatch):
    import pytest
    from zhihuer_wiki import agent_runner, pipeline
    monkeypatch.setattr(pipeline, "next_jobs", lambda _: [])
    monkeypatch.setattr(pipeline, "status", lambda _: {"state": "completed"})
    monkeypatch.setattr(pipeline, "validate", lambda _: {"valid": False, "errors": ["Changed source snapshot"]})
    with pytest.raises(agent_runner.AgentError, match="Changed source snapshot"):
        agent_runner.drive(tmp_path)
