from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from ..mcp_server import call_tool, serve_stdio


def cmd_mcp_serve(args: argparse.Namespace) -> None:
    serve_stdio(args.db)


def cmd_mcp_doctor(args: argparse.Namespace) -> None:
    result = call_tool("mcp_doctor", {"project_id": args.project}, args.db)
    result["db_path"] = str(args.db) if args.db else "default"
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_mcp_reviewer_start(args: argparse.Namespace) -> None:
    register_result = call_tool(
        "register_reviewer",
        {
            "reviewer": args.reviewer,
            "capabilities": args.capabilities,
            "max_concurrent": args.max_concurrent,
            "metadata": {"startup_path": "nilo mcp reviewer-start"},
        },
        args.db,
    )
    registered_reviewer = register_result["reviewer"]["reviewer"]
    print(f"reviewer: {registered_reviewer}")
    print(f"status: {register_result['reviewer']['status']}")
    print(f"heartbeat: {register_result['reviewer']['last_heartbeat_at']}")
    print("revived_review_requests:")
    if register_result["revived_review_requests"]:
        for review_id in register_result["revived_review_requests"]:
            print(f"- {review_id}")
    else:
        print("- none")
    print("claim:")
    print("- none")
    print("next_action: reviewer worker must call claim_next_review via MCP, then import_review_result")


def write_claim_file(path: Path, body: str, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    print(f"{label}: {path}")


def cmd_mcp_reviewer_claim(args: argparse.Namespace) -> None:
    register_result = call_tool(
        "register_reviewer",
        {
            "reviewer": args.reviewer,
            "capabilities": args.capabilities,
            "max_concurrent": args.max_concurrent,
            "metadata": {"worker_path": "nilo mcp reviewer-claim"},
        },
        args.db,
    )
    registered_reviewer = register_result["reviewer"]["reviewer"]
    claim_args = {"reviewer": registered_reviewer}
    if args.project:
        claim_args["project_id"] = args.project
    claim_result = call_tool("claim_next_review", claim_args, args.db)
    print(f"reviewer: {registered_reviewer}")
    print(f"status: {register_result['reviewer']['status']}")
    print(f"heartbeat: {register_result['reviewer']['last_heartbeat_at']}")
    print("claim:")
    if not claim_result["claimed"]:
        print("- none")
        return
    print(f"- review_request: {claim_result['review_id']}")
    print(f"- task: {claim_result['task_id']}")
    print("next_action: review prompt_md and import_review_result through MCP")
    if args.write_default:
        review_id = claim_result["review_id"]
        prompt_path = Path(".nilo") / "reviews" / f"{review_id}_prompt.md"
        template_path = Path(".nilo") / "reviews" / f"{review_id}.md"
        write_claim_file(prompt_path, claim_result["prompt_md"], "handoff_prompt")
        write_claim_file(template_path, claim_result["template_md"], "review_template")


class LocalMcpStdioClient:
    def __init__(self, db_path: Path | None, transcript: list[dict] | None = None) -> None:
        command = [sys.executable, "-c", "from nilo.cli import main; main()"]
        if db_path is not None:
            command.extend(["--db", str(db_path)])
        command.extend(["mcp", "serve"])
        self.process = subprocess.Popen(
            command,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._next_id = 1
        self.transcript = transcript

    def initialize(self, client_name: str = "nilo-reviewer-worker") -> dict:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": client_name},
            },
        )
        self.notify("notifications/initialized")
        return response

    def call_tool(self, name: str, arguments: dict) -> dict:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response["result"]
        if result.get("isError"):
            raise RuntimeError(result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def request(self, method: str, params: dict) -> dict:
        request_id = self._next_id
        self._next_id += 1
        response = self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if "error" in response:
            raise RuntimeError(json.dumps(response["error"], ensure_ascii=False))
        return response

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method}, expect_response=False)

    def _send(self, payload: dict, expect_response: bool = True) -> dict:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("MCP stdio process pipes are not available")
        log_entry: dict = {"request": payload}
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        if not expect_response:
            log_entry["response"] = None
            if self.transcript is not None:
                self.transcript.append(log_entry)
            return {}
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr is not None else ""
            raise RuntimeError(f"MCP stdio process exited without a response: {stderr}")
        response = json.loads(line)
        log_entry["response"] = response
        if self.transcript is not None:
            self.transcript.append(log_entry)
        return response

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()


def cmd_mcp_ping(args: argparse.Namespace) -> None:
    transcript: list[dict] = []
    client = LocalMcpStdioClient(args.db, transcript=transcript)
    try:
        initialize_response = client.initialize(client_name="hello-client")
        tools_response = client.request("tools/list", {})
    finally:
        client.close()

    tools = tools_response["result"]["tools"]
    result = {
        "ok": True,
        "server": initialize_response["result"]["serverInfo"],
        "protocolVersion": initialize_response["result"]["protocolVersion"],
        "tool_count": len(tools),
        "tool_names": [tool["name"] for tool in tools],
        "transcript": transcript,
    }
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        args.log_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_mcp_reviewer_worker(args: argparse.Namespace) -> None:
    body_md = args.result_file.read()
    args.result_file.close()
    client = LocalMcpStdioClient(args.db)
    try:
        client.initialize()
        register_result = client.call_tool(
            "register_reviewer",
            {
                "reviewer": args.reviewer,
                "capabilities": args.capabilities,
                "max_concurrent": args.max_concurrent,
                "metadata": {"worker_path": "nilo mcp reviewer-worker"},
            },
        )
        registered_reviewer = register_result["reviewer"]["reviewer"]
        print(f"reviewer: {registered_reviewer}")
        print(f"status: {register_result['reviewer']['status']}")
        print(f"heartbeat: {register_result['reviewer']['last_heartbeat_at']}")

        deadline = time.monotonic() + max(args.wait_seconds, 0.0)
        claim_args = {"reviewer": registered_reviewer}
        if args.project:
            claim_args["project_id"] = args.project
        while True:
            claim_result = client.call_tool("claim_next_review", claim_args)
            if claim_result["claimed"]:
                break
            if time.monotonic() >= deadline:
                print("claim:")
                print("- none")
                raise SystemExit(1)
            time.sleep(max(args.poll_interval, 0.01))

        print("claim:")
        print(f"- review_request: {claim_result['review_id']}")
        print(f"- task: {claim_result['task_id']}")
        import_result = client.call_tool(
            "import_review_result",
            {
                "task_id": claim_result["task_id"],
                "review_id": claim_result["review_id"],
                "reviewer": registered_reviewer,
                "last_seen_event_id": claim_result["latest_event"]["event_id"],
                "body_md": body_md,
            },
        )
        print("import:")
        print(f"- review_result: {import_result['review_result']['id']}")
        print(f"- verdict: {import_result['review_result']['verdict']}")
    finally:
        client.close()
