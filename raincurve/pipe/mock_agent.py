from __future__ import annotations

import json
import logging
from typing import Any

import openai

from .models import InterceptedRequest, MockResponse
from .state import StateStore

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a mock {api} API server. Return realistic JSON responses.

Rules:
- Match the real {api} API response schema
- Realistic IDs with correct prefixes (Stripe: cus_, ch_, pi_, sub_; Twilio: SM, CA; etc.)
- Only include non-null fields to keep responses compact
- For create/update: return the object, set state_writes to {{"<type>": {{"<id>": true}}}}
  (the body is auto-stored — do NOT duplicate it in state_writes)
- For list: return objects from state in the API's list envelope, state_writes {{}}
- For retrieve: return object from state if exists, 404 if not, state_writes {{}}
- For delete: return confirmation, set state_writes to {{"<type>": {{"<id>": null}}}}
- Timestamps: unix integers near 1750000000
- livemode: false

State:
{state}

Respond with ONLY valid JSON, no markdown:
{{"status": <int>, "body": <json>, "state_writes": {{...}}}}"""


class MockAgent:
    def __init__(
        self,
        client: openai.OpenAI,
        model: str = "openai/gpt-5.4-nano",
    ) -> None:
        self._client = client
        self._model = model

    def generate_response(
        self,
        request: InterceptedRequest,
        state: StateStore,
    ) -> MockResponse:
        current_state = state.dump(request.api)
        state_json = json.dumps(current_state, indent=2)
        if len(state_json) > 4000:
            summary: dict[str, Any] = {}
            for rtype, objects in current_state.items():
                summary[rtype] = {
                    "count": len(objects),
                    "recent": objects[-3:] if len(objects) > 3 else objects,
                }
            state_json = json.dumps(summary, indent=2)

        system = _SYSTEM_PROMPT.format(api=request.api, state=state_json)

        user_parts = [f"{request.method} {request.path}"]
        if request.body:
            user_parts.append(f"Body: {request.body[:3000]}")

        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": "\n".join(user_parts)},
            ],
            temperature=0.2,
            max_tokens=2048,
        )

        content = (resp.choices[0].message.content or "{}").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = self._try_repair_json(content)
            if parsed is None:
                log.warning("Pipe: LLM returned invalid JSON: %s", content[:200])
                return MockResponse(
                    status=500,
                    body={"error": {"message": "Mock generation failed: invalid LLM response"}},
                )

        body = parsed.get("body", {})
        mock_resp = MockResponse(
            status=parsed.get("status", 200),
            body=body,
            state_writes=parsed.get("state_writes", {}),
        )

        for resource_type, objects in mock_resp.state_writes.items():
            for obj_id, obj in objects.items():
                if obj is None:
                    state.delete(request.api, resource_type, obj_id)
                elif obj is True:
                    state.put(request.api, resource_type, obj_id, body if isinstance(body, dict) else {})
                else:
                    state.put(request.api, resource_type, obj_id, obj)

        return mock_resp

    @staticmethod
    def _try_repair_json(content: str) -> dict | None:
        """Try to salvage truncated JSON by closing open braces/brackets."""
        opens = 0
        open_sq = 0
        for ch in content:
            if ch == "{":
                opens += 1
            elif ch == "}":
                opens -= 1
            elif ch == "[":
                open_sq += 1
            elif ch == "]":
                open_sq -= 1

        if opens <= 0 and open_sq <= 0:
            return None

        repaired = content.rstrip().rstrip(",")
        repaired += "]" * open_sq + "}" * opens

        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None
