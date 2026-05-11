from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

from raincurve.agents.cognition import AgentCognition, REACT_INSTRUCTION
from raincurve.agents.error_classifier import classify as classify_error
from raincurve.agents.memory import EpisodicMemory, DeclarativeMemory
from raincurve.agents.tool_guardrails import ToolGuardrails, _looks_like_error
from raincurve.config import load_global_config


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class AgentResult:
    success: bool
    output: dict[str, Any] | None = None
    failure_reason: str | None = None
    duration_s: float = 0.0
    tool_call_count: int = 0


def _truncate(text: str, head: int = 6000, tail: int = 6000) -> str:
    if not text or len(text) <= head + tail + 32:
        return text
    elided = len(text) - head - tail
    return f"{text[:head]}\n\n[... {elided} bytes elided ...]\n\n{text[-tail:]}"


def _exec_bash(cmd: str, cwd: str, timeout_s: int = 120, env: dict[str, str] | None = None) -> CommandResult:
    import os
    import tempfile
    full_env = dict(os.environ)
    if env:
        full_env.update(env)

    # All platforms have command-line length limits. If the command is long,
    # write it to a temp script file and execute that instead.
    tmp_file = None
    if len(cmd) > 8000:
        try:
            is_python = cmd.strip().startswith(("import ", "from ", "#!", "print(", "def ", "class "))
            suffix = ".py" if is_python else ".sh"
            tmp_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=suffix, dir=cwd, delete=False, encoding="utf-8",
            )
            tmp_file.write(cmd)
            tmp_file.close()
            cmd = f'python "{tmp_file.name}"' if is_python else f'bash "{tmp_file.name}"'
        except Exception:
            pass

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=full_env,
            encoding="utf-8",
            errors="replace",
        )
        return CommandResult(
            exit_code=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )
    except subprocess.TimeoutExpired:
        return CommandResult(exit_code=124, stdout="", stderr=f"Command timed out after {timeout_s}s")
    except PermissionError:
        return CommandResult(exit_code=1, stdout="", stderr="Command too long for Windows shell. Try writing to a file first.")
    finally:
        if tmp_file and os.path.exists(tmp_file.name):
            try:
                os.unlink(tmp_file.name)
            except Exception:
                pass


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_KEY = os.environ.get("RAINCURVE_OPENROUTER_KEY", "")


def _get_llm_client(provider: str | None = None, model_override: str | None = None):
    cfg = load_global_config()
    prov = provider or cfg.llm.provider or "openrouter"

    if prov == "openrouter":
        import openai
        api_key = (
            cfg.llm.openrouter_api_key
            or os.environ.get("OPENROUTER_API_KEY")
            or OPENROUTER_DEFAULT_KEY
        )
        generic_model = cfg.llm.model if cfg.llm.model and "/" in cfg.llm.model else None
        model = model_override or cfg.llm.openrouter_model or generic_model or "moonshotai/kimi-k2.5"
        client = openai.OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "https://raincurve.dev",
                "X-OpenRouter-Title": "raincurve",
            },
        )
        return "openai", model, client

    api_key = cfg.llm.api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("No LLM API key configured. Run `raincurve init` or set ANTHROPIC_API_KEY.")

    if prov == "anthropic":
        import anthropic
        model = model_override or cfg.llm.model or "claude-sonnet-4-5-20250929"
        return "anthropic", model, anthropic.Anthropic(api_key=api_key, timeout=120.0)
    elif prov == "openai":
        import openai
        model = model_override or cfg.llm.model or "gpt-4o"
        return "openai", model, openai.OpenAI(api_key=api_key)
    else:
        raise ValueError(f"Unknown LLM provider: {prov}")


class BaseAgent:
    MAX_TOOL_CALLS: int = 1000
    MAX_WALLCLOCK_S: int = 3600
    MAX_TOKENS: int = 8192

    def __init__(
        self,
        project_dir: str,
        on_log: Callable[[str], None] | None = None,
        max_tool_calls: int | None = None,
        max_wallclock_s: int | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.on_log = on_log or (lambda s: None)
        if max_tool_calls is not None:
            self.MAX_TOOL_CALLS = max_tool_calls
        if max_wallclock_s is not None:
            self.MAX_WALLCLOCK_S = max_wallclock_s

    COMPACT_AFTER_MESSAGES: int = 40

    def _log(self, msg: str) -> None:
        self.on_log(msg)

    @staticmethod
    def _compact_messages(messages: list[dict]) -> list[dict]:
        """Trim old tool results to reduce token usage while preserving quality.

        Strategy: keep the first message (initial prompt) and last 20 messages
        fully intact. In older messages, only truncate large SUCCESSFUL tool
        results (the agent already acted on them). Error outputs and short
        results are kept in full so the agent can reference past failures."""
        if len(messages) <= 40:
            return messages
        keep_tail = 20
        head = messages[:1]
        middle = messages[1:-keep_tail]
        tail = messages[-keep_tail:]
        compacted = []
        for msg in middle:
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                new_content = []
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        text = block.get("content", "")
                        if isinstance(text, str) and len(text) > 800:
                            looks_like_error = (
                                "error" in text.lower()[:500]
                                or "failed" in text.lower()[:500]
                                or "exit_code=1" in text[:100]
                                or "STDERR" in text[:500]
                                or "traceback" in text.lower()[:500]
                            )
                            if not looks_like_error:
                                block = {
                                    **block,
                                    "content": text[:400] + "\n[...truncated, was "
                                    + str(len(text)) + " chars]",
                                }
                    new_content.append(block)
                compacted.append({**msg, "content": new_content})
            else:
                compacted.append(msg)
        return head + compacted + tail

    MODEL_OVERRIDE: str | None = None

    def _run_loop(
        self,
        system_prompt: str,
        initial_message: str,
        tools: list[dict],
        tool_handler: Callable[[str, dict], Any],
    ) -> AgentResult:
        provider_name, model, client = _get_llm_client(model_override=self.MODEL_OVERRIDE)
        start = time.time()

        session_id = time.strftime("%Y%m%d_%H%M%S")
        self._cognition = AgentCognition(
            episodic=EpisodicMemory(self.project_dir, session_id),
            declarative=DeclarativeMemory(self.project_dir),
        )
        system_prompt = system_prompt + REACT_INSTRUCTION

        if provider_name == "anthropic":
            return self._run_anthropic_loop(
                client, model, system_prompt, initial_message, tools, tool_handler, start
            )
        else:
            return self._run_openai_loop(
                client, model, system_prompt, initial_message, tools, tool_handler, start
            )

    def _run_anthropic_loop(
        self,
        client: Any,
        model: str,
        system_prompt: str,
        initial_message: str,
        tools: list[dict],
        tool_handler: Callable[[str, dict], Any],
        start: float,
    ) -> AgentResult:
        messages: list[dict] = [{"role": "user", "content": initial_message}]
        tool_call_count = 0
        done_result: dict | None = None
        guardrails = ToolGuardrails()
        compressed_this_iteration = False
        api_retries = 0

        while True:
            elapsed = time.time() - start
            if elapsed > self.MAX_WALLCLOCK_S:
                return AgentResult(
                    success=False,
                    failure_reason=f"Wallclock limit exceeded ({self.MAX_WALLCLOCK_S}s)",
                    duration_s=elapsed,
                    tool_call_count=tool_call_count,
                )
            if tool_call_count >= self.MAX_TOOL_CALLS:
                return AgentResult(
                    success=False,
                    failure_reason=f"Tool call limit exceeded ({self.MAX_TOOL_CALLS})",
                    duration_s=elapsed,
                    tool_call_count=tool_call_count,
                )

            if len(messages) > self.COMPACT_AFTER_MESSAGES:
                messages = self._compact_messages(messages)

            try:
                effective_prompt = system_prompt
                cog_ctx = self._cognition.build_cognitive_context()
                if cog_ctx:
                    effective_prompt = system_prompt + f"\n\n## Session State\n{cog_ctx}"
                system_blocks = [
                    {
                        "type": "text",
                        "text": effective_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                cached_tools = [
                    {**t, "cache_control": {"type": "ephemeral"}} if i == len(tools) - 1 else t
                    for i, t in enumerate(tools)
                ]
                resp = client.messages.create(
                    model=model,
                    max_tokens=self.MAX_TOKENS,
                    system=system_blocks,
                    messages=messages,
                    tools=cached_tools,
                )
                compressed_this_iteration = False
                api_retries = 0
            except Exception as api_err:
                classified = classify_error(api_err)
                api_retries += 1
                self._log(f"API error ({classified.category}): {classified.message[:150]}")
                if api_retries > 5:
                    return AgentResult(
                        success=False,
                        failure_reason=f"API error after {api_retries} retries ({classified.category}): {classified.message[:200]}",
                        duration_s=time.time() - start,
                        tool_call_count=tool_call_count,
                    )
                if classified.should_compress and not compressed_this_iteration:
                    messages = self._compact_messages(messages)
                    compressed_this_iteration = True
                    continue
                if classified.retryable:
                    if classified.wait_seconds:
                        time.sleep(classified.wait_seconds)
                    continue
                return AgentResult(
                    success=False,
                    failure_reason=f"API error ({classified.category}): {classified.message[:200]}",
                    duration_s=time.time() - start,
                    tool_call_count=tool_call_count,
                )

            assistant_blocks: list[dict] = []
            tool_results: list[dict] = []

            for block in resp.content:
                if block.type == "text":
                    self._log(block.text)
                    assistant_blocks.append({"type": "text", "text": block.text})
                    self._cognition.extract_reasoning(block.text)
                elif block.type == "tool_use":
                    tool_call_count += 1
                    assistant_blocks.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

                    if block.name == "done":
                        done_result = block.input
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Acknowledged.",
                        })
                    else:
                        result = tool_handler(block.name, block.input)
                        result_str = result if isinstance(result, str) else json.dumps(result)
                        result_str = _truncate(result_str)
                        self._cognition.record_tool_call(
                            block.name, block.input, result_str[:200],
                            _looks_like_error(result_str),
                        )
                        guardrail_msg = guardrails.check(block.name, block.input, result_str)
                        if guardrail_msg:
                            self._log(f"Guardrail: {guardrail_msg[:100]}")
                            if guardrail_msg.startswith("BLOCKED:"):
                                result_str = guardrail_msg
                            else:
                                result_str = result_str + "\n\n" + guardrail_msg
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        })

            messages.append({"role": "assistant", "content": assistant_blocks})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if self._cognition.should_reflect():
                messages.append({
                    "role": "user",
                    "content": self._cognition.build_reflection_prompt(),
                })

            if done_result is not None:
                verification = self._verify_done(done_result)
                if verification is None:
                    return AgentResult(
                        success=True,
                        output=done_result,
                        duration_s=time.time() - start,
                        tool_call_count=tool_call_count,
                    )
                else:
                    self._log(f"Verification failed: {verification}")
                    done_result = None
                    messages.append({
                        "role": "user",
                        "content": self._cognition.build_verification_reflection(verification),
                    })

            if resp.stop_reason == "end_turn" and done_result is None:
                messages.append({
                    "role": "user",
                    "content": "You stopped without calling the `done` tool. Please continue working or call `done` when finished.",
                })

    def _run_openai_loop(
        self,
        client: Any,
        model: str,
        system_prompt: str,
        initial_message: str,
        tools: list[dict],
        tool_handler: Callable[[str, dict], Any],
        start: float,
    ) -> AgentResult:
        oai_tools = _anthropic_tools_to_openai(tools)
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": initial_message},
        ]
        tool_call_count = 0
        done_result: dict | None = None
        guardrails = ToolGuardrails()
        compressed_this_iteration = False
        api_retries = 0

        while True:
            elapsed = time.time() - start
            if elapsed > self.MAX_WALLCLOCK_S:
                return AgentResult(
                    success=False,
                    failure_reason=f"Wallclock limit exceeded ({self.MAX_WALLCLOCK_S}s)",
                    duration_s=elapsed,
                    tool_call_count=tool_call_count,
                )
            if tool_call_count >= self.MAX_TOOL_CALLS:
                return AgentResult(
                    success=False,
                    failure_reason=f"Tool call limit exceeded ({self.MAX_TOOL_CALLS})",
                    duration_s=elapsed,
                    tool_call_count=tool_call_count,
                )

            if len(messages) > self.COMPACT_AFTER_MESSAGES:
                messages = self._compact_messages(messages)

            cog_ctx = self._cognition.build_cognitive_context()
            if cog_ctx:
                messages[0] = {"role": "system", "content": system_prompt + f"\n\n## Session State\n{cog_ctx}"}

            create_kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
            }
            if oai_tools:
                create_kwargs["tools"] = oai_tools

            if "/" in model:
                create_kwargs["extra_body"] = {
                    "provider": {
                        "order": ["google-vertex"],
                        "allow_fallbacks": True,
                    },
                    "reasoning": {
                        "effort": "high",
                    },
                }

            try:
                resp = client.chat.completions.create(**create_kwargs)
                compressed_this_iteration = False
                api_retries = 0
            except Exception as api_err:
                classified = classify_error(api_err)
                api_retries += 1
                self._log(f"API error ({classified.category}): {classified.message[:150]}")
                if api_retries > 5:
                    return AgentResult(
                        success=False,
                        failure_reason=f"API error after {api_retries} retries ({classified.category}): {classified.message[:200]}",
                        duration_s=time.time() - start,
                        tool_call_count=tool_call_count,
                    )
                if classified.should_compress and not compressed_this_iteration:
                    messages = self._compact_messages(messages)
                    compressed_this_iteration = True
                    continue
                if classified.retryable:
                    if classified.wait_seconds:
                        time.sleep(classified.wait_seconds)
                    continue
                return AgentResult(
                    success=False,
                    failure_reason=f"API error ({classified.category}): {classified.message[:200]}",
                    duration_s=time.time() - start,
                    tool_call_count=tool_call_count,
                )

            choice = resp.choices[0]
            msg = choice.message
            if msg.content:
                self._cognition.extract_reasoning(msg.content)
            msg_dict = msg.model_dump()
            for reasoning_field in ("reasoning_content", "reasoning"):
                val = getattr(msg, reasoning_field, None)
                if val and reasoning_field not in msg_dict:
                    msg_dict[reasoning_field] = val
            messages.append(msg_dict)

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_call_count += 1

                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError as e:
                        self._log(f"Malformed JSON in {tc.function.name} args: {e}")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": (
                                f"ERROR: Your tool call had invalid JSON arguments: {e}. "
                                f"Please retry with valid JSON."
                            ),
                        })
                        continue

                    if tc.function.name == "done":
                        done_result = args
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "Acknowledged.",
                        })
                    else:
                        result = tool_handler(tc.function.name, args)
                        result_str = result if isinstance(result, str) else json.dumps(result)
                        result_str = _truncate(result_str)
                        self._cognition.record_tool_call(
                            tc.function.name, args, result_str[:200],
                            _looks_like_error(result_str),
                        )
                        guardrail_msg = guardrails.check(tc.function.name, args, result_str)
                        if guardrail_msg:
                            self._log(f"Guardrail: {guardrail_msg[:100]}")
                            if guardrail_msg.startswith("BLOCKED:"):
                                result_str = guardrail_msg
                            else:
                                result_str = result_str + "\n\n" + guardrail_msg
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_str,
                        })

                if self._cognition.should_reflect():
                    messages.append({
                        "role": "user",
                        "content": self._cognition.build_reflection_prompt(),
                    })

                if done_result is not None:
                    verification = self._verify_done(done_result)
                    if verification is None:
                        return AgentResult(
                            success=True,
                            output=done_result,
                            duration_s=time.time() - start,
                            tool_call_count=tool_call_count,
                        )
                    else:
                        self._log(f"Verification failed: {verification}")
                        done_result = None
                        messages.append({
                            "role": "user",
                            "content": self._cognition.build_verification_reflection(verification),
                        })
            elif choice.finish_reason == "stop":
                if msg.content:
                    self._log(msg.content)
                messages.append({
                    "role": "user",
                    "content": "You stopped without calling the `done` tool. Please continue working or call `done` when finished.",
                })

    def _verify_done(self, done_output: dict) -> str | None:
        return None


def _anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    oai = []
    for t in tools:
        oai.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        })
    return oai
