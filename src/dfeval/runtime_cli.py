"""Developer entry points for the standalone inference resource broker."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .episode_spec import read_spec
from .inference import TextClient
from .runtime import RuntimeBroker, RuntimeLimits
from .runtime_http import make_server
from .runtime_witness import WitnessError, canonical, verify_witness


def serve(args: argparse.Namespace) -> int:
    spec = read_spec(Path(args.spec)) if args.spec else None
    limits = RuntimeLimits(max_calls=args.max_calls, max_output_tokens=args.output_budget,
                           max_input_bytes=args.input_byte_budget, max_request_bytes=args.max_request_bytes,
                           max_in_flight=args.max_concurrent, max_wall_seconds=args.wall_seconds,
                           call_timeout=args.call_timeout, max_evidence_bytes=args.max_evidence_bytes)
    client = TextClient(provider=args.provider, model=args.model, mode=args.mode,
                        endpoint=args.endpoint, api_key_env=args.api_key_env,
                        timeout=args.call_timeout, max_output_tokens=args.max_output_per_call,
                        max_request_bytes=min(16_000_000, args.max_request_bytes + 65536),
                        max_response_bytes=args.max_response_bytes, num_ctx=args.num_ctx,
                        token_limit_field=args.token_limit_field)
    broker = RuntimeBroker(Path(args.out), client, limits=limits,
                           objective=args.objective or "Care for the dwarves", spec=spec)
    server = None
    try:
        server = make_server(broker, port=args.port)
        server.timeout = 0.25
        connection = broker.out / "runtime-connection.json"
        # A development client credential, never the upstream provider credential.
        fd = os.open(connection, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as dest:
            dest.write(canonical({"url": f"http://127.0.0.1:{server.server_address[1]}",
                                  "token": server.token}) + b"\n")
        print("Development inference broker ready; no game connection or code isolation.", flush=True)
        print(f"Client connection file: {connection}", flush=True)
        print(f"Evidence: {broker.out / 'evidence'}", flush=True)
        try:
            while True:
                state = broker.status()
                if state["admission_stopped"] or state["remaining"]["wall_seconds"] <= 0:
                    break
                server.handle_request()
        except KeyboardInterrupt:
            pass
    finally:
        try:
            try:
                status = broker.close()
            except WitnessError:
                status = broker.status()
        finally:
            if server is not None:
                server.server_close()
    print(json.dumps(status, indent=2))
    return 1 if status["fatal_error"] else 0


def audit(args: argparse.Namespace) -> int:
    result = verify_witness(Path(args.events), expected_head=args.head)
    print(json.dumps(result, indent=2))
    return 0


def add_parsers(sub) -> None:
    serve_parser = sub.add_parser("runtime-serve", help="development shared inference broker; no game or code sandbox")
    serve_parser.add_argument("--out", required=True, help="new session directory; existing directories are rejected")
    serve_parser.add_argument("--model", required=True)
    serve_parser.add_argument("--provider", choices=["ollama", "chat_completions"], default="ollama")
    serve_parser.add_argument("--mode", choices=["local", "cloud"], default="local")
    serve_parser.add_argument("--endpoint")
    serve_parser.add_argument("--api-key-env")
    briefing = serve_parser.add_mutually_exclusive_group()
    briefing.add_argument("--objective", help="exact objective text; default: Care for the dwarves")
    briefing.add_argument("--spec", help="optional reusable episode specification; declarations only, no game is loaded")
    serve_parser.add_argument("--port", type=int, default=0)
    serve_parser.add_argument("--num-ctx", type=int, help="Ollama context capacity; omitted uses server default")
    serve_parser.add_argument("--token-limit-field", choices=["max_tokens", "max_completion_tokens"])
    serve_parser.add_argument("--max-calls", type=int, default=64)
    serve_parser.add_argument("--output-budget", type=int, default=131072)
    serve_parser.add_argument("--input-byte-budget", type=int, default=16_000_000)
    serve_parser.add_argument("--max-output-per-call", type=int, default=32768)
    serve_parser.add_argument("--max-concurrent", type=int, default=4)
    serve_parser.add_argument("--wall-seconds", type=float, default=3600)
    serve_parser.add_argument("--call-timeout", type=float, default=300)
    serve_parser.add_argument("--max-request-bytes", type=int, default=262144)
    serve_parser.add_argument("--max-response-bytes", type=int, default=2_000_000)
    serve_parser.add_argument("--max-evidence-bytes", type=int, default=256_000_000)
    serve_parser.set_defaults(func=serve)
    audit_parser = sub.add_parser("runtime-audit", help="verify a development runtime witness hash chain")
    audit_parser.add_argument("events")
    audit_parser.add_argument("--head", help="independently retained final SHA-256 head")
    audit_parser.set_defaults(func=audit)
