from __future__ import annotations

from dataclasses import dataclass, field

from raincurve.agents.memory import DeclarativeMemory, EpisodicMemory


REACT_INSTRUCTION = (
    "\n\n## Reasoning Protocol\n"
    "Before each action, briefly state your reasoning. Use these prefixes:\n"
    "- `Thought:` for your reasoning about what to do next\n"
    "- `Goal:` or `Sub-goal:` to declare what you're working toward\n"
    "- `Fact:` or `Note:` to record a project fact worth remembering\n"
    "This helps you stay on track and learn from each step."
)


@dataclass
class AgentCognition:
    episodic: EpisodicMemory
    declarative: DeclarativeMemory
    reflect_every: int = 8
    tool_call_count: int = 0
    _thoughts: list[str] = field(default_factory=list)
    _goals: list[dict] = field(default_factory=list)
    _reflections: list[str] = field(default_factory=list)

    def extract_reasoning(self, text: str) -> None:
        if not text:
            return
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(("Thought:", "Reasoning:")):
                self._thoughts.append(s)
                self.episodic.record("reasoning", {"text": s[:300]})
            elif s.startswith(("Goal:", "Sub-goal:")):
                goal = s.split(":", 1)[1].strip()
                if goal:
                    self._goals.append({"goal": goal, "status": "active"})
                    self.episodic.record("goal", {"text": goal})
            elif s.startswith(("Fact:", "Note:", "Remember:")):
                self.declarative.extract_from_text(s)

    def record_tool_call(self, name: str, args: dict, result_preview: str, is_error: bool) -> None:
        self.tool_call_count += 1
        self.episodic.record("tool_call", {
            "name": name,
            "args_preview": str(args)[:200],
            "result_preview": result_preview[:200],
            "is_error": is_error,
        })

    def should_reflect(self) -> bool:
        return self.tool_call_count > 0 and self.tool_call_count % self.reflect_every == 0

    def build_reflection_prompt(self) -> str:
        recent = self.episodic.recent(self.reflect_every)
        errors = sum(1 for e in recent if e.get("is_error"))
        active = [g["goal"] for g in self._goals if g["status"] == "active"]
        goal_text = f" Active goals: {', '.join(active[-3:])}." if active else ""

        return (
            f"REFLECTION CHECKPOINT ({self.tool_call_count} tool calls, "
            f"{errors} errors in last batch).{goal_text}\n"
            f"Before your next action, briefly reflect:\n"
            f"1. What has worked so far?\n"
            f"2. What has failed or been inefficient?\n"
            f"3. What is your updated plan for the remaining work?\n"
            f"Prefix your reflection with 'Thought:' so it's captured."
        )

    def build_verification_reflection(self, failure_msg: str) -> str:
        return (
            f"Verification failed:\n{failure_msg}\n\n"
            f"Before retrying, reflect on WHY this failed. "
            f"What assumption was wrong? What did you miss? "
            f"Prefix your analysis with 'Thought:' then fix and try again."
        )

    def build_cognitive_context(self) -> str:
        parts = []
        active = [g["goal"] for g in self._goals if g["status"] == "active"]
        if active:
            parts.append(f"Goals: {', '.join(active[-5:])}")
        if self.tool_call_count > 0:
            parts.append(self.episodic.summary())
        if self._reflections:
            parts.append(f"Last reflection: {self._reflections[-1][:200]}")
        facts = self.declarative.all_facts()
        if facts:
            fact_strs = [f"{v}" for v in list(facts.values())[:5]]
            parts.append(f"Facts: {'; '.join(fact_strs)}")
        return " | ".join(parts) if parts else ""
