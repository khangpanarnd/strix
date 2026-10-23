"""`strix view [<run>]` command: serve a run's viewer UI locally.

With no run to show (an empty workspace, or ``--launch``) the viewer starts in
**launch mode**: it builds a :class:`ScanLauncher` and serves the launch form so
a scan can be started from the browser (FR-1, FR-2). A graceful shutdown stops
any child the launcher started before the server stops (FR-28, NFR-6).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from rich.console import Console

from strix.core.paths import (
    RUNS_DIR_NAME,
    latest_run_dir,
    run_dir_for,
    run_record_path,
    runs_base_dir,
)
from strix.interface.viewer.launch_request import LaunchLimits
from strix.interface.viewer.scan_launcher import ScanLauncher
from strix.interface.viewer.server import authorized_url, bundle_is_built, serve
from strix.interface.viewer.transcript import read_run_summary


if TYPE_CHECKING:
    import argparse
    from http.server import ThreadingHTTPServer
    from pathlib import Path


logger = logging.getLogger(__name__)

# Hosts that keep the viewer private to this machine; any other bind is exposed
# to the network and warrants the NFR-6 warning.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def run_view(argv: list[str]) -> None:
    args = _parse_args(argv)
    console = Console()

    if not bundle_is_built():
        console.print(
            "[bold red]Viewer UI is not built.[/]\n"
            "Build it with: [cyan]cd strix/interface/viewer/frontend && npm ci && npm run build[/]"
        )
        raise SystemExit(1)

    run_dir = _resolve_run_dir(args.run, console, launch=args.launch)

    if run_dir is None:
        _serve_launch_mode(args, console)
    else:
        _serve_run(args, run_dir, console)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    import argparse

    parser = argparse.ArgumentParser(
        prog="strix view",
        description="Open a local web view of a Strix run, or launch a new scan from the browser.",
    )
    parser.add_argument(
        "run",
        nargs="?",
        default=None,
        help=(
            f"Run name under ./{RUNS_DIR_NAME} (defaults to the most recent run; "
            "with no run at all the viewer starts in launch mode)."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to serve on (default: an available ephemeral port).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1; use 0.0.0.0 for all IPv4 interfaces).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the browser automatically.",
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Start in launch mode (serve the form to start a new scan), even when runs exist.",
    )
    return parser.parse_args(argv)


def _serve_run(args: argparse.Namespace, run_dir: Path, console: Console) -> None:
    """Serve a fixed run exactly as before (``--launch`` absent, a run exists)."""
    httpd, url, token = serve(
        run_dir,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
    open_url = authorized_url(url, token)

    summary = read_run_summary(run_dir)
    live = not summary.get("finished", False)

    from strix.telemetry import posthog

    posthog.viewer_opened(source="cli", live=live)

    console.print()
    console.print(f"Serving [bold white]{run_dir.name}[/] ({_state_label(summary)}) at:")
    console.print(f"  [#60a5fa]{open_url}[/]", soft_wrap=True)
    console.print("[dim]This link authorizes the browser; anyone you share it with can steer[/]")
    console.print("[dim]a live scan and browse history. Press Ctrl-C to stop the viewer.[/]")
    console.print()

    _run_forever(console, httpd, launcher=None)


def _serve_launch_mode(args: argparse.Namespace, console: Console) -> None:
    """Serve the launch form (FR-1, FR-2); build a launcher and clean up its children."""
    runs_dir = runs_base_dir()
    runs_dir.mkdir(parents=True, exist_ok=True)
    launcher = ScanLauncher(runs_dir, LaunchLimits.from_env())

    httpd, url, token = serve(
        None,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        runs_dir=runs_dir,
        launcher=launcher,
    )
    open_url = authorized_url(url, token)

    from strix.telemetry import posthog

    posthog.viewer_opened(source="cli", live=False)

    console.print()
    console.print("Serving [bold white]Strix[/] ([#eab308]launch mode[/]) at:")
    console.print(f"  [#60a5fa]{open_url}[/]", soft_wrap=True)
    console.print("[dim]Open the link to start a scan from the browser. This link authorizes[/]")
    console.print("[dim]the browser; anyone you share it with can start scans. Ctrl-C to stop.[/]")
    if args.host not in _LOOPBACK_HOSTS:
        # NFR-6: a non-loopback bind is reachable over the network.
        console.print(
            "[bold yellow]Warning:[/] this viewer is reachable over the network. "
            "Anyone with the link can start scans that spend your LLM budget."
        )
    console.print()

    _run_forever(console, httpd, launcher=launcher)


def _run_forever(
    console: Console, httpd: ThreadingHTTPServer, *, launcher: ScanLauncher | None
) -> None:
    """Block until Ctrl-C, then stop launched children before the server stops.

    The launcher is shut down first (FR-28) so every child it started receives
    the graceful-then-force sequence before the HTTP server is torn down.
    """
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[dim]Viewer stopped.[/]")
    finally:
        if launcher is not None:
            launcher.shutdown()
        httpd.shutdown()
        httpd.server_close()


def _state_label(summary: dict[str, object]) -> str:
    if not summary.get("finished", False):
        return "[#eab308]live[/]"

    status = summary.get("status")
    if status == "failed":
        return "[#ef4444]failed[/]"
    if status in {"stopped", "interrupted"}:
        return f"[#eab308]{status}[/]"
    return "[#22c55e]finished[/]"


def _resolve_run_dir(run: str | None, console: Console, *, launch: bool) -> Path | None:
    """Resolve the run to serve, or None to start launch mode.

    ``--launch`` always starts launch mode. A named run that does not exist
    still errors as before (FR-2). Plain ``strix view`` with no runs starts
    launch mode (FR-1) rather than exiting.
    """
    if launch:
        return None

    if run:
        run_dir = run_dir_for(run)
        if not run_record_path(run_dir).is_file():
            _fail_no_run(console, requested=run)
        return run_dir

    # No run named and no --launch: open the latest run, or launch mode if the
    # workspace has no runs yet (FR-1).
    return latest_run_dir()


def _fail_no_run(console: Console, *, requested: str) -> None:
    base = runs_base_dir()
    available = (
        sorted(
            (child.name for child in base.iterdir() if run_record_path(child).is_file()),
            reverse=True,
        )
        if base.is_dir()
        else []
    )

    console.print(f"[bold red]No run named '{requested}' under ./{RUNS_DIR_NAME}.[/]")
    if available:
        console.print("Available runs:")
        for name in available[:20]:
            console.print(f"  [cyan]{name}[/]")
    raise SystemExit(1)


__all__ = ["run_view"]
