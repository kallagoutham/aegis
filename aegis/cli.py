"""Command line interface for Aegis.

Operational tasks that need to run outside the HTTP API: ingesting a runbook
repository, checking the index, searching from a terminal, and creating the
first admin user.

Ingestion in particular belongs here rather than only behind an endpoint. The
common case is a scheduled job syncing a docs repository, and a cron entry
calling ``aegis ingest ./runbooks`` is simpler and more debuggable than one
issuing an authenticated HTTP request.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
import json
from pathlib import Path
import sys

from aegis.core.config import settings


def _print_header(text: str) -> None:
    """Print a section header."""
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


async def _cmd_ingest(args: argparse.Namespace) -> int:
    """Ingest a file or directory into the knowledge base."""
    from aegis.ingestion.pipeline import IngestionPipeline

    # Blocking filesystem calls are fine here: the CLI is a single-shot
    # process with no concurrent requests to stall.
    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        print(f"error: path does not exist: {path}", file=sys.stderr)
        return 1

    _print_header(f"Ingesting {path}")
    pipeline = IngestionPipeline(concurrency=args.concurrency)
    summary = await pipeline.ingest_path(path, force=args.force)

    print(f"\nProcessed : {len(summary.results)}")
    print(f"Indexed   : {summary.indexed}")
    print(f"Unchanged : {summary.skipped}")
    print(f"Failed    : {summary.failed}")
    print(f"Chunks    : {summary.total_chunks}")
    print(f"Duration  : {summary.duration_ms / 1000:.1f}s")

    if summary.failed:
        print("\nFailures:")
        for result in summary.results:
            if result.error:
                print(f"  {result.source_uri}: {result.error}")

    # Non-zero exit on any failure, so a cron job or CI step notices.
    return 1 if summary.failed else 0


async def _cmd_search(args: argparse.Namespace) -> int:
    """Search the knowledge base from the terminal."""
    from aegis.retrieval.hybrid import (
        HybridRetriever,
        RetrievalRequest,
    )
    from aegis.services.database import session_scope

    async with session_scope() as session:
        retriever = HybridRetriever(session)
        response = await retriever.search(
            RetrievalRequest(
                query=args.query,
                top_k=args.top_k,
                service=args.service,
                rerank=not args.no_rerank,
            )
        )

    if args.json:
        print(json.dumps({"query": response.query, "hits": response.citations()}, indent=2))
        return 0

    _print_header(f"{len(response.results)} results for: {args.query}")
    for index, result in enumerate(response.results, start=1):
        print(f"\n[{index}] {result.citation()}")
        print(f"    score={result.score:.3f} strategy={result.strategy} source={result.source_type.value}")
        preview = result.content[:400].replace("\n", "\n    ")
        print(f"    {preview}")

    print(f"\nTimings: {response.timings_ms}")
    return 0


async def _cmd_stats(args: argparse.Namespace) -> int:
    """Report knowledge base size and coverage."""
    from aegis.retrieval.vector_store import VectorStore
    from aegis.services.database import session_scope

    async with session_scope() as session:
        stats = await VectorStore(session).stats()

    if args.json:
        print(json.dumps(stats, indent=2))
        return 0

    _print_header("Knowledge base")
    print(f"Documents          : {stats['documents']}")
    print(f"Chunks             : {stats['chunks']}")
    print(f"Chunks unembedded  : {stats['unembedded_chunks']}")
    print(f"Distinct services  : {stats['services']}")
    print("\nBy type:")
    for source_type, count in sorted(stats["documents_by_type"].items()):
        print(f"  {source_type:20s} {count}")

    if stats["unembedded_chunks"]:
        print(
            f"\nwarning: {stats['unembedded_chunks']} chunks have no embedding. "
            "Re-run ingestion with --force to repair."
        )
    return 0


async def _cmd_analyze(args: argparse.Namespace) -> int:
    """Analyse a log file without touching the database or an LLM."""
    from aegis.analysis import analyse_logs

    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"error: file does not exist: {path}", file=sys.stderr)
        return 1

    analysis = analyse_logs(path.read_text(encoding="utf-8", errors="replace"), max_lines=args.max_lines)

    if args.json:
        print(json.dumps(analysis.to_dict(), indent=2))
    else:
        print(analysis.to_prompt_summary())
    return 0


async def _cmd_create_user(args: argparse.Namespace) -> int:
    """Create a user account, optionally as an admin.

    Needed to bootstrap: ingestion requires an admin, and registration through
    the API creates responders only.
    """
    import getpass

    from aegis.models.user import UserRole
    from aegis.services.database import (
        UserRepository,
        session_scope,
    )

    password = args.password or getpass.getpass("Password: ")

    from aegis.schemas.auth import UserCreate

    try:
        # Route through the schema so the CLI enforces the same password policy
        # as the API rather than quietly allowing weaker ones.
        UserCreate(email=args.email, password=password, full_name=args.full_name or "")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    async with session_scope() as session:
        repository = UserRepository(session)
        user = await repository.create_user(args.email, password, args.full_name or "")
        if args.admin:
            user.role = UserRole.ADMIN
            session.add(user)

    print(f"Created {user.email} with role {'admin' if args.admin else 'responder'} (id {user.id})")
    return 0


async def _cmd_check(args: argparse.Namespace) -> int:
    """Verify configuration and dependency reachability."""
    from aegis.services.database import (
        check_database_health,
        check_pgvector_available,
    )

    _print_header("Aegis configuration check")
    print(f"Environment   : {settings.ENVIRONMENT.value}")
    print(f"Database      : {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}")
    print(f"LLM model     : {settings.DEFAULT_LLM_MODEL}")
    print(f"Embedding     : {settings.EMBEDDING_MODEL} ({settings.EMBEDDING_DIMENSIONS}d)")
    print(f"Langfuse      : {'enabled' if settings.LANGFUSE_ENABLED else 'disabled'}")

    failures = 0

    database = await check_database_health()
    if database["healthy"]:
        print("\n[ok]   database reachable")
    else:
        print(f"\n[FAIL] database unreachable: {database.get('error')}")
        failures += 1

    if database["healthy"]:
        if await check_pgvector_available():
            print("[ok]   pgvector extension installed")
        else:
            print("[FAIL] pgvector missing - run: CREATE EXTENSION IF NOT EXISTS vector;")
            failures += 1

    if settings.OPENAI_API_KEY.get_secret_value():
        print("[ok]   OPENAI_API_KEY configured")
    else:
        print("[FAIL] OPENAI_API_KEY is not set")
        failures += 1

    if settings.JWT_SECRET_KEY.get_secret_value():
        print("[ok]   JWT_SECRET_KEY configured")
    else:
        print("[FAIL] JWT_SECRET_KEY is not set")
        failures += 1

    print(f"\n{failures} check(s) failed." if failures else "\nAll checks passed.")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="aegis",
        description="Aegis - AI incident response platform",
    )
    parser.add_argument("--version", action="version", version=f"aegis {settings.VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Ingest documents into the knowledge base")
    ingest.add_argument("path", help="File or directory to ingest")
    ingest.add_argument("--force", action="store_true", help="Re-index even if content is unchanged")
    ingest.add_argument("--concurrency", type=int, default=settings.INGESTION_CONCURRENCY)
    ingest.set_defaults(handler=_cmd_ingest)

    search = subparsers.add_parser("search", help="Search the knowledge base")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=8)
    search.add_argument("--service", default=None)
    search.add_argument("--no-rerank", action="store_true")
    search.add_argument("--json", action="store_true")
    search.set_defaults(handler=_cmd_search)

    stats = subparsers.add_parser("stats", help="Show knowledge base statistics")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(handler=_cmd_stats)

    analyze = subparsers.add_parser("analyze", help="Analyse a log file")
    analyze.add_argument("path")
    analyze.add_argument("--max-lines", type=int, default=None)
    analyze.add_argument("--json", action="store_true")
    analyze.set_defaults(handler=_cmd_analyze)

    create_user = subparsers.add_parser("create-user", help="Create a user account")
    create_user.add_argument("email")
    create_user.add_argument("--password", default=None, help="Prompted for if omitted")
    create_user.add_argument("--full-name", default=None)
    create_user.add_argument("--admin", action="store_true")
    create_user.set_defaults(handler=_cmd_create_user)

    check = subparsers.add_parser("check", help="Verify configuration and dependencies")
    check.set_defaults(handler=_cmd_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    args = build_parser().parse_args(argv)

    try:
        return asyncio.run(args.handler(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if settings.DEBUG:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
