import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zhihuer_wiki import agent_runner


def job(tmp_path):
    (tmp_path / "prompt.md").write_text("Extract actual evidence.", encoding="utf-8")
    (tmp_path / "input.json").write_text('{"text":"`danger` $(not-shell) 中文"}', encoding="utf-8")
    (tmp_path / "schema.json").write_text('{"type":"object"}', encoding="utf-8")
    return {"job_id": "extract-0001", "stage": "extraction",
            "prompt_path": str(tmp_path / "prompt.md"), "input_path": str(tmp_path / "input.json"),
            "schema_path": str(tmp_path / "schema.json"), "response_path": str(tmp_path / "response.json")}


def test_real_adapter_preserves_input_via_stdin_and_records_result(tmp_path, monkeypatch):
    captured = {}
    def fake_run(command, **kwargs):
        captured.update(command=command, **kwargs)
        Path(command[command.index("-o") + 1]).write_text('{"documents":[]}', encoding="utf-8")
        kwargs["stdout"].write('{"type":"turn.completed"}\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    result = agent_runner.run_job(job(tmp_path), executable="codex", progress=lambda _: None)
    assert captured["command"][captured["command"].index("--sandbox") + 1] == "read-only"
    assert captured["command"][-1] == "-"
    assert "$(not-shell)" in captured["input"]
    assert not captured.get("shell")
    assert result["backend"] == "codex-cli"
    assert Path(result["event_path"]).read_text().strip() == '{"type":"turn.completed"}'


def test_failed_agent_cannot_reuse_stale_response(tmp_path, monkeypatch):
    item = job(tmp_path)
    (tmp_path / "response.json").write_text('{"old":true}', encoding="utf-8")
    monkeypatch.setattr(agent_runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(agent_runner.AgentError, match="退出码 1"):
        agent_runner.run_job(item, executable="codex", progress=lambda _: None)
    assert not (tmp_path / "response.json").exists()
    assert len(list(tmp_path.glob("response.previous-*.json"))) == 1


def test_success_exit_without_output_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(agent_runner.AgentError):
        agent_runner.run_job(job(tmp_path), executable="codex", progress=lambda _: None)


def test_malformed_json_is_not_submitted(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        Path(command[command.index("-o") + 1]).write_text("done!", encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    with pytest.raises(agent_runner.AgentError, match="有效 JSON"):
        agent_runner.run_job(job(tmp_path), executable="codex", progress=lambda _: None)


def test_no_codex_explains_manual_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner.shutil, "which", lambda _: None)
    with pytest.raises(agent_runner.AgentError, match="manual"):
        agent_runner.run_job(job(tmp_path), progress=lambda _: None)


def test_timeout_does_not_report_success(tmp_path, monkeypatch):
    def timed_out(*args, **kwargs):
        raise agent_runner.subprocess.TimeoutExpired("codex", 1)
    monkeypatch.setattr(agent_runner.subprocess, "run", timed_out)
    with pytest.raises(agent_runner.AgentError, match="超时"):
        agent_runner.run_job(job(tmp_path), executable="codex", timeout=1, progress=lambda _: None)
