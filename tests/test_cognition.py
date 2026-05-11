import json
import tempfile
from pathlib import Path

from raincurve.agents.memory import EpisodicMemory, DeclarativeMemory, ProceduralMemory
from raincurve.agents.cognition import AgentCognition


class TestEpisodicMemory:
    def test_record_and_recent(self):
        with tempfile.TemporaryDirectory() as td:
            mem = EpisodicMemory(td, "test_session")
            mem.record("tool_call", {"name": "bash", "is_error": False})
            mem.record("tool_call", {"name": "read_file", "is_error": True})
            recent = mem.recent(5)
            assert len(recent) == 2
            assert recent[0]["name"] == "bash"
            assert recent[1]["is_error"] is True

    def test_summary(self):
        with tempfile.TemporaryDirectory() as td:
            mem = EpisodicMemory(td, "test_session")
            mem.record("tool_call", {"name": "bash", "is_error": False})
            mem.record("tool_call", {"name": "bash", "is_error": True})
            mem.record("reflection", {"text": "test"})
            s = mem.summary()
            assert "3 tool calls" in s
            assert "1 errors" in s
            assert "1 reflections" in s


class TestDeclarativeMemory:
    def test_set_and_get(self):
        with tempfile.TemporaryDirectory() as td:
            mem = DeclarativeMemory(td)
            mem.set("db_type", "postgres")
            assert mem.get("db_type") == "postgres"
            assert mem.get("nonexistent") is None

    def test_extract_from_text(self):
        with tempfile.TemporaryDirectory() as td:
            mem = DeclarativeMemory(td)
            mem.extract_from_text("Fact: This project uses pnpm\nSome other line\nNote: Port 3000 is the default")
            facts = mem.all_facts()
            assert len(facts) == 2
            assert any("pnpm" in v for v in facts.values())
            assert any("3000" in v for v in facts.values())

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            mem1 = DeclarativeMemory(td)
            mem1.set("key", "value")
            mem2 = DeclarativeMemory(td)
            assert mem2.get("key") == "value"


class TestProceduralMemory:
    def test_save_and_load(self):
        proc = ProceduralMemory()
        proc.save("test_stack_xyz", "# Test skill\nPort: 3000")
        content = proc.load("test_stack_xyz")
        assert content is not None
        assert "Port: 3000" in content
        (proc.dir / "test_stack_xyz.md").unlink(missing_ok=True)

    def test_load_missing(self):
        proc = ProceduralMemory()
        assert proc.load("nonexistent_stack_abc") is None


class TestAgentCognition:
    def _make(self, td):
        return AgentCognition(
            episodic=EpisodicMemory(td, "test"),
            declarative=DeclarativeMemory(td),
        )

    def test_extract_thought(self):
        with tempfile.TemporaryDirectory() as td:
            cog = self._make(td)
            cog.extract_reasoning("Thought: I should check docker ps first")
            assert len(cog._thoughts) == 1
            assert "docker ps" in cog._thoughts[0]

    def test_extract_goal(self):
        with tempfile.TemporaryDirectory() as td:
            cog = self._make(td)
            cog.extract_reasoning("Goal: Build the Docker image\nSub-goal: Create Dockerfile")
            assert len(cog._goals) == 2
            assert cog._goals[0]["goal"] == "Build the Docker image"
            assert cog._goals[1]["status"] == "active"

    def test_extract_fact(self):
        with tempfile.TemporaryDirectory() as td:
            cog = self._make(td)
            cog.extract_reasoning("Fact: This project uses yarn as package manager")
            facts = cog.declarative.all_facts()
            assert len(facts) == 1

    def test_should_reflect_at_interval(self):
        with tempfile.TemporaryDirectory() as td:
            cog = self._make(td)
            cog.reflect_every = 4
            for i in range(4):
                cog.record_tool_call("bash", {}, "ok", False)
            assert cog.should_reflect()

    def test_should_not_reflect_between(self):
        with tempfile.TemporaryDirectory() as td:
            cog = self._make(td)
            cog.reflect_every = 8
            for i in range(5):
                cog.record_tool_call("bash", {}, "ok", False)
            assert not cog.should_reflect()

    def test_reflection_prompt_includes_count(self):
        with tempfile.TemporaryDirectory() as td:
            cog = self._make(td)
            cog.tool_call_count = 16
            prompt = cog.build_reflection_prompt()
            assert "16 tool calls" in prompt
            assert "REFLECTION CHECKPOINT" in prompt

    def test_verification_reflection(self):
        with tempfile.TemporaryDirectory() as td:
            cog = self._make(td)
            msg = cog.build_verification_reflection("Health check failed")
            assert "Health check failed" in msg
            assert "WHY" in msg
            assert "Thought:" in msg

    def test_cognitive_context_empty(self):
        with tempfile.TemporaryDirectory() as td:
            cog = self._make(td)
            assert cog.build_cognitive_context() == ""

    def test_cognitive_context_with_goals(self):
        with tempfile.TemporaryDirectory() as td:
            cog = self._make(td)
            cog._goals = [{"goal": "Build image", "status": "active"}]
            cog.record_tool_call("bash", {}, "ok", False)
            ctx = cog.build_cognitive_context()
            assert "Build image" in ctx
            assert "1 tool calls" in ctx
