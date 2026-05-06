"""Command-line entry point for ``ehr-simulator``.

Today: one subcommand (``serve``) that boots uvicorn against the FastAPI app
factory in :mod:`ehr_simulator.web.app`. Typer + ``validate-config`` + ``preflight``
+ ``preview`` arrive in S5.
"""

from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ehr-simulator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="Run the FastAPI server via uvicorn.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        uvicorn.run(
            "ehr_simulator.web.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
