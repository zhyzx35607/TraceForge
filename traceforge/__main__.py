import argparse
import sys


def _configure_utf8_stdio() -> None:
    """Keep JSON diagnostics printable on Windows consoles and CI runners."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main():
    _configure_utf8_stdio()
    parser = argparse.ArgumentParser(prog="traceforge")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="Start the local workbench")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8710)
    commands.add_parser("evaluate", help="Run reproducible offline harness evaluation")
    commands.add_parser("retrieval-evaluate", help="Compare lexical retrieval methods on the public synthetic corpus")
    doctor = commands.add_parser("doctor", help="Inspect dependencies and provider connectivity")
    doctor.add_argument("--smoke", action="store_true", help="Also make one small billed generation request")
    benchmark = commands.add_parser("benchmark", help="Run the isolated multi-fault benchmark")
    benchmark.add_argument("--mode", choices=["oracle", "retrieval", "live"], default="oracle")
    benchmark.add_argument("--case", action="append", dest="cases", help="Case ID; repeat to select several")
    args = parser.parse_args()
    if args.command == "doctor":
        import asyncio
        import json

        from .config import Settings
        from .diagnostics import doctor
        print(json.dumps(asyncio.run(doctor(Settings.from_env(), smoke=args.smoke)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "evaluate":
        from .evaluation import main as evaluate
        return evaluate()
    if args.command == "retrieval-evaluate":
        import json

        from .config import Settings
        from .retrieval_evaluation import run_retrieval_evaluation
        report = run_retrieval_evaluation(Settings.from_env())
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "benchmark":
        import asyncio
        import json

        from .benchmark import run_benchmark
        from .config import Settings
        report = asyncio.run(run_benchmark(Settings.from_env(), args.mode, args.cases))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "completed" and not report["metrics"]["failed_cases"] else 1
    if args.command in {None, "serve"}:
        import uvicorn

        from .api import create_app
        from .config import Settings
        settings = Settings.from_env()
        host = getattr(args, "host", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"} and not settings.api_token:
            parser.error("非本机监听必须设置 TRACEFORGE_API_TOKEN")
        uvicorn.run(create_app(settings), host=host, port=getattr(args, "port", 8710))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
