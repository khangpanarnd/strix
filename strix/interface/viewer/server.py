"""Local HTTP server that serves the viewer SPA and a run's data from disk.

Design notes:
- Uses only the standard library (no new runtime dependency). The workload is
  serving static files plus a handful of JSON reads off disk, so an async stack
  buys nothing here.
- The browser polls the JSON endpoints (~1s) rather than using SSE: a finished
  run stops polling, and short-lived polls survive sleep/network blips without
  server-side connection state, which suits a stdlib ThreadingHTTPServer.
- All reads happen per-request straight from disk, so the same server serves a
  live in-progress run and a finished one identically; the SPA distinguishes
  them via the ``finished`` flag on /api/run.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
import time
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from strix.core.paths import run_record_path
from strix.interface.viewer import auth
from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.launch_request import parse_launch_request
from strix.interface.viewer.transcript import (
    build_run_state,
    primary_target,
    read_report_markdown,
    read_run_summary,
    read_vulnerabilities,
    severity_counts,
)


if TYPE_CHECKING:
    from collections.abc import Callable

    from strix.interface.viewer.llm_connection_test import ConnectionTester
    from strix.interface.viewer.llm_profiles import ProfileStore
    from strix.interface.viewer.llm_settings import SettingsStore
    from strix.interface.viewer.notifications import NotificationService
    from strix.interface.viewer.scan_launcher import ScanLauncher


logger = logging.getLogger(__name__)


def bundle_dir() -> Path:
    """Directory holding the committed, prebuilt SPA (index.html + assets)."""
    return Path(__file__).resolve().parent / "static"


def bundle_is_built() -> bool:
    return (bundle_dir() / "index.html").is_file()


def _iter_run_dirs(base_dir: Path) -> list[Path]:
    """Every run directory under ``base_dir``, newest first by record mtime."""
    if not base_dir.is_dir():
        return []
    run_dirs = [child for child in base_dir.iterdir() if run_record_path(child).is_file()]
    run_dirs.sort(key=lambda child: run_record_path(child).stat().st_mtime, reverse=True)
    return run_dirs


def _latest_run_dir(base_dir: Path) -> Path | None:
    """The newest run under ``base_dir`` by ``run.json`` mtime, or None (DD-8)."""
    run_dirs = _iter_run_dirs(base_dir)
    return run_dirs[0] if run_dirs else None


def run_list_entry(run_dir: Path) -> dict[str, Any]:
    """Compact summary of a single run for the history list."""
    record = read_run_summary(run_dir)
    return {
        "name": record.get("run_name") or run_dir.name,
        "target": primary_target(record),
        "scan_mode": record.get("scan_mode"),
        "status": record.get("status"),
        "start_time": record.get("start_time"),
        "end_time": record.get("end_time"),
        "finished": bool(record.get("finished")),
        "severity_counts": severity_counts(read_vulnerabilities(run_dir)),
    }


def build_runs_payload(base_dir: Path, *, unlocked: bool) -> dict[str, Any]:
    """The /api/runs payload. Gates the run list behind the caller's authorization.

    ``unlocked`` is the caller's access decision (today: holds this process's
    session capability). When locked, the count is still advertised so the UI can
    tease the history, but the entries only appear once unlocked. Email
    verification is no longer part of this decision.
    """
    run_dirs = _iter_run_dirs(base_dir)
    count = len(run_dirs)
    if not unlocked:
        return {"locked": True, "count": count, "runs": []}
    return {"locked": False, "count": count, "runs": [run_list_entry(d) for d in run_dirs]}


def resolve_run_dir(
    base_dir: Path, run_param: str | None, default_run_dir: Path | None
) -> Path | None:
    """Resolve a ``?run=`` value to a real run directory under ``base_dir``.

    Returns ``default_run_dir`` when no run is requested (which may itself be
    ``None`` in launch mode with no run yet, so the caller answers 404
    ``no_run``). Rejects traversal and unknown runs (returns None) so the caller
    can answer 404.
    """
    if not run_param:
        return default_run_dir
    base = base_dir.resolve()
    candidate = (base / run_param).resolve()
    # Only direct children of the runs base that actually hold a run record.
    if candidate.parent != base or not run_record_path(candidate).is_file():
        return None
    return candidate


# Prefix of the cookie carrying the per-process session capability. The bound
# port is appended (``strix_viewer_session_<port>``) because browsers scope
# cookies by host only, never by port: concurrent viewers on 127.0.0.1 would
# otherwise share one cookie slot and clobber each other's session.
SESSION_COOKIE_PREFIX = "strix_viewer_session"


class _ViewerState:
    def __init__(
        self,
        run_dir: Path | None,
        runs_dir: Path,
        assets_dir: Path,
        steer_handler: Callable[[str, str], bool] | None = None,
        launcher: ScanLauncher | None = None,
        settings_store: SettingsStore | None = None,
        connection_tester: ConnectionTester | None = None,
        profile_store: ProfileStore | None = None,
        notifier: NotificationService | None = None,
    ) -> None:
        # The run the viewer was started on, or None in launch mode with no run
        # at start. Handlers go through ``default_run()`` (DD-8), never this
        # field directly, so a reload mid-scan lands on the newest run.
        self.run_dir = run_dir
        # The LLM-settings store and the isolated connection tester (Step 5/6).
        # Both are present whenever the viewer runs (launch and fixed-run mode);
        # when either is None the four settings routes answer 403
        # ``settings_unavailable`` and ``/api/capabilities`` reports
        # ``llm_settings.available=false`` (BR-5 analogue; see
        # ``_handle_settings_*``). They never gate the pre-existing endpoints.
        self.settings_store = settings_store
        self.connection_tester = connection_tester
        # The scan-notification service (Slack / Teams / Telegram). Optional: when
        # None the /api/notifications routes answer 403 ``notifications_unavailable``
        # and ``/api/capabilities`` reports ``notifications.available=false``.
        self.notifier = notifier
        # The named-profiles layer over ``settings_store`` (same lock/file). It
        # is optional: when not passed it is built lazily from ``settings_store``
        # by ``profiles()`` so existing call sites keep working. When there is no
        # settings store the four profile routes answer 403 like the flat ones.
        self._profile_store = profile_store
        self.assets_dir = assets_dir
        # The strix_runs directory that holds launched runs; replaces the old
        # ``run_dir.parent`` as the base for listing and resolving runs. Always
        # set, even when no run exists yet (launch mode).
        self.base_dir = runs_dir
        self.runs_dir = runs_dir
        # The launch service, present only in launch mode (BR-5). When None the
        # launch/stop/status endpoints answer 403 ``launch_unavailable``.
        self.launcher = launcher
        # The viewer's own origin (``http://host:port``), set in ``serve()``
        # after the socket binds (BR-2). Empty until then.
        self.origin = ""
        self.bound_host = ""
        # Set only when the viewer runs inside a live scan process (the TUI
        # launcher), which can deliver a message to a running agent. Absent for
        # standalone ``strix view`` / finished runs, so steering is unavailable.
        self.steer_handler = steer_handler
        # Unguessable per-process capability. It is minted here, printed/opened
        # for the operator who started the server (see ``authorized_url``), and
        # exchanged for a session cookie only when presented on the initial page
        # load. It is the request-level authorization the review asked for:
        # reachability of the port (e.g. when bound with ``--host``) is not
        # enough to read run data, steer a live scan, trigger a report, or
        # browse history -- the token is never handed to a caller who merely
        # reaches ``/``.
        self.session_token = secrets.token_urlsafe(32)
        # Finalized in ``serve()`` once the port is known (the server binds
        # after this state is constructed); see SESSION_COOKIE_PREFIX.
        self.cookie_name = SESSION_COOKIE_PREFIX
        # Sliding-window rate limiter for settings CHANGES (PUT + reset), shared
        # across worker threads (BR-4: 30 per rolling minute). The connection
        # test keeps its own 6/min window inside ``ConnectionTester``, so it is
        # not counted here. ``_settings_change_lock`` guards the deque.
        self.settings_changes: deque[float] = deque()
        self.settings_change_lock = threading.Lock()

    def profiles(self) -> ProfileStore | None:
        """The profiles layer, built lazily from ``settings_store`` (DD).

        Returns ``None`` only when no settings store is wired (the profile
        routes then answer 403 ``settings_unavailable`` like the flat routes).
        """
        if self._profile_store is None and self.settings_store is not None:
            from strix.interface.viewer.llm_profiles import ProfileStore

            self._profile_store = ProfileStore(self.settings_store)
        return self._profile_store

    def default_run(self) -> Path | None:
        """The run a request resolves against when none is named (DD-8, §10a).

        Returns the fixed run when the viewer was started on one; otherwise the
        latest run at request time (so a reload mid-scan lands on the newest
        run), or ``None`` when the workspace has no run at all.
        """
        if self.run_dir is not None:
            return self.run_dir
        return _latest_run_dir(self.runs_dir)


def _make_handler(state: _ViewerState) -> type[BaseHTTPRequestHandler]:
    class ViewerHandler(BaseHTTPRequestHandler):
        server_version = "StrixViewer/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("viewer %s - %s", self.address_string(), format % args)

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            path = parts.path
            try:
                if path.startswith("/api/"):
                    self._handle_api(path, parse_qs(parts.query))
                else:
                    self._handle_static(path, parse_qs(parts.query))
            except BrokenPipeError:
                # The browser closed the connection mid-response (e.g. it
                # navigated away between polls). Not an error.
                logger.debug("viewer client disconnected during %s", path)
            except Exception:
                # A bad request must never kill the worker thread.
                logger.exception("viewer request failed: %s", path)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            routes: dict[str, Callable[[], None]] = {
                "/api/event": self._handle_event,
                "/api/auth/otp/start": self._handle_otp_start,
                "/api/auth/otp/verify": self._handle_otp_verify,
                "/api/auth/forget": self._handle_forget,
                "/api/report/send": self._handle_report_send,
                "/api/feedback": self._handle_feedback,
                "/api/agents/steer": self._handle_steer,
                "/api/scans": self._handle_scan_launch,
                "/api/scans/stop": self._handle_scan_stop,
                "/api/settings/reset": self._handle_settings_reset,
                "/api/settings/test": self._handle_settings_test,
                "/api/settings/profiles/save": self._handle_profiles_save,
                "/api/settings/profiles/activate": self._handle_profiles_activate,
                "/api/settings/profiles/delete": self._handle_profiles_delete,
                "/api/notifications/test": self._handle_notifications_test,
            }
            try:
                handler = routes.get(path)
                if handler is not None:
                    handler()
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            except BrokenPipeError:
                logger.debug("viewer client disconnected during POST %s", path)
            except Exception:
                # A bad request must never kill the worker thread.
                logger.exception("viewer request failed: POST %s", path)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

        def do_PUT(self) -> None:
            path = urlsplit(self.path).path
            try:
                if path == "/api/settings":
                    self._handle_settings_put()
                elif path == "/api/notifications":
                    self._handle_notifications_put()
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            except BrokenPipeError:
                logger.debug("viewer client disconnected during PUT %s", path)
            except Exception:
                # A bad request must never kill the worker thread.
                logger.exception("viewer request failed: PUT %s", path)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {}
            return body if isinstance(body, dict) else {}

        # Funnel events the viewer is allowed to forward. This handler is the
        # trust boundary: only these event names, with only their known props,
        # ever reach PostHog. Everything else (including any PII) is dropped.
        _EMAIL_EVENTS = frozenset(
            {"email_submitted", "email_verified", "report_sent", "work_email_required"}
        )

        def _handle_event(self) -> None:
            body = self._read_body()
            # Forwarded as anonymous PostHog events that respect the global
            # telemetry opt-out. Never forward the email, code, or report body:
            # only the whitelisted event names and their known props are passed.
            event = body.get("event")
            if event == "cta_clicked":
                from strix.telemetry import posthog

                cta = str(body.get("cta") or "unknown")
                surface = body.get("surface")
                posthog.viewer_cta_clicked(cta, surface=str(surface) if surface else None)
            elif event in self._EMAIL_EVENTS:
                from strix.telemetry import posthog

                purpose = body.get("purpose")
                posthog.viewer_email_event(str(event), purpose=str(purpose) if purpose else None)
            elif event == "agent_steered":
                from strix.telemetry import posthog

                posthog.viewer_agent_steered()
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()

        def _handle_api(self, path: str, query: dict[str, list[str]]) -> None:
            # The launched run is always viewable with no verification. The
            # cross-run history list (/api/runs) unlocks its entries for any
            # caller that holds this process's session capability, so merely
            # reaching an exposed --host port never leaks the run list (the
            # cookie is handed only to the browser this process bootstrapped).
            # Email verification is intentionally NOT required here: browsing
            # local run history is a local convenience, and gating it behind an
            # OTP was unnecessary friction. Verification still guards emailing an
            # encrypted report (see _handle_report_send).
            if path == "/api/runs":
                unlocked = self._has_session()
                payload = build_runs_payload(state.base_dir, unlocked=unlocked)
                self._send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/capabilities":
                self._send_json(HTTPStatus.OK, self._capabilities_payload())
                return
            # Each of these checks the session cookie itself (or deliberately
            # answers without one, for auth status), so they sit before the
            # blanket gate below.
            own_gate_routes = {
                "/api/settings": self._handle_settings_get,
                "/api/settings/profiles": self._handle_profiles_get,
                "/api/notifications": self._handle_notifications_get,
                "/api/auth/status": self._handle_auth_status,
            }
            own_gate_handler = own_gate_routes.get(path)
            if own_gate_handler is not None:
                own_gate_handler()
                return

            # All remaining GET endpoints expose run metadata or scan output.
            # Require the capability even for the run used to launch the viewer;
            # reachability of an exposed --host port must not grant data access.
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            self._handle_authed_get(path, query)

        def _handle_authed_get(self, path: str, query: dict[str, list[str]]) -> None:
            """Session-gated GET routes: launch status/artifacts and run data."""
            # Launch-mode status and artifact endpoints take the raw query (they
            # resolve or ignore ``?run=`` themselves).
            launch_routes = {
                "/api/scans/status": self._handle_scan_status,
                "/api/run/artifacts": self._handle_run_artifacts,
                "/api/run/artifact": self._handle_run_artifact,
            }
            launch_handler = launch_routes.get(path)
            if launch_handler is not None:
                launch_handler(query)
                return

            run_values = query.get("run")
            run_param = run_values[0] if run_values else None
            run_dir = resolve_run_dir(state.base_dir, run_param, state.default_run())
            if run_dir is None:
                # In launch mode with no run yet, or a reload before any run
                # exists, the run-data endpoints answer 404 ``no_run`` (DD-8).
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "no_run"})
                return

            # The launched run is always viewable. Any *other* run's data is part
            # of the history: with a fixed run it needs the session capability
            # (held above). With no fixed run (launch mode) every run requires
            # the cookie, which a browser that opened the authorized link has.
            if (
                state.run_dir is not None
                and run_dir.resolve() != state.run_dir.resolve()
                and not self._has_session()
            ):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return

            if path == "/api/run":
                self._send_json(HTTPStatus.OK, read_run_summary(run_dir))
            elif path == "/api/vulnerabilities":
                self._send_json(HTTPStatus.OK, read_vulnerabilities(run_dir))
            elif path == "/api/report":
                self._send_json(HTTPStatus.OK, {"markdown": read_report_markdown(run_dir)})
            elif path == "/api/transcript":
                self._send_json(HTTPStatus.OK, build_run_state(run_dir))
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})

        def _handle_auth_status(self) -> None:
            # The cached verified email is only disclosed to a caller holding this
            # process's session capability, so a cookie-less client on an exposed
            # --host port cannot read it; everyone else looks unverified.
            # Verification is reported through is_verified() so an expired record
            # is advertised as unverified -- otherwise the SPA would suppress
            # re-verification while history stays locked, stranding the user.
            if not self._has_session():
                self._send_json(HTTPStatus.OK, {"verified": False, "email": None})
                return
            record = auth.read_auth()
            self._send_json(
                HTTPStatus.OK,
                {
                    "verified": auth.is_verified(),
                    "email": record.get("email") if record else None,
                },
            )

        def _handle_otp_start(self) -> None:
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            email = str(self._read_body().get("email") or "").strip()
            if not email:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_email"})
                return
            try:
                auth.otp_start(email)
            except auth.RelayError as exc:
                self._send_relay_error(exc)
                return
            self._send_json(HTTPStatus.OK, {"ok": True})

        def _handle_otp_verify(self) -> None:
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            body = self._read_body()
            email = str(body.get("email") or "").strip()
            code = str(body.get("code") or "").strip()
            if not email or not code:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_code"})
                return
            try:
                result = auth.otp_verify(email, code)
            except auth.RelayError as exc:
                self._send_relay_error(exc)
                return
            auth.write_auth(
                email=result.get("email") or email,
                token=result["token"],
                verified_at=result.get("expires_at") or "",
            )
            verified_email = result.get("email") or email
            self._send_json(HTTPStatus.OK, {"verified": True, "email": verified_email})

        def _handle_forget(self) -> None:
            # Clearing the cached verification is a state change, so it requires
            # this process's session capability: a cookie-less caller on an
            # exposed --host port must not be able to log the operator out.
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            auth.forget()
            self._send_json(HTTPStatus.OK, {"ok": True})

        def _handle_report_send(self) -> None:
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            record = auth.read_auth()
            if record is None:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unverified"})
                return
            run_param = str(self._read_body().get("run") or "") or None
            run_dir = resolve_run_dir(state.base_dir, run_param, state.default_run())
            if run_dir is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown run"})
                return

            summary = read_run_summary(run_dir)
            # Emailing only makes sense for a completed run; a live scan would
            # send a partial report. The UI hides the entry point, but fail
            # closed here too so the endpoint can't be driven mid-scan.
            if not summary.get("finished", False):
                self._send_json(HTTPStatus.CONFLICT, {"error": "run_not_finished"})
                return

            from strix.interface.viewer.report_pdf import build_encrypted_report

            pdf_bytes, password, filename = build_encrypted_report(run_dir)
            run_name = str(summary.get("run_name") or run_dir.name)
            target = primary_target(summary) or "unknown target"
            try:
                # The password is intentionally NOT passed here; only the
                # encrypted PDF bytes reach the relay.
                auth.report_send(record["token"], pdf_bytes, filename, run_name, target)
            except auth.RelayError as exc:
                self._send_relay_error(exc)
                return
            # The password is returned only to a session-authorized browser.
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "password": password, "filename": filename},
            )

        # Cap on a feedback message so a runaway client cannot flood the relay.
        _FEEDBACK_MESSAGE_MAX = 5000

        def _handle_feedback(self) -> None:
            # Requires this process's session capability, like the other POSTs,
            # so an exposed --host port can't be used to spam the relay.
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            body = self._read_body()
            email = str(body.get("email") or "").strip()
            message = str(body.get("message") or "").strip()
            if not email:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_email"})
                return
            if not message:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_message"})
                return
            message = message[: self._FEEDBACK_MESSAGE_MAX]
            try:
                auth.feedback_submit(email, message)
            except auth.RelayError as exc:
                self._send_relay_error(exc)
                return
            # Server-authoritative: fire only after a successful relay (respects
            # the telemetry opt-out; no message/email content is sent).
            from strix.telemetry import posthog

            posthog.viewer_feedback_submitted()
            self._send_json(HTTPStatus.OK, {"ok": True})

        # Cap on a steering message so a runaway client cannot flood the agent.
        _STEER_MESSAGE_MAX = 4000

        def _handle_steer(self) -> None:
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            body = self._read_body()
            agent_id = body.get("agent_id")
            message = body.get("message")
            if not isinstance(agent_id, str) or not agent_id.strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_agent_id"})
                return
            if (
                not isinstance(message, str)
                or not message.strip()
                or len(message) > self._STEER_MESSAGE_MAX
            ):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_message"})
                return
            if state.steer_handler is None:
                # Standalone / finished-run viewing has no live scan to steer.
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "steering_unavailable"})
                return
            delivered = state.steer_handler(agent_id, message)
            if delivered:
                self._send_json(HTTPStatus.OK, {"ok": True})
            else:
                self._send_json(HTTPStatus.OK, {"ok": False, "error": "not_delivered"})

        # Launch endpoint caps: the JSON body is at most 16 KiB (NFR-3, BR-3).
        _LAUNCH_BODY_MAX = 16 * 1024

        def _capabilities_payload(self) -> dict[str, Any]:
            """The /api/capabilities body, unauthenticated as today (FR-3, DD-8).

            ``scan_launch_available`` is true exactly in launch mode; ``initial_run``
            is true when a run exists at this moment; ``scan_limits`` mirrors the
            server's bounds so the form hard-codes nothing.
            """
            launch_available = state.launcher is not None
            payload: dict[str, Any] = {
                "can_steer": state.steer_handler is not None,
                "scan_launch_available": launch_available,
                "initial_run": state.default_run() is not None,
                "scan_limits": None,
                # The settings page is available whenever the store was wired
                # (launch and fixed-run mode). ``editable`` tells the SPA whether
                # to render the write controls; a non-loopback bind shows them
                # read-only (BR-2). Unauthenticated, like the rest of this body.
                "llm_settings": {
                    "available": state.settings_store is not None,
                    "editable": self._is_loopback_bind(),
                },
                # Scan notifications (Slack / Teams / Telegram): same read-only
                # rule as the LLM settings on a non-loopback bind.
                "notifications": {
                    "available": state.notifier is not None,
                    "editable": self._is_loopback_bind(),
                },
            }
            if launch_available:
                limits = state.launcher.limits  # type: ignore[union-attr]
                payload["scan_limits"] = {
                    "max_budget_usd": limits.max_budget_usd,
                    "default_budget_usd": limits.default_budget_usd,
                    "modes": list(limits.modes),
                    "instruction_max": limits.instruction_max,
                }
            return payload

        def _launch_precheck(self) -> bool:
            """Run the ordered launch gate checks a→e (section 2, BR-1..BR-3).

            Returns True when every check passed and the caller may read the
            body and proceed; otherwise it has already sent the error response.
            Order: cookie → launch mode → Origin/Host → Content-Type →
            Content-Length. The body is never read until all pass.
            """
            error = self._first_gate_error()
            if error is None:
                return True
            status, code = error
            self._send_json(status, {"error": code})
            return False

        def _first_gate_error(self) -> tuple[HTTPStatus, str] | None:
            """The first failing launch gate as ``(status, code)``, or None if all pass."""
            if not self._has_session():
                return HTTPStatus.FORBIDDEN, "forbidden"
            if state.launcher is None:
                return HTTPStatus.FORBIDDEN, "launch_unavailable"
            if not self._origin_ok():
                return HTTPStatus.FORBIDDEN, "bad_origin"
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_content_type"
            return self._content_length_error()

        def _content_length_error(self) -> tuple[HTTPStatus, str] | None:
            """Validate ``Content-Length``: 411 when missing/invalid, 413 when oversized."""
            length = self._content_length()
            if length is None:
                return HTTPStatus.LENGTH_REQUIRED, "length_required"
            if length > self._LAUNCH_BODY_MAX:
                return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large"
            return None

        def _content_length(self) -> int | None:
            """The numeric ``Content-Length``, or None when missing/invalid (BR-3).

            A missing length (including a chunked body, which the stdlib server
            does not support) returns None so the caller answers 411.
            """
            raw = self.headers.get("Content-Length")
            if raw is None:
                return None
            try:
                value = int(raw)
            except ValueError:
                return None
            return value if value >= 0 else None

        def _origin_ok(self) -> bool:
            """Enforce the Origin/Host rule (BR-2).

            When ``Origin`` is present it must equal the viewer's own origin (with
            loopback aliases accepted only when the viewer is bound to loopback).
            When absent, ``Host`` must equal the bound ``host:port``.
            """
            origin = self.headers.get("Origin")
            if origin is not None:
                return origin in self._acceptable_origins()
            host = (self.headers.get("Host") or "").strip()
            return host in self._acceptable_hosts()

        def _bound_hostport(self) -> str:
            """The viewer's bound ``host:port`` parsed from its origin."""
            return state.origin.split("://", 1)[-1] if state.origin else ""

        def _is_loopback_bind(self) -> bool:
            return state.bound_host in ("127.0.0.1", "localhost", "::1")

        def _acceptable_origins(self) -> set[str]:
            origins = {state.origin}
            if self._is_loopback_bind():
                _, _, port = self._bound_hostport().partition(":")
                suffix = f":{port}" if port else ""
                origins |= {f"http://127.0.0.1{suffix}", f"http://localhost{suffix}"}
            return origins

        def _acceptable_hosts(self) -> set[str]:
            hosts = {self._bound_hostport()}
            if self._is_loopback_bind():
                _, _, port = self._bound_hostport().partition(":")
                suffix = f":{port}" if port else ""
                hosts |= {f"127.0.0.1{suffix}", f"localhost{suffix}"}
            return hosts

        def _read_launch_body(self, length: int) -> Any:
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw or b"{}")

        def _send_launch_error(self, exc: LaunchError) -> None:
            headers = (
                {"Retry-After": str(exc.extra["retry_after"])}
                if exc.code == "rate_limited"
                else None
            )
            self._send_json(HTTPStatus(exc.status), exc.to_payload(), extra_headers=headers)

        def _handle_scan_launch(self) -> None:
            """POST /api/scans — validate, rate-limit, spawn (section 2)."""
            if not self._launch_precheck():
                return
            assert state.launcher is not None
            length = self._content_length() or 0
            try:
                body = self._read_launch_body(length)
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            try:
                request = parse_launch_request(body, state.launcher.limits)
                run_name = state.launcher.launch(request)
            except LaunchError as exc:
                self._send_launch_error(exc)
                return
            self._send_json(HTTPStatus.ACCEPTED, {"run_name": run_name, "state": "starting"})

        def _handle_scan_stop(self) -> None:
            """POST /api/scans/stop — graceful stop of a live child (FR-27)."""
            if not self._launch_precheck():
                return
            assert state.launcher is not None
            length = self._content_length() or 0
            try:
                body = self._read_launch_body(length)
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            run = body.get("run") if isinstance(body, dict) else None
            if not isinstance(run, str) or not run:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            try:
                result = state.launcher.stop(run)
            except LaunchError as exc:
                self._send_launch_error(exc)
                return
            self._send_json(HTTPStatus.ACCEPTED, result)

        def _handle_scan_status(self, query: dict[str, list[str]]) -> None:
            """GET /api/scans/status?run= — status of a tracked child (session-gated)."""
            if state.launcher is None:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "launch_unavailable"})
                return
            run_values = query.get("run")
            run = run_values[0] if run_values else ""
            self._send_json(HTTPStatus.OK, state.launcher.status(run))

        # --- LLM settings (Step 5.1; FR-1..FR-9, FR-27..FR-32, BR-1..BR-5) ---

        # Settings change rate limit (BR-4): 30 changes per rolling minute (PUT, reset,
        # profile save/activate/delete). Raised from 10 so setting up several profiles
        # in a row is not throttled.
        _SETTINGS_CHANGE_LIMIT = 30
        _SETTINGS_RATE_WINDOW_SECONDS = 60.0

        def _handle_settings_get(self) -> None:
            """GET /api/settings — the effective view (section 3; FR-5..FR-8).

            Requires the session cookie (BR-1); when no store is wired it answers
            403 ``settings_unavailable`` (the chosen sentinel, mirrored by
            ``/api/capabilities``). GET works on any bind — read-only viewers
            still see their settings (BR-2) — so ``editable`` reflects the real
            loopback decision and no editable gate is applied here.

            Lock order (BR-35): ``scan_active`` is read from the launcher BEFORE
            ``store.view`` takes the store lock, never while holding it.
            """
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            if state.settings_store is None:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "settings_unavailable"})
                return
            # Launcher lock (outer) before the store lock (inner): snapshot the
            # active-scan flag first, then build the view.
            scan_active = state.launcher is not None and state.launcher.active_run() is not None
            view = state.settings_store.view(
                editable=self._is_loopback_bind(), scan_active=scan_active
            )
            self._send_json(HTTPStatus.OK, view)

        def _settings_gate_error(self) -> tuple[HTTPStatus, str] | None:
            """First failing gate for a settings change/test, or None if all pass.

            Order (section 4): cookie → store wired → editable (loopback bind) →
            Origin/Host → Content-Type → Content-Length. The body is never read
            until every header check passes (BR-3). The per-endpoint rate limit
            runs after this (the test endpoint's own 6/min lives in the tester).
            """
            if not self._has_session():
                return HTTPStatus.FORBIDDEN, "forbidden"
            if state.settings_store is None:
                return HTTPStatus.FORBIDDEN, "settings_unavailable"
            if not self._is_loopback_bind():
                return HTTPStatus.FORBIDDEN, "settings_read_only"
            if not self._origin_ok():
                return HTTPStatus.FORBIDDEN, "bad_origin"
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_content_type"
            return self._content_length_error()

        def _settings_precheck(self) -> bool:
            """Run the ordered settings gate; send the error and return False on failure."""
            error = self._settings_gate_error()
            if error is None:
                return True
            status, code = error
            self._send_json(status, {"error": code})
            return False

        def _settings_change_rate_ok(self) -> bool:
            """Count this change against the 10/min window; send 429 when full (BR-4).

            Counts PUT and reset together. On a breach it sends ``rate_limited``
            (429) with a ``Retry-After`` header and returns False. The connection
            test is NOT counted here — it has its own 6/min window in the tester.
            """
            now = time.monotonic()
            cutoff = now - self._SETTINGS_RATE_WINDOW_SECONDS
            with state.settings_change_lock:
                while state.settings_changes and state.settings_changes[0] <= cutoff:
                    state.settings_changes.popleft()
                if len(state.settings_changes) >= self._SETTINGS_CHANGE_LIMIT:
                    self._send_json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "rate_limited", "message": "too many settings changes"},
                        extra_headers={"Retry-After": str(int(self._SETTINGS_RATE_WINDOW_SECONDS))},
                    )
                    return False
                state.settings_changes.append(now)
            return True

        def _read_settings_body(self) -> Any:
            """Read the JSON body after the gate passed; never logged (NFR-1)."""
            length = self._content_length() or 0
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw or b"{}")

        def _handle_settings_put(self) -> None:
            """PUT /api/settings — validate, save, return the refreshed view (section 4).

            Gate → rate limit (10/min, PUT+reset) → read body → ``store.apply``.
            On success returns 200 with the GET view (``editable`` overridden with
            the real loopback value) plus ``warnings``. All ``LaunchError`` (the
            error-table codes the store raises) map through ``_send_launch_error``.
            The key is never read into any response or log (BR-12, NFR-1).
            """
            if not self._settings_precheck():
                return
            assert state.settings_store is not None
            if not self._settings_change_rate_ok():
                return
            try:
                body = self._read_settings_body()
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            if not isinstance(body, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            try:
                view = state.settings_store.apply(body)
            except LaunchError as exc:
                self._send_launch_error(exc)
                return
            # ``apply`` returns the view with ``editable=True``; override with the
            # real bind so a race on the toggle cannot mislead the SPA.
            view["editable"] = self._is_loopback_bind()
            self._send_json(HTTPStatus.OK, view)

        def _handle_settings_reset(self) -> None:
            """POST /api/settings/reset — clear the marker + strip set (section 5, FR-14).

            Same gate and rate window as PUT. Body ``{"confirm": true}``; the
            store raises ``invalid_json`` when confirm is not true.
            """
            if not self._settings_precheck():
                return
            assert state.settings_store is not None
            if not self._settings_change_rate_ok():
                return
            try:
                body = self._read_settings_body()
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            confirm = body.get("confirm") if isinstance(body, dict) else None
            try:
                view = state.settings_store.reset(confirm=confirm is True)
            except LaunchError as exc:
                self._send_launch_error(exc)
                return
            view["editable"] = self._is_loopback_bind()
            self._send_json(HTTPStatus.OK, view)

        def _handle_settings_test(self) -> None:
            """POST /api/settings/test — isolated preflight (section 7, FR-27..FR-32).

            Same gate as PUT, but NOT counted against the 10/min change window —
            the tester enforces its own 6/min and one-at-a-time limits. Resolves
            effective values (``store.effective_test_values``), then runs the
            child (``tester.run``) and returns the result dict. An ``ok:false``
            result is still HTTP 200; only ``LaunchError`` (409/429 from the
            tester, 400 from value resolution) maps through the error path.
            """
            if not self._settings_precheck():
                return
            assert state.settings_store is not None
            if state.connection_tester is None:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "settings_unavailable"})
                return
            try:
                body = self._read_settings_body()
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            if not isinstance(body, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            try:
                if isinstance(body, dict) and body.get("profile_id") is not None:
                    profiles = state.profiles()
                    assert profiles is not None
                    values = profiles.effective_test_values(body)
                else:
                    values = state.settings_store.effective_test_values(body)
                result = state.connection_tester.run(values)
            except LaunchError as exc:
                self._send_launch_error(exc)
                return
            self._send_json(HTTPStatus.OK, result)

        # --- LLM profiles (spec: several named profiles, one active) --------

        def _handle_profiles_get(self) -> None:
            """GET /api/settings/profiles — the profiles view (spec GET shape).

            Same authorization as ``GET /api/settings``: cookie required, works
            on any bind, ``editable`` reflects the real loopback decision. The
            key is never returned.
            """
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            profiles = state.profiles()
            if profiles is None:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "settings_unavailable"})
                return
            scan_active = state.launcher is not None and state.launcher.active_run() is not None
            try:
                view = profiles.view(editable=self._is_loopback_bind(), scan_active=scan_active)
            except LaunchError as exc:
                self._send_launch_error(exc)
                return
            self._send_json(HTTPStatus.OK, view)

        def _handle_profiles_change(
            self, apply: Callable[[ProfileStore, dict[str, Any]], dict[str, Any]]
        ) -> None:
            """Shared body for the three profile-change POSTs (spec).

            Same gate order and 10/min change window as ``/api/settings/reset``:
            gate -> rate limit -> read body -> ``apply``. On success returns 200
            with the profiles view; every ``LaunchError`` maps through the error
            path. ``editable`` is overridden with the real loopback value.
            """
            if not self._settings_precheck():
                return
            profiles = state.profiles()
            assert profiles is not None
            if not self._settings_change_rate_ok():
                return
            try:
                body = self._read_settings_body()
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            if not isinstance(body, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            try:
                view = apply(profiles, body)
            except LaunchError as exc:
                self._send_launch_error(exc)
                return
            view["editable"] = self._is_loopback_bind()
            self._send_json(HTTPStatus.OK, view)

        def _handle_profiles_save(self) -> None:
            """POST /api/settings/profiles/save — create or update a profile."""
            self._handle_profiles_change(lambda p, b: p.save(b))

        def _handle_profiles_activate(self) -> None:
            """POST /api/settings/profiles/activate — set and mirror the active profile."""
            self._handle_profiles_change(lambda p, b: p.activate(b))

        def _handle_profiles_delete(self) -> None:
            """POST /api/settings/profiles/delete — delete a non-active profile."""
            self._handle_profiles_change(lambda p, b: p.delete(b))

        # The four allow-listed downloadable artifacts, keyed by the fixed name
        # the client sends; the value is never joined from client text (BR-32).
        _ARTIFACTS: ClassVar[dict[str, str]] = {
            "report.html": "report.html",
            "penetration_test_report.md": "penetration_test_report.md",
            "findings.sarif": "findings.sarif",
            "vulnerabilities.json": "vulnerabilities.json",
        }

        # --- Scan notifications (Slack / Microsoft Teams / Telegram) ---

        def _notifications_gate_error(self) -> tuple[HTTPStatus, str] | None:
            """First failing gate for a notifications change/test, or None if all pass.

            Same order as the settings gate: cookie → service wired → editable
            (loopback bind) → Origin/Host → Content-Type → Content-Length. The
            body is never read until every header check passes.
            """
            if not self._has_session():
                return HTTPStatus.FORBIDDEN, "forbidden"
            if state.notifier is None:
                return HTTPStatus.FORBIDDEN, "notifications_unavailable"
            if not self._is_loopback_bind():
                return HTTPStatus.FORBIDDEN, "notifications_read_only"
            if not self._origin_ok():
                return HTTPStatus.FORBIDDEN, "bad_origin"
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_content_type"
            return self._content_length_error()

        def _notifications_precheck(self) -> bool:
            error = self._notifications_gate_error()
            if error is None:
                return True
            status, code = error
            self._send_json(status, {"error": code})
            return False

        def _handle_notifications_get(self) -> None:
            """GET /api/notifications — flags and hints only; secrets are never returned."""
            if not self._has_session():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            if state.notifier is None:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "notifications_unavailable"})
                return
            view = state.notifier.store.view(editable=self._is_loopback_bind())
            self._send_json(HTTPStatus.OK, view)

        def _handle_notifications_put(self) -> None:
            """PUT /api/notifications — validate and save a partial update."""
            if not self._notifications_precheck():
                return
            assert state.notifier is not None
            if not self._settings_change_rate_ok():
                return
            try:
                body = self._read_settings_body()
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            if not isinstance(body, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            try:
                view = state.notifier.store.apply(body)
            except LaunchError as exc:
                self._send_launch_error(exc)
                return
            view["editable"] = self._is_loopback_bind()
            self._send_json(HTTPStatus.OK, view)

        def _handle_notifications_test(self) -> None:
            """POST /api/notifications/test — send one sample message to one channel.

            Body: ``{"channel": "slack"|"teams"|"telegram"}`` plus optional typed
            values (``webhook_url`` / ``bot_token`` / ``chat_id``) that override
            the saved ones, so a destination can be tested before it is saved.
            A failed delivery is still HTTP 200 with ``ok: false``; only a bad
            request or the 6/min limit maps to an error status.
            """
            if not self._notifications_precheck():
                return
            assert state.notifier is not None
            try:
                body = self._read_settings_body()
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            if not isinstance(body, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            channel = body.get("channel")
            if not isinstance(channel, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_channel", "message": "channel must be a string"},
                )
                return
            try:
                resolved = state.notifier.store.resolve_test_channel(channel, body)
                result = state.notifier.send_test(resolved)
            except LaunchError as exc:
                self._send_launch_error(exc)
                return
            self._send_json(
                HTTPStatus.OK,
                {"ok": result.ok, "message": result.message, "status": result.status},
            )

        def _handle_run_artifacts(self, query: dict[str, list[str]]) -> None:
            """GET /api/run/artifacts?run= — which allow-listed files exist (BR-32)."""
            run_dir = self._resolve_artifact_run(query)
            if run_dir is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "no_run"})
                return
            available = [name for name, fn in self._ARTIFACTS.items() if (run_dir / fn).is_file()]
            self._send_json(HTTPStatus.OK, {"available": available})

        def _handle_run_artifact(self, query: dict[str, list[str]]) -> None:
            """GET /api/run/artifact?run=&name= — stream one allow-listed file (BR-32)."""
            run_dir = self._resolve_artifact_run(query)
            if run_dir is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "no_run"})
                return
            name_values = query.get("name")
            key = name_values[0] if name_values else ""
            filename = self._ARTIFACTS.get(key)
            if filename is None:
                # Any name not in the fixed dict is 404; client text is never a path.
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            target = run_dir / filename
            if not target.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            content = target.read_bytes()
            content_type, _ = mimetypes.guess_type(filename)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def _resolve_artifact_run(self, query: dict[str, list[str]]) -> Path | None:
            """Resolve ``?run=`` through the existing run-name guard (BR-32, BR-33)."""
            run_values = query.get("run")
            run_param = run_values[0] if run_values else None
            return resolve_run_dir(state.base_dir, run_param, state.default_run())

        def _send_relay_error(self, exc: auth.RelayError) -> None:
            status_by_code = {
                "rate_limited": HTTPStatus.TOO_MANY_REQUESTS,
                "invalid_email": HTTPStatus.BAD_REQUEST,
                "invalid_message": HTTPStatus.BAD_REQUEST,
                "work_email_required": HTTPStatus.BAD_REQUEST,
                "invalid_code": HTTPStatus.FORBIDDEN,
                "reverify": HTTPStatus.UNAUTHORIZED,
                "forbidden": HTTPStatus.FORBIDDEN,
                "too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "unavailable": HTTPStatus.BAD_GATEWAY,
            }
            status = status_by_code.get(exc.code, HTTPStatus.BAD_GATEWAY)
            self._send_json(status, {"error": exc.code})

        def _cookies(self) -> dict[str, str]:
            jar: dict[str, str] = {}
            for chunk in (self.headers.get("Cookie") or "").split(";"):
                name, sep, value = chunk.strip().partition("=")
                if sep:
                    jar[name] = value
            return jar

        def _has_session(self) -> bool:
            """True when the request carries this process's session capability.

            The cookie is set only when the SPA is served (index.html), so only
            the browser this process handed the page to can pass. A direct
            caller on an exposed port has no cookie and is rejected.
            """
            supplied = self._cookies().get(state.cookie_name, "")
            return bool(supplied) and secrets.compare_digest(supplied, state.session_token)

        def _token_presented(self, query: dict[str, list[str]]) -> bool:
            """True when the request carries the correct bootstrap token.

            The token reaches the operator's browser through the URL printed /
            opened by the process that started the server, a channel an
            arbitrary network caller on an exposed port cannot observe.
            """
            supplied = (query.get("token") or [""])[0]
            return bool(supplied) and secrets.compare_digest(supplied, state.session_token)

        def _handle_static(self, path: str, query: dict[str, list[str]]) -> None:
            target = self._resolve_asset(path)
            if target is None:
                # SPA fallback: unknown non-asset routes render index.html so
                # client-side deep links work.
                target = state.assets_dir / "index.html"
            is_index = target.name == "index.html"
            if not target.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            content = target.read_bytes()
            content_type, _ = mimetypes.guess_type(str(target))
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            if is_index and self._token_presented(query):
                # Exchange the bootstrap token for the per-process session
                # capability. Issued only when the correct token is presented,
                # so a caller who merely reaches ``/`` never obtains it.
                # HttpOnly (JS never needs it; fetch sends it automatically) and
                # SameSite=Strict (never sent from a cross-site context).
                self.send_header(
                    "Set-Cookie",
                    f"{state.cookie_name}={state.session_token}; Path=/; HttpOnly; SameSite=Strict",
                )
            self.end_headers()
            self.wfile.write(content)

        def _resolve_asset(self, path: str) -> Path | None:
            rel = unquote(path).lstrip("/")
            if not rel or rel.endswith("/"):
                return None
            root = state.assets_dir.resolve()
            candidate = (root / rel).resolve()
            # Path-traversal guard: never serve outside the bundle root.
            if root != candidate and root not in candidate.parents:
                logger.warning("viewer rejected traversal attempt: %s", path)
                return None
            return candidate if candidate.is_file() else None

        def _send_json(
            self,
            status: HTTPStatus,
            payload: Any,
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    return ViewerHandler


def authorized_url(base_url: str, token: str) -> str:
    """URL that bootstraps the viewer session for the operator.

    Presenting ``token`` on the initial page load is what mints the session
    cookie, so this URL is printed / opened only for the operator who started
    the server. Sharing it (rather than the bare ``base_url``) is what lets a
    trusted remote user authorize when the viewer is exposed with ``--host``.
    """
    return f"{base_url}/?{urlencode({'token': token})}"


def serve(
    run_dir: Path | None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    steer_handler: Callable[[str, str], bool] | None = None,
    runs_dir: Path | None = None,
    launcher: ScanLauncher | None = None,
    settings_store: SettingsStore | None = None,
    connection_tester: ConnectionTester | None = None,
    profile_store: ProfileStore | None = None,
    notifier: NotificationService | None = None,
) -> tuple[ThreadingHTTPServer, str, str]:
    """Start the viewer server on a background thread; return (server, url, token).

    ``url`` is the bare base; pass it through ``authorized_url(url, token)`` to
    build the operator link that authorizes the browser.

    Binds an ephemeral port by default. If a fixed ``port`` is requested but in
    use, falls back to an ephemeral port. Reused by both the ``strix view``
    command and the in-TUI launcher; callers own the server's lifetime.

    ``steer_handler`` is supplied only by the in-TUI launcher, which runs inside
    the live scan process and can forward a message to a running agent. Left
    ``None`` (standalone ``strix view``), steering is reported unavailable.

    Launch mode (``strix view --launch`` / an empty workspace) passes a
    ``launcher`` and an explicit ``runs_dir``, and may pass ``run_dir=None``
    when there is no run to start on. Existing callers keep passing a concrete
    ``run_dir`` and nothing else; ``runs_dir`` then defaults to its parent so
    their behaviour is unchanged (DD-8).

    ``settings_store`` and ``connection_tester`` wire the LLM settings page
    (Step 5/6). They are passed in both launch and fixed-run mode so the page
    works whenever the viewer runs. When either is ``None`` the four
    ``/api/settings*`` routes answer 403 ``settings_unavailable`` and
    ``/api/capabilities`` reports ``llm_settings.available=false``; the
    pre-existing endpoints are never affected.

    ``profile_store`` is the optional named-profiles layer; when omitted it is
    built lazily from ``settings_store`` by ``_ViewerState.profiles()`` so
    existing callers need not change.

    ``notifier`` is the optional scan-notification service (Slack / Teams /
    Telegram). When omitted, ``/api/notifications*`` answers 403
    ``notifications_unavailable`` and ``/api/capabilities`` reports
    ``notifications.available=false``. The caller owns its start/stop.
    """
    if runs_dir is None:
        if run_dir is None:
            raise ValueError("serve() needs either run_dir or runs_dir")
        runs_dir = run_dir.parent

    assets_dir = bundle_dir()
    state = _ViewerState(
        run_dir=run_dir,
        runs_dir=runs_dir,
        assets_dir=assets_dir,
        steer_handler=steer_handler,
        launcher=launcher,
        settings_store=settings_store,
        connection_tester=connection_tester,
        profile_store=profile_store,
        notifier=notifier,
    )
    handler = _make_handler(state)

    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError:
        if port == 0:
            raise
        logger.info("viewer port %s unavailable, falling back to an ephemeral port", port)
        httpd = ThreadingHTTPServer((host, 0), handler)

    httpd.daemon_threads = True
    bound_port = int(httpd.server_address[1])
    state.cookie_name = f"{SESSION_COOKIE_PREFIX}_{bound_port}"
    url = f"http://{host}:{bound_port}"
    # Record the bound origin so the Origin/Host check (BR-2) has the truth.
    state.origin = url
    state.bound_host = host

    thread = threading.Thread(target=httpd.serve_forever, name="strix-viewer", daemon=True)
    thread.start()

    if open_browser:
        _open_browser(authorized_url(url, state.session_token))

    return httpd, url, state.session_token


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - launching the browser is best-effort
        logger.debug("could not open browser for %s", url, exc_info=True)


__all__ = ["authorized_url", "bundle_dir", "bundle_is_built", "serve"]
