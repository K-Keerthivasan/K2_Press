"""Local model agent.

Drives the existing fetch -> score -> render pipeline via a local OpenAI-compatible model that
can either call tools natively or use the JSON fallback command loop. This
module is pure orchestration: it knows the tool *schemas* and runs the call
loop, but the actual tool implementations are injected by app.py (so there is
no duplicated pipeline logic and no circular import). Plain chat models can
still run agent tasks through the fallback path.

"""
from __future__ import annotations
import json
import re
from typing import Any, Callable


def system_prompt(brands: dict[str, str], formats: dict[str, str]) -> str:
    brand_lines = "\n".join(f"  - {k}: {n}" for k, n in brands.items())
    fmt_lines   = ", ".join(formats.keys())
    return (
        "You are the content agent for a multi-brand social-media post generator.\n"
        "You help the user fetch trending stories and turn them into ready-to-publish posts.\n\n"
        "Brands (use the KEY, not the name):\n" + brand_lines + "\n\n"
        f"Formats: {fmt_lines}. Default to 'carousel' unless the user asks otherwise.\n\n"
        "How to work:\n"
        "- To create posts you MUST first call fetch_top_stories for the brand, then call\n"
        "  generate_posts referencing the story NUMBERS returned by that fetch.\n"
        "- Never invent story numbers you have not fetched.\n"
        "- Do exactly what the user asked; don't over-produce. If they say '3 posts', make 3.\n"
        "- When finished, give a short plain-text summary: brand, how many posts, which format.\n"
        "- Posts are saved to disk for the user to review before any publishing Ã¢â‚¬â€ you do not publish."
    )


def build_tools(brands: dict[str, str], formats: dict[str, str]) -> list[dict]:
    bkeys = list(brands.keys())
    fkeys = list(formats.keys())
    return [
        {"type": "function", "function": {
            "name": "fetch_top_stories",
            "description": "Fetch and score the top trending stories for a brand from its RSS feeds. "
                           "Returns numbered stories you can later reference in generate_posts.",
            "parameters": {"type": "object", "properties": {
                "brand_key": {"type": "string", "enum": bkeys},
                "category":  {"type": "string", "description": "feed category key, or '' for all feeds"},
                "count":     {"type": "integer", "description": "how many top stories to return (1-25)"},
            }, "required": ["brand_key"]}}},
        {"type": "function", "function": {
            "name": "generate_posts",
            "description": "Render posts for stories returned by the most recent fetch_top_stories for this brand.",
            "parameters": {"type": "object", "properties": {
                "brand_key":     {"type": "string", "enum": bkeys},
                "story_numbers": {"type": "array", "items": {"type": "integer"},
                                  "description": "1-based numbers from the last fetch for this brand"},
                "formats":       {"type": "array", "items": {"type": "string", "enum": fkeys},
                                  "description": "post formats to render (default ['carousel'])"},
                "tone":          {"type": "string", "description": "optional stance, e.g. 'hyped, exciting'"},
            }, "required": ["brand_key", "story_numbers"]}}},
    ]


def _tool_result_summary(result: Any) -> str:
    if isinstance(result, dict):
        return result.get("summary") or result.get("error") or "done"
    return str(result)


def _call_tool(name: str, args: dict, dispatch: dict[str, Callable[..., Any]]) -> Any:
    fn = dispatch.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    try:
        return fn(**args)
    except Exception as e:
        return {"error": str(e)}


def _extract_json_object(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group())


def _json_agent_messages(messages: list[dict], tools: list[dict]) -> list[dict]:
    tool_docs = []
    for t in tools:
        f = t["function"]
        tool_docs.append({
            "name": f["name"],
            "description": f.get("description", ""),
            "parameters": f.get("parameters", {}),
        })
    protocol = (
        "Your local model server does not need native function calling. "
        "Use this JSON command protocol instead.\n"
        "Reply with exactly one JSON object and no markdown.\n"
        "To call a tool: {\"tool\":\"tool_name\",\"arguments\":{...}}\n"
        "When finished: {\"final\":\"short plain-text summary\"}\n"
        "Available tools:\n" + json.dumps(tool_docs, ensure_ascii=False)
    )
    out = []
    for m in messages:
        if m.get("role") == "tool":
            out.append({"role": "user", "content": "Tool result: " + m.get("content", "")})
        elif m.get("role") in {"system", "user", "assistant"}:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    out.insert(1 if out and out[0].get("role") == "system" else 0,
               {"role": "system", "content": protocol})
    return out


def _run_json_agent(client, model: str, messages: list[dict], tools: list[dict],
                    dispatch: dict[str, Callable[..., Any]],
                    max_steps: int) -> tuple[str, list[dict], list[dict]]:
    """Tool loop for plain local chat models without native tool calling."""
    steps: list[dict] = []
    artifacts: list[dict] = []
    json_messages = _json_agent_messages(messages, tools)

    for _ in range(max_steps):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=json_messages,
                response_format={"type": "json_object"},
                temperature=0.1,  # Deterministic: low temp for consistent output
            )
        except Exception:
            resp = client.chat.completions.create(
                model=model,
                messages=json_messages,
                temperature=0.1,  # Deterministic: low temp for consistent output
            )
        content = resp.choices[0].message.content or ""
        json_messages.append({"role": "assistant", "content": content})
        try:
            cmd = _extract_json_object(content)
        except (json.JSONDecodeError, TypeError):
            json_messages.append({
                "role": "user",
                "content": "Reply again with only JSON: either a tool command or a final answer.",
            })
            continue

        if cmd.get("final"):
            final = str(cmd["final"])
            messages.append({"role": "assistant", "content": final})
            return final, steps, artifacts

        name = cmd.get("tool")
        args = cmd.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            json_messages.append({
                "role": "user",
                "content": "Invalid command. Use {\"tool\":\"name\",\"arguments\":{...}}.",
            })
            continue

        result = _call_tool(name, args, dispatch)
        if isinstance(result, dict) and result.get("posts"):
            artifacts.extend(result["posts"])
        steps.append({"tool": name, "args": args, "summary": _tool_result_summary(result)})
        result_text = json.dumps(result, ensure_ascii=False)[:6000]
        json_messages.append({"role": "user", "content": f"Tool result for {name}: {result_text}"})
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"Tool result for {name}: {result_text}"})

    return ("Stopped after the maximum number of steps. Ask me to continue if needed.",
            steps, artifacts)


def run_agent(client, model: str, messages: list[dict], tools: list[dict],
              dispatch: dict[str, Callable[..., Any]],
              max_steps: int = 8) -> tuple[str, list[dict], list[dict]]:
    """Run the tool-call loop until the model returns a plain answer.

    Returns (final_text, steps, artifacts). `messages` is mutated in place so the
    caller can persist the running conversation. `steps` is a UI-friendly log of
    tool actions; `artifacts` accumulates generated posts for thumbnails.
    """
    steps: list[dict] = []
    artifacts: list[dict] = []

    for _ in range(max_steps):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=tools, temperature=0.1)  # Deterministic
        except Exception:
            return _run_json_agent(client, model, messages, tools, dispatch, max_steps)
        msg = resp.choices[0].message

        if not msg.tool_calls:
            if not steps:
                return _run_json_agent(client, model, messages, tools, dispatch, max_steps)
            messages.append({"role": "assistant", "content": msg.content or ""})
            return (msg.content or "", steps, artifacts)

        # Echo the assistant's tool-call turn back into the history verbatim.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            } for tc in msg.tool_calls],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _call_tool(name, args, dispatch)
            if isinstance(result, dict) and result.get("posts"):
                artifacts.extend(result["posts"])
            steps.append({
                "tool": name, "args": args,
                "summary": _tool_result_summary(result),
            })
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result)[:6000]})

    return ("Stopped after the maximum number of steps. Ask me to continue if needed.",
            steps, artifacts)
