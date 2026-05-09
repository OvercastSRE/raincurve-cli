from __future__ import annotations

import tempfile
from pathlib import Path

from raincurve.models.code_context import CodeContext, ProjectDocs
from raincurve.orchestrator import SandboxOrchestrator


class TestReadProjectDocs:
    def test_reads_readme(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "README.md").write_text("# Hello World", encoding="utf-8")
            orch = SandboxOrchestrator(
                project_dir=tmpdir,
                project_name="test",
                container_name="rc-test",
                network_name="rc-test-net",
                env_overrides={},
                detection_result=_empty_detection(),
                repo_brief=None,
                on_log=lambda m: None,
            )
            docs = orch._read_project_docs()
            assert docs.readme == "# Hello World"

    def test_reads_claude_md(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "CLAUDE.md").write_text("## Build\nnpm start", encoding="utf-8")
            orch = SandboxOrchestrator(
                project_dir=tmpdir,
                project_name="test",
                container_name="rc-test",
                network_name="rc-test-net",
                env_overrides={},
                detection_result=_empty_detection(),
                repo_brief=None,
                on_log=lambda m: None,
            )
            docs = orch._read_project_docs()
            assert docs.claude_md is not None
            assert "npm start" in docs.claude_md

    def test_reads_cursorrules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".cursorrules").write_text("use typescript", encoding="utf-8")
            orch = SandboxOrchestrator(
                project_dir=tmpdir,
                project_name="test",
                container_name="rc-test",
                network_name="rc-test-net",
                env_overrides={},
                detection_result=_empty_detection(),
                repo_brief=None,
                on_log=lambda m: None,
            )
            docs = orch._read_project_docs()
            assert docs.cursor_rules == "use typescript"

    def test_no_docs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = SandboxOrchestrator(
                project_dir=tmpdir,
                project_name="test",
                container_name="rc-test",
                network_name="rc-test-net",
                env_overrides={},
                detection_result=_empty_detection(),
                repo_brief=None,
                on_log=lambda m: None,
            )
            docs = orch._read_project_docs()
            assert docs.readme is None
            assert docs.claude_md is None


class TestFallbackCodeContext:
    def test_builds_from_detection(self):
        from raincurve.stubs.detector import DetectionResult

        detection = DetectionResult()
        detection.import_hits = {
            "stripe": ["src/billing.ts"],
            "sendgrid": ["src/email.ts"],
        }

        orch = SandboxOrchestrator(
            project_dir="/tmp/test",
            project_name="test",
            container_name="rc-test",
            network_name="rc-test-net",
            env_overrides={},
            detection_result=detection,
            repo_brief=None,
            on_log=lambda m: None,
        )

        ctx = orch._build_fallback_code_context(ProjectDocs())
        assert len(ctx.sdk_usages) == 2
        names = {s.service_name for s in ctx.sdk_usages}
        assert "stripe" in names
        assert "sendgrid" in names

    def test_empty_detection(self):
        orch = SandboxOrchestrator(
            project_dir="/tmp/test",
            project_name="test",
            container_name="rc-test",
            network_name="rc-test-net",
            env_overrides={},
            detection_result=_empty_detection(),
            repo_brief=None,
            on_log=lambda m: None,
        )
        ctx = orch._build_fallback_code_context(ProjectDocs())
        assert ctx.sdk_usages == []
        assert ctx.language == "unknown"


class TestCodeContextPersistence:
    def test_persist_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = SandboxOrchestrator(
                project_dir=tmpdir,
                project_name="test",
                container_name="rc-test",
                network_name="rc-test-net",
                env_overrides={},
                detection_result=_empty_detection(),
                repo_brief=None,
                on_log=lambda m: None,
            )
            ctx = CodeContext(
                project_name="test",
                project_dir=tmpdir,
                language="python",
                app_description="A test app",
            )
            orch._persist_code_context(ctx)

            loaded = orch._load_cached_code_context()
            assert loaded is not None
            assert loaded.project_name == "test"
            assert loaded.app_description == "A test app"


def _empty_detection():
    from raincurve.stubs.detector import DetectionResult

    return DetectionResult()
