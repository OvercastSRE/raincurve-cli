"""
Browser container script — runs INSIDE a Docker container on the sandbox network.

Image: rc-playwright:local (built from python:3.12-slim + playwright)

Two responsibilities:
  1. SCENARIO HTTP SERVER — listens on port 9000 for POST /scenario
     with {goal, login?, max_steps, repo_context?}. Uses accessibility tree
     extraction + Claude tool use for fast, selector-based automation.
  2. HEALTH ENDPOINT — GET /health returns {ok: true, url: current_page_url}

Stdout protocol:
  BOOT:<info>     startup message
  READY:<wxh>     Chromium up
  URL:<url>       page navigated
  STEP:<json>     persona action
  WARN:<msg>      non-fatal warning
  ERROR:<msg>     fatal error
  FRAME:<b64png>  screenshot for live viewer

Env vars:
  START_URL          required
  VIEWPORT_WIDTH     1280
  VIEWPORT_HEIGHT    800
  SCENARIO_PORT      9000
  ANTHROPIC_API_KEY  required
  PERSONA_MODEL      claude-sonnet-4-6
  PERSONA_MAX_STEPS  60
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import traceback
from typing import Any, Dict, List, Optional


def _emit(line: str) -> None:
    sys.stdout.write(line)
    if not line.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _emit_step(payload: Dict[str, Any]) -> None:
    _emit(f"STEP:{json.dumps(payload, default=str)}")


async def _take_screenshot_b64(page: Any) -> str:
    png = await page.screenshot(type="png", full_page=False, timeout=5_000)
    return base64.b64encode(png).decode("ascii")


# ---------------------------------------------------------------------------
# Accessibility tree extraction
# ---------------------------------------------------------------------------

async def _get_accessibility_tree(page: Any) -> tuple[str, dict[int, Any]]:
    """Extract the page's accessibility tree and return a numbered text
    representation plus an index mapping element IDs to Playwright locators."""

    # Get all interactive and visible elements via JS
    elements = await page.evaluate("""() => {
        const results = [];
        const seen = new Set();

        function getSelector(el) {
            if (el.id) return '#' + CSS.escape(el.id);
            if (el.name && el.tagName) {
                const sel = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
                if (document.querySelectorAll(sel).length === 1) return sel;
            }
            // Build a unique path
            const parts = [];
            let current = el;
            while (current && current !== document.body) {
                let sel = current.tagName.toLowerCase();
                if (current.id) {
                    sel = '#' + CSS.escape(current.id);
                    parts.unshift(sel);
                    break;
                }
                const parent = current.parentElement;
                if (parent) {
                    const siblings = Array.from(parent.children).filter(
                        c => c.tagName === current.tagName
                    );
                    if (siblings.length > 1) {
                        const idx = siblings.indexOf(current) + 1;
                        sel += ':nth-of-type(' + idx + ')';
                    }
                }
                parts.unshift(sel);
                current = parent;
            }
            return parts.join(' > ');
        }

        function getText(el) {
            // Get direct text content, not children's
            let text = '';
            for (const node of el.childNodes) {
                if (node.nodeType === 3) text += node.textContent.trim();
            }
            // Fallback to aria-label, title, placeholder, alt
            return text
                || el.getAttribute('aria-label')
                || el.getAttribute('title')
                || el.getAttribute('placeholder')
                || el.getAttribute('alt')
                || el.innerText?.slice(0, 80)
                || '';
        }

        function isVisible(el) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            if (parseFloat(style.opacity) === 0) return false;
            return true;
        }

        // Interactive elements
        const selectors = [
            'a[href]', 'button', 'input', 'select', 'textarea',
            '[role="button"]', '[role="link"]', '[role="tab"]',
            '[role="menuitem"]', '[role="option"]', '[role="checkbox"]',
            '[role="radio"]', '[role="switch"]', '[role="combobox"]',
            '[role="searchbox"]', '[role="slider"]',
            '[onclick]', '[tabindex]',
        ];

        for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
                if (seen.has(el) || !isVisible(el)) continue;
                seen.add(el);

                const tag = el.tagName.toLowerCase();
                const role = el.getAttribute('role') || '';
                const type = el.getAttribute('type') || '';
                const text = getText(el).slice(0, 100).trim();
                const value = el.value || '';
                const href = el.getAttribute('href') || '';
                const checked = el.checked;
                const disabled = el.disabled;
                const cssSelector = getSelector(el);

                results.push({
                    tag, role, type, text, value: value.slice(0, 50),
                    href: href.slice(0, 100), checked, disabled,
                    selector: cssSelector,
                });
            }
        }

        // Also get headings and major text for context
        for (const el of document.querySelectorAll('h1, h2, h3, [role="heading"]')) {
            if (seen.has(el) || !isVisible(el)) continue;
            seen.add(el);
            results.push({
                tag: el.tagName.toLowerCase(), role: 'heading',
                type: '', text: el.innerText?.slice(0, 100) || '',
                value: '', href: '', checked: false, disabled: false,
                selector: getSelector(el),
            });
        }

        return results;
    }""")

    # Build numbered text representation
    lines: list[str] = []
    index: dict[int, Any] = {}

    lines.append(f"Current URL: {page.url}")
    lines.append(f"Page title: {await page.title()}")
    lines.append("")

    for i, el in enumerate(elements):
        idx = i + 1
        index[idx] = el

        tag = el["tag"]
        role = el.get("role", "")
        etype = el.get("type", "")
        text = el.get("text", "").strip()
        value = el.get("value", "")
        href = el.get("href", "")
        checked = el.get("checked", False)
        disabled = el.get("disabled", False)

        # Build description
        desc_parts = []
        if tag == "a":
            desc_parts.append("link")
        elif tag == "button" or role == "button":
            desc_parts.append("button")
        elif tag == "input":
            desc_parts.append(f"input[{etype or 'text'}]")
        elif tag == "select":
            desc_parts.append("dropdown")
        elif tag == "textarea":
            desc_parts.append("textarea")
        elif role == "menuitem":
            desc_parts.append("menuitem")
        elif role == "tab":
            desc_parts.append("tab")
        elif role == "heading":
            desc_parts.append(f"{tag}")
        else:
            desc_parts.append(role or tag)

        if text:
            desc_parts.append(f'"{text}"')
        if value:
            desc_parts.append(f'value="{value}"')
        if href and tag == "a":
            desc_parts.append(f'→ {href}')
        if checked:
            desc_parts.append("[checked]")
        if disabled:
            desc_parts.append("[disabled]")

        lines.append(f"[{idx}] {' '.join(desc_parts)}")

    return "\n".join(lines), index


# ---------------------------------------------------------------------------
# Scenario runner (accessibility tree based)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "click",
        "description": "Click an element by its index number from the accessibility tree.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element": {"type": "integer", "description": "Element index to click"},
            },
            "required": ["element"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into an input field. Clicks the element first to focus it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element": {"type": "integer", "description": "Element index to type into"},
                "text": {"type": "string", "description": "Text to type"},
                "clear_first": {"type": "boolean", "description": "Clear existing value before typing. Default true."},
            },
            "required": ["element", "text"],
        },
    },
    {
        "name": "select_option",
        "description": "Select an option from a dropdown by its visible text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element": {"type": "integer", "description": "Dropdown element index"},
                "value": {"type": "string", "description": "Option text to select"},
            },
            "required": ["element", "value"],
        },
    },
    {
        "name": "navigate",
        "description": "Navigate to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page up or down.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
            },
            "required": ["direction"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key (Enter, Escape, Tab, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key to press (e.g. Enter, Escape, Tab)"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "wait",
        "description": "Wait for a short time (e.g. for animations or loading).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ms": {"type": "integer", "description": "Milliseconds to wait. Default 1000."},
            },
        },
    },
    {
        "name": "done",
        "description": "Call when you have achieved the goal or determined it cannot be done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["succeeded", "failed", "stuck"]},
                "summary": {"type": "string", "description": "What you did and what happened."},
            },
            "required": ["verdict", "summary"],
        },
    },
]


async def _execute_action(page: Any, name: str, args: dict, index: dict[int, Any]) -> str:
    """Execute a tool action and return a result string."""

    if name == "click":
        el = index.get(args["element"])
        if not el:
            return f"Element [{args['element']}] not found"
        selector = el["selector"]
        try:
            await page.click(selector, timeout=5_000)
            await asyncio.sleep(0.5)
            return f"Clicked [{args['element']}] ({el.get('text', '')[:40]})"
        except Exception as e:
            return f"Click failed: {e}"

    elif name == "type_text":
        el = index.get(args["element"])
        if not el:
            return f"Element [{args['element']}] not found"
        selector = el["selector"]
        clear = args.get("clear_first", True)
        try:
            if clear:
                await page.fill(selector, "", timeout=3_000)
            await page.fill(selector, args["text"], timeout=5_000)
            return f"Typed into [{args['element']}]"
        except Exception:
            try:
                await page.click(selector, timeout=3_000)
                if clear:
                    await page.keyboard.press("Control+a")
                await page.keyboard.type(args["text"], delay=30)
                return f"Typed into [{args['element']}] (via keyboard)"
            except Exception as e:
                return f"Type failed: {e}"

    elif name == "select_option":
        el = index.get(args["element"])
        if not el:
            return f"Element [{args['element']}] not found"
        selector = el["selector"]
        try:
            await page.select_option(selector, label=args["value"], timeout=5_000)
            return f"Selected '{args['value']}' in [{args['element']}]"
        except Exception as e:
            return f"Select failed: {e}"

    elif name == "navigate":
        try:
            await page.goto(args["url"], wait_until="domcontentloaded", timeout=15_000)
            return f"Navigated to {page.url}"
        except Exception as e:
            return f"Navigation failed: {e}"

    elif name == "scroll":
        direction = args.get("direction", "down")
        delta = 600 if direction == "down" else -600
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(0.3)
        return f"Scrolled {direction}"

    elif name == "press_key":
        key = args["key"]
        await page.keyboard.press(key)
        await asyncio.sleep(0.3)
        return f"Pressed {key}"

    elif name == "wait":
        ms = args.get("ms", 1000)
        await asyncio.sleep(ms / 1000)
        return f"Waited {ms}ms"

    return f"Unknown action: {name}"


async def run_scenario(
    page: Any,
    goal: str,
    *,
    login: Optional[Dict[str, Any]] = None,
    max_steps: int = 60,
    model: str = "claude-sonnet-4-6",
    api_key: str = "",
    repo_context: str = "",
    viewport_width: int = 1280,
    viewport_height: int = 800,
) -> Dict[str, Any]:
    if not api_key:
        return {"verdict": "errored", "summary": "no anthropic api key", "steps": 0}

    try:
        from anthropic import Anthropic
    except ImportError:
        return {"verdict": "errored", "summary": "anthropic SDK not installed", "steps": 0}

    client = Anthropic(api_key=api_key)

    # Auto-login if credentials provided
    auto_authed = False
    if login:
        try:
            await _persona_login(page, login)
            auto_authed = True
            _emit_step({
                "step": 0, "action": "login",
                "args": {"email": login.get("email"), "path": login.get("login_path")},
                "result": "auto-authed",
            })
        except Exception as exc:
            _emit_step({"step": 0, "action": "login", "args": {}, "error": str(exc)[:200]})

    # Initial screenshot for viewer
    screenshot_b64 = await _take_screenshot_b64(page)
    _emit(f"FRAME:{screenshot_b64}")

    # Get initial accessibility tree
    tree_text, element_index = await _get_accessibility_tree(page)

    system_prompt = f"""You are a browser automation agent. You interact with a live web application \
by reading its accessibility tree and calling tools to click, type, navigate, and scroll.

You will receive the page's interactive elements as a numbered list like:
[1] button "Login"
[2] input[text] "Search" value="..."
[3] link "Dashboard" → /dashboard

Use the element numbers to interact. After each action, you'll get an updated tree.

Rules:
- Be efficient. Achieve the goal in as few steps as possible.
- If an element disappears after clicking, it's normal — check the new tree.
- Use `done` when the goal is achieved or you're stuck.
- Max {max_steps} steps.
- If auto_authed is true, you're already logged in — skip login flows.
"""

    user_parts: List[str] = []
    if repo_context:
        user_parts.append(repo_context)
        user_parts.append("")
    user_parts.append(f"Goal: {goal}")
    user_parts.append(f"auto_authed: {auto_authed}")
    user_parts.append("")
    user_parts.append("Current page state:")
    user_parts.append(tree_text)

    messages: list[dict] = [{"role": "user", "content": "\n".join(user_parts)}]

    step = 0
    verdict = "stuck"
    summary = "max steps reached"

    while step < max_steps:
        try:
            resp = await asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=1024,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as exc:
            _emit(f"WARN:LLM call failed: {exc}")
            verdict = "errored"
            summary = f"LLM call failed: {exc}"
            break

        # Parse response
        tool_uses = []
        text_parts = []
        assistant_content = []

        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                t = getattr(block, "text", "") or ""
                if t.strip():
                    text_parts.append(t.strip())
                assistant_content.append({"type": "text", "text": t})
            elif btype == "tool_use":
                tool_uses.append(block)
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        # No tool calls = done
        if not tool_uses:
            verdict = "succeeded"
            summary = text_parts[-1] if text_parts else "completed"
            summary = summary[:300]
            break

        messages.append({"role": "assistant", "content": assistant_content})

        # Execute each tool call
        tool_results = []
        for tu in tool_uses:
            step += 1
            name = tu.name
            args = tu.input or {}
            thought = text_parts[0] if text_parts else None
            text_parts = []

            # Handle "done" tool
            if name == "done":
                verdict = args.get("verdict", "succeeded")
                summary = args.get("summary", "completed")[:300]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "Acknowledged.",
                })
                _emit_step({
                    "step": step, "action": "done",
                    "args": args,
                    "thought": thought,
                    "verdict": verdict, "summary": summary,
                })
                # Take final screenshot for viewer
                try:
                    ss = await _take_screenshot_b64(page)
                    _emit(f"FRAME:{ss}")
                except Exception:
                    pass
                _emit_step({"step": step, "action": "scenario_end", "verdict": verdict, "summary": summary})
                return {"verdict": verdict, "summary": summary, "steps": step}

            # Execute action
            try:
                result = await _execute_action(page, name, args, element_index)
                _emit_step({
                    "step": step, "action": name,
                    "args": {k: v for k, v in args.items()},
                    "result": str(result)[:300],
                    "thought": thought,
                })
            except Exception as exc:
                result = f"error: {exc}"
                _emit_step({
                    "step": step, "action": name,
                    "args": {k: v for k, v in args.items()},
                    "error": str(exc)[:300],
                    "thought": thought,
                })

            # Screenshot for viewer
            try:
                ss = await _take_screenshot_b64(page)
                _emit(f"FRAME:{ss}")
            except Exception:
                pass

            # Get updated accessibility tree
            await asyncio.sleep(0.3)
            tree_text, element_index = await _get_accessibility_tree(page)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": f"{result}\n\nUpdated page state:\n{tree_text}",
            })

        messages.append({"role": "user", "content": tool_results})

        if getattr(resp, "stop_reason", None) == "end_turn" and verdict == "stuck":
            verdict = "succeeded"
            summary = text_parts[-1] if text_parts else "completed"
            summary = summary[:300]
            break

    _emit_step({"step": step, "action": "scenario_end", "verdict": verdict, "summary": summary})
    return {"verdict": verdict, "summary": summary, "steps": step}


# ---------------------------------------------------------------------------
# Playwright-based login (no LLM needed)
# ---------------------------------------------------------------------------

async def _persona_login(page: Any, login: Dict[str, Any]) -> None:
    email = login.get("email") or login.get("username") or ""
    password = login.get("password") or ""
    login_path = login.get("login_path") or "/login"
    if not email or not password:
        raise RuntimeError("login: email or password missing")

    if not login_path.startswith("/"):
        login_path = "/" + login_path

    base_url = page.url.rsplit("/", 1)[0] if page.url else ""
    target = base_url + login_path if base_url.startswith("http") else login_path
    await page.goto(target, wait_until="domcontentloaded", timeout=15_000)

    email_selectors = [
        "input[name=email]", "input[type=email]",
        "input[name=username]", "input[id=username]", "input[name=identifier]",
    ]
    pw_selectors = [
        "input[name=password]", "input[type=password]", "input[id=password]",
    ]
    submit_selectors = [
        "button[type=submit]", "input[type=submit]",
        "text=Sign In", "text=Log In", "text=Login", "text=Continue",
    ]

    filled_email = False
    for sel in email_selectors:
        try:
            await page.fill(sel, email, timeout=2_000)
            filled_email = True
            break
        except Exception:
            continue
    if not filled_email:
        raise RuntimeError("login: no email/username field found")

    filled_pw = False
    for sel in pw_selectors:
        try:
            await page.fill(sel, password, timeout=2_000)
            filled_pw = True
            break
        except Exception:
            continue
    if not filled_pw:
        raise RuntimeError("login: no password field found")

    submitted = False
    for sel in submit_selectors:
        try:
            async with page.expect_navigation(timeout=8_000):
                await page.click(sel, timeout=2_000)
            submitted = True
            break
        except Exception:
            try:
                await page.click(sel, timeout=2_000)
                submitted = True
                break
            except Exception:
                continue
    if not submitted:
        raise RuntimeError("login: could not submit the form")


# ---------------------------------------------------------------------------
# Main + HTTP server
# ---------------------------------------------------------------------------

async def main() -> int:
    start_url = os.environ.get("START_URL", "").strip()
    if not start_url:
        _emit("ERROR:START_URL env var is required")
        return 2

    vw = int(os.environ.get("VIEWPORT_WIDTH", "1280"))
    vh = int(os.environ.get("VIEWPORT_HEIGHT", "800"))
    scenario_port = int(os.environ.get("SCENARIO_PORT", "9000"))
    persona_model = os.environ.get("PERSONA_MODEL", "claude-sonnet-4-6")
    persona_max_steps = int(os.environ.get("PERSONA_MAX_STEPS", "60"))
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        _emit(f"ERROR:playwright not available: {exc}")
        return 3

    _emit(f"BOOT:browser-view starting; start_url={start_url} viewport={vw}x{vh} scenario_port={scenario_port}")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            _emit(f"ERROR:chromium launch failed: {exc}")
            traceback.print_exc(file=sys.stderr)
            return 4

        context = await browser.new_context(
            viewport={"width": vw, "height": vh},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)
            _emit(f"READY:{vw}x{vh}")
            _emit(f"URL:{page.url}")
        except Exception as exc:
            _emit(f"ERROR:initial navigation failed: {exc}")

        scenario_lock = asyncio.Lock()

        async def scenario_server() -> None:
            try:
                from aiohttp import web
            except ImportError:
                _emit("WARN:aiohttp not installed; scenario endpoint disabled")
                return

            async def handle_scenario(request: "web.Request") -> "web.Response":
                if scenario_lock.locked():
                    return web.json_response(
                        {"error": "another scenario is running"}, status=409,
                    )
                async with scenario_lock:
                    try:
                        body = await request.json()
                    except Exception as exc:
                        return web.json_response({"error": f"bad json: {exc}"}, status=400)
                    goal = (body.get("goal") or "").strip()
                    if not goal:
                        return web.json_response({"error": "goal required"}, status=400)
                    login = body.get("login") or None
                    max_steps = int(body.get("max_steps") or persona_max_steps)
                    repo_context = (body.get("repo_context") or "").strip()
                    result = await run_scenario(
                        page, goal,
                        login=login,
                        max_steps=max_steps,
                        model=persona_model,
                        api_key=api_key,
                        repo_context=repo_context,
                        viewport_width=vw,
                        viewport_height=vh,
                    )
                    return web.json_response(result)

            async def handle_health(_request: "web.Request") -> "web.Response":
                return web.json_response({"ok": True, "url": page.url})

            app = web.Application()
            app.router.add_post("/scenario", handle_scenario)
            app.router.add_get("/health", handle_health)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host="0.0.0.0", port=scenario_port)
            await site.start()
            _emit(f"BOOT:scenario server listening on :{scenario_port}")
            while True:
                await asyncio.sleep(3600)

        await scenario_server()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()) or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        _emit(f"ERROR:fatal: {exc}")
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
