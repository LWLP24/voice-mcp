from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calltool")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("api", help="Run the REST and MCP API server")

    worker = commands.add_parser("worker", help="Manage the LiveKit worker")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    worker_commands.add_parser("start", help="Start the LiveKit worker")

    doctor = commands.add_parser("doctor", help="Check CallTool dependencies")
    doctor.add_argument("--call", metavar="PHONE_NUMBER", help="Place a diagnostic call")

    sip = commands.add_parser("sip", help="Manage the LiveKit SIP integration")
    sip_commands = sip.add_subparsers(dest="sip_command", required=True)
    sip_commands.add_parser("bootstrap", help="Create or find the Telnyx outbound trunk")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "api":
        from calltool.api.app import run as run_api

        run_api()
        return

    if args.command == "worker":
        from calltool.worker.server import run as run_worker

        run_worker()
        return

    if args.command == "doctor":
        from calltool.cli.doctor import run as run_doctor

        raise SystemExit(asyncio.run(run_doctor(call_number=args.call)))

    if args.command == "sip":
        from calltool.cli.sip import bootstrap

        raise SystemExit(asyncio.run(bootstrap()))

    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    main()
