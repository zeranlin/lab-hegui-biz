#!/usr/bin/env python3
"""Minimal Responses API to Chat Completions proxy for local hegui runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from typing import Any


RESP_COUNTER = count(1)


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if text is not None:
                parts.append(str(text))
    return "\n".join(parts)


def responses_input_to_messages(req: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system_parts: list[str] = []
    instructions = req.get("instructions")
    if instructions:
        system_parts.append(str(instructions))

    pending_tool_names: dict[str, str] = {}
    pending_tool_calls: list[dict[str, Any]] = []
    for item in req.get("input") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            role = item.get("role") or "user"
            if role == "developer":
                system_parts.append(content_text(item.get("content")))
                continue
            elif role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            if role == "system":
                system_parts.append(content_text(item.get("content")))
                continue
            messages.append({"role": role, "content": content_text(item.get("content"))})
        elif item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or f"call_{len(pending_tool_calls) + 1}")
            name = str(item.get("name") or "unknown")
            args = item.get("arguments") or "{}"
            pending_tool_names[call_id] = name
            pending_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": str(args)},
                }
            )
        elif item_type == "function_call_output":
            if pending_tool_calls:
                messages.append({"role": "assistant", "content": None, "tool_calls": pending_tool_calls})
                pending_tool_calls = []
            call_id = str(item.get("call_id") or "")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": pending_tool_names.get(call_id, "tool"),
                    "content": str(item.get("output") or ""),
                }
            )
    if pending_tool_calls:
        messages.append({"role": "assistant", "content": None, "tool_calls": pending_tool_calls})
    if system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(part for part in system_parts if part)})
    return messages


def responses_tools_to_chat(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if tool.get("type") != "function":
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description") or "",
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def post_chat(upstream: str, api_key: str, req: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": req.get("model"),
        "messages": responses_input_to_messages(req),
        "tools": responses_tools_to_chat(req.get("tools")),
        "tool_choice": "auto",
        "stream": False,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if not payload["tools"]:
        payload.pop("tools")
        payload.pop("tool_choice")
    with open("/tmp/hegui-chat-payload.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        upstream.rstrip("/") + "/chat/completions",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"upstream chat failed: {exc.code} {detail}") from exc


def sse_event(handler: BaseHTTPRequestHandler, event: dict[str, Any]) -> None:
    handler.wfile.write(("event: " + event["type"] + "\n").encode("utf-8"))
    handler.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8"))
    handler.wfile.flush()


def completed_response(response_id: str, model: str, output: list[dict[str, Any]], usage: dict[str, Any] | None) -> dict[str, Any]:
    upstream_usage = usage or {}
    input_tokens = upstream_usage.get("prompt_tokens", upstream_usage.get("input_tokens", 0)) or 0
    output_tokens = upstream_usage.get("completion_tokens", upstream_usage.get("output_tokens", 0)) or 0
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": upstream_usage.get("total_tokens", input_tokens + output_tokens) or 0,
        },
    }


def emit_chat_as_responses(handler: BaseHTTPRequestHandler, req: dict[str, Any], chat: dict[str, Any]) -> None:
    response_id = f"resp_{next(RESP_COUNTER)}"
    model = chat.get("model") or req.get("model") or "unknown"
    message = (chat.get("choices") or [{}])[0].get("message") or {}
    output: list[dict[str, Any]] = []

    sse_event(handler, {"type": "response.created", "response": {"id": response_id, "object": "response", "created_at": int(time.time()), "status": "in_progress", "model": model, "output": []}})

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        for index, call in enumerate(tool_calls):
            function = call.get("function") or {}
            call_id = str(call.get("id") or f"call_{index + 1}")
            item_id = f"fc_{call_id}"
            args = str(function.get("arguments") or "{}")
            item = {
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "name": function.get("name") or "unknown",
                "arguments": args,
                "status": "completed",
            }
            sse_event(handler, {"type": "response.output_item.added", "output_index": index, "item": item | {"status": "in_progress", "arguments": ""}})
            sse_event(handler, {"type": "response.function_call_arguments.delta", "item_id": item_id, "output_index": index, "delta": args})
            sse_event(handler, {"type": "response.function_call_arguments.done", "item_id": item_id, "output_index": index, "arguments": args})
            sse_event(handler, {"type": "response.output_item.done", "output_index": index, "item": item})
            output.append(item)
    else:
        text = message.get("content")
        if text is None:
            text = ""
        item_id = f"msg_{response_id}"
        item = {
            "type": "message",
            "id": item_id,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": str(text), "annotations": []}],
        }
        sse_event(handler, {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": item_id, "status": "in_progress", "role": "assistant", "content": []}})
        sse_event(handler, {"type": "response.content_part.added", "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
        if text:
            sse_event(handler, {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": str(text)})
        sse_event(handler, {"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": str(text)})
        sse_event(handler, {"type": "response.content_part.done", "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": str(text), "annotations": []}})
        sse_event(handler, {"type": "response.output_item.done", "output_index": 0, "item": item})
        output.append(item)

    response = completed_response(response_id, model, output, chat.get("usage"))
    sse_event(handler, {"type": "response.completed", "response": response})
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def make_handler(upstream: str, api_key: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/v1/responses":
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get("content-length", "0")))
            try:
                req = json.loads(raw.decode("utf-8"))
                chat = post_chat(upstream, api_key, req)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                emit_chat_as_responses(self, req, chat)
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": str(exc)}}).encode("utf-8"))

        def log_message(self, fmt: str, *args: Any) -> None:
            print(fmt % args, file=sys.stderr)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="127.0.0.1:18081")
    parser.add_argument("--upstream", default=os.environ.get("HEGUI_CHAT_BASE_URL", "http://127.0.0.1:10011/v1"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    args = parser.parse_args()
    if not args.api_key:
        print("missing api key: set OPENAI_API_KEY or pass --api-key", file=sys.stderr)
        return 2
    host, port_s = args.listen.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port_s)), make_handler(args.upstream, args.api_key))
    print(f"responses_chat_proxy listening on http://{args.listen}/v1 -> {args.upstream}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
