"""`strix view [<run>]` command: serve a run's viewer UI locally.

With no run to show (an empty workspace, or ``--launch``) the viewer starts in
**launch mode**: it builds a :class:`ScanLauncher` and serves the launch form so
a scan can be started from the browser (FR-1, FR-2). A graceful shutdown stops
any child the launcher started before the server stops (FR-28, NFR-6).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape

from strix.core.paths import (
    RUNS_DIR_NAME,
    latest_run_dir,
    run_dir_for,
    run_record_path,
    runs_base_dir,
)
from strix.interface.viewer import credentials_store
from strix.interface.viewer.launch_request import LaunchLimits
from strix.interface.viewer.llm_connection_test import ConnectionTester
from strix.interface.viewer.llm_profiles import ProfileStore
from strix.interface.viewer.llm_settings import SettingsStore
from strix.interface.viewer.login import (
    LoginConfig,
    LoginManager,
    env_login_config,
)
from strix.interface.viewer.notification_store import NotificationStore
from strix.interface.viewer.notifications import NotificationService
from strix.interface.viewer.scan_launcher import ScanLauncher
from strix.interface.viewer.server import bundle_is_built, serve
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
    # A locked-out operator can reset the stored account before anything starts.
    if args.reset_login is not None:
        _apply_login_reset(console, args.reset_login)
    # Built before anything starts so a bad STRIX_VIEWER_* value fails fast and clearly.
    login = _build_login(console)

    if run_dir is None:
        _serve_launch_mode(args, console, login)
    else:
        _serve_run(args, run_dir, console, login)


def _build_login(console: Console) -> LoginManager | None:  # noqa: ARG001 - kept for call-site symmetry
    """The sign-in manager, or None when login is switched off.

    Resolution: ``STRIX_VIEWER_LOGIN=off`` disables sign-in; otherwise the
    persisted credential store is used, seeded with the default ``admin`` /
    ``admin`` (change forced) on first run. A successful password change is
    written back to the store.
    """
    resolved = env_login_config(os.environ)
    if resolved is None:
        return None

    account = credentials_store.load_or_seed()
    config = LoginConfig(
        username=account.username,
        password_hash=account.password_hash,
        must_change=account.must_change,
    )

    def _persist(new_password: str) -> None:
        # Re-read so a change between startup and now is respected, then store
        # the new hash and clear the forced-change flag.
        current = credentials_store.read() or account
        credentials_store.set_password(current, new_password)

    return LoginManager(config, on_password_changed=_persist)


def _apply_login_reset(console: Console, new_password: str) -> None:
    """Reset the stored viewer account before serving, for a locked-out operator.

    An empty ``new_password`` restores the default ``admin`` / ``admin`` (with a
    forced change at first sign-in) by removing the store so it re-seeds. A given
    password is validated and written straight to the store with no forced
    change. Never prints the password.
    """
    if new_password == "":
        with contextlib.suppress(OSError):
            credentials_store.CREDENTIALS_PATH.unlink(missing_ok=True)
        credentials_store.load_or_seed()
        console.print(
            "[bold green]Login reset.[/] Sign in with [bold]admin[/] / [bold]admin[/]; "
            "you will be asked to set a new password."
        )
        return

    error = credentials_store.validate_new_password(new_password)
    if error is not None:
        console.print(
            "[bold red]Could not reset the password:[/] it must be at least "
            f"{credentials_store.MIN_PASSWORD_CHARS} characters and not the default."
        )
        raise SystemExit(1)
    account = credentials_store.load_or_seed()
    credentials_store.set_password(account, new_password)
    console.print(
        f"[bold green]Password reset[/] for user [bold]{escape(account.username)}[/]. "
        "Sign in with the new password."
    )


def _print_login_note(console: Console, login: LoginManager | None) -> None:
    """Tell the operator whether sign-in is on, without ever printing a secret."""
    if login is None:
        console.print(
            "[bold yellow]Sign-in is off[/] "
            "[dim](STRIX_VIEWER_LOGIN=off): anyone who can reach the page gets in.[/]"
        )
        return
    console.print(f"[dim]Sign-in is on. Username:[/] [bold]{escape(login.username)}[/]")
    if login.must_change_password:
        console.print(
            "[bold yellow]First sign-in:[/] log in with [bold]admin[/] / [bold]admin[/] "
            "[dim]— you will be asked to set a new password before the app opens.[/]"
        )
    console.print(
        "[dim]Change the password from the sign-in screen; it is stored on this machine.[/]"
    )
    console.print(
        "[dim]Locked out? Restart with[/] [cyan]strix view --reset-login[/] "
        "[dim](back to admin/admin) or[/] [cyan]strix view --reset-login <password>[/]"
        "[dim]. Turn sign-in off with STRIX_VIEWER_LOGIN=off.[/]"
    )


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
    parser.add_argument(
        "--reset-login",
        nargs="?",
        const="",
        default=None,
        metavar="PASSWORD",
        help=(
            "Reset the viewer account before starting: with a value, set the "
            "password to it; with no value, restore the default admin/admin "
            "(you will be asked to change it at first sign-in). Use this if you "
            "are locked out."
        ),
    )
    return parser.parse_args(argv)


def _serve_run(
    args: argparse.Namespace,
    run_dir: Path,
    console: Console,
    login: LoginManager | None = None,
) -> None:
    """Serve a fixed run (``--launch`` absent, a run exists), plus the settings page.

    The settings page works whenever the viewer runs (Step 6.1), so a store and
    tester are built here too; fixed-run mode has no launcher, so ``scan_active``
    is always false for this viewer.
    """
    settings_store = SettingsStore()
    connection_tester = ConnectionTester()
    # Fixed-run mode has no launcher, so there is nothing to watch; the service
    # still backs the Notifications page (configure + send a test) because the
    # config file is shared with launch-mode viewers.
    notifier = NotificationService(NotificationStore())
    notifier.start()
    httpd, url, _token = serve(
        run_dir,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        settings_store=settings_store,
        connection_tester=connection_tester,
        profile_store=ProfileStore(settings_store),
        notifier=notifier,
        login=login,
    )
    open_url = url

    summary = read_run_summary(run_dir)
    live = not summary.get("finished", False)

    from strix.telemetry import posthog

    posthog.viewer_opened(source="cli", live=live)

    console.print()
    console.print(f"Serving [bold white]{run_dir.name}[/] ({_state_label(summary)}) at:")
    console.print(f"  [#60a5fa]{open_url}[/]", soft_wrap=True)
    if login is None:
        console.print(
            "[dim]Sign-in is off: anyone who can reach this address can steer a live[/]"
        )
        console.print("[dim]scan and browse history. Press Ctrl-C to stop the viewer.[/]")
    else:
        console.print("[dim]Sign in to steer a live scan and browse history. Ctrl-C to stop.[/]")
    _print_login_note(console, login)
    console.print()

    _run_forever(
        console, httpd, launcher=None, connection_tester=connection_tester, notifier=notifier
    )


def _serve_launch_mode(
    args: argparse.Namespace, console: Console, login: LoginManager | None = None
) -> None:
    """Serve the launch form (FR-1, FR-2); build a launcher and clean up its children."""
    runs_dir = runs_base_dir()
    runs_dir.mkdir(parents=True, exist_ok=True)
    # Built once and shared: the store's hooks feed the launcher so a save
    # between two launches is seen by the next spawn with no restart (FR-17,
    # NFR-5), and the same store/tester back the settings page (Step 6.1).
    settings_store = SettingsStore()
    connection_tester = ConnectionTester()
    launcher = ScanLauncher(
        runs_dir,
        LaunchLimits.from_env(),
        child_env_builder=settings_store.child_env,
        pre_launch_check=settings_store.pre_launch_check,
    )
    # Watches the launcher's children and sends Slack / Teams / Telegram
    # notifications. Built after the launcher so it can observe it.
    notifier = NotificationService(NotificationStore(), launcher=launcher, runs_dir=runs_dir)
    notifier.start()

    httpd, url, _token = serve(
        None,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        runs_dir=runs_dir,
        launcher=launcher,
        settings_store=settings_store,
        connection_tester=connection_tester,
        profile_store=ProfileStore(settings_store),
        notifier=notifier,
        login=login,
    )
    open_url = url

    from strix.telemetry import posthog

    posthog.viewer_opened(source="cli", live=False)

    console.print()
    console.print("Serving [bold white]Strix[/] ([#eab308]launch mode[/]) at:")
    console.print(f"  [#60a5fa]{open_url}[/]", soft_wrap=True)
    console.print(
        "[dim]Open the link and sign in to start a scan from the browser. Ctrl-C to stop.[/]"
    )
    _print_login_note(console, login)
    if args.host not in _LOOPBACK_HOSTS:
        # NFR-6: a non-loopback bind is reachable over the network.
        console.print(
            "[bold yellow]Warning:[/] this viewer is reachable over the network. "
            "Anyone who signs in can start scans that spend your LLM budget."
        )
        if login is None:
            console.print(
                "[bold red]Sign-in is off while exposed to the network — "
                "anyone who can reach this port has full control.[/]"
            )
    console.print()

    _run_forever(
        console, httpd, launcher=launcher, connection_tester=connection_tester, notifier=notifier
    )


def _run_forever(
    console: Console,
    httpd: ThreadingHTTPServer,
    *,
    launcher: ScanLauncher | None,
    connection_tester: ConnectionTester | None = None,
    notifier: NotificationService | None = None,
) -> None:
    """Block until Ctrl-C, then stop launched children before the server stops.

    The launcher is shut down first (FR-28) so every child it started receives
    the graceful-then-force sequence before the HTTP server is torn down. The
    notifier stops right after, so scans the shutdown just stopped are still
    announced ("Scan stopped") before the process exits.

    A connection test in flight is handled by the tester itself: it runs one
    child at a time under a 90-second hard wall-clock limit and kills the whole
    process tree on expiry (BR-31), and the child is spawned with
    ``start_new_session`` / a new process group so it is not tied to this
    process. ``ConnectionTester`` exposes no ``close()``/``shutdown()`` (it owns
    no long-lived resource between tests), so there is nothing to stop here
    beyond letting that bounded test finish; the parameter is accepted to make
    the ownership explicit and to give a future tester-shutdown a wiring point.
    """
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[dim]Viewer stopped.[/]")
    finally:
        if launcher is not None:
            launcher.shutdown()
        if notifier is not None:
            notifier.stop()
        _ = connection_tester  # no shutdown hook today; see the docstring.
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
