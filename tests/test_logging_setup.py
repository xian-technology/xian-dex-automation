import io
import logging
import re

from xian_dex_automation.logging_setup import ServiceFormatter, exception_context


def test_utc_single_line_and_exception_context_omit_secrets():
    try:
        raise OSError(13, "private_key=secret-canary signed_payload=payload-canary")
    except OSError as exc:
        context = exception_context(exc)
    record = logging.LogRecord(
        "worker", logging.ERROR, __file__, 1, "operation failed\nforged | line", (), None
    )
    record.created = 0
    record.__dict__.update(context, reason="rpc_unavailable", request_id="request-7")
    rendered = ServiceFormatter("dex-automation").format(record)
    assert rendered.startswith("1970-01-01T00:00:00.000Z | ERROR | dex-automation/worker | ")
    assert "\n" not in rendered
    assert "secret-canary" not in rendered and "payload-canary" not in rendered
    assert "error_type=PermissionError" in rendered and "errno=13" in rendered
    assert "reason=rpc_unavailable" in rendered and "request_id=request-7" in rendered
    assert "test_utc_single_line" in rendered


def test_traceback_format_does_not_render_message_or_locals():
    try:
        local_secret = "never-render-this"
        raise ValueError(local_secret)
    except ValueError:
        import sys

        record = logging.LogRecord(
            "worker", logging.ERROR, __file__, 1, "request failed", (), sys.exc_info()
        )
    output = ServiceFormatter("dex-automation").format(record)
    assert local_secret not in output
    assert "ValueError" in output
    assert "test_traceback_format" in output


def test_formatter_bounds_records_and_omits_unapproved_context():
    record = logging.LogRecord("worker", logging.INFO, __file__, 1, "é" * 10000, (), None)
    record.private_key = "secret-context"
    output = ServiceFormatter("dex-automation").format(record)
    assert len(output.encode()) <= 8192
    assert "secret-context" not in output


def test_setup_keeps_stdout_clean_and_is_idempotent(monkeypatch):
    import sys

    from xian_dex_automation.logging_setup import configure_logging

    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        configure_logging()
        configure_logging()
        logging.getLogger("worker").warning("one marker")
        assert not stdout.getvalue()
        assert stderr.getvalue().count("one marker") == 1
        assert re.match(r"\d{4}-\d\d-\d\dT.*Z \| WARNING \| ", stderr.getvalue())
    finally:
        for handler in root.handlers:
            if handler not in handlers:
                handler.close()
        root.handlers = handlers
        root.setLevel(level)


def test_external_control_characters_and_credential_url_are_not_rendered():
    record = logging.LogRecord(
        "worker",
        logging.WARNING,
        __file__,
        1,
        "endpoint https://user:secret-url@rpc.test/?token=private-token\x1b[31m\tfailed",
        (),
        None,
    )
    output = ServiceFormatter().format(record)
    assert "secret-url" not in output and "private-token" not in output
    assert "\x1b" not in output and "\t" not in output


def test_task_group_failure_retains_child_location_without_payload():
    def leaf():
        raise ValueError("secret-child-payload")

    try:
        try:
            leaf()
        except ValueError as exc:
            raise ExceptionGroup("secret-group-payload", [exc])
    except ExceptionGroup as exc:
        context = exception_context(exc)
    assert "ExceptionGroup" in context["error_type"] and "ValueError" in context["error_type"]
    assert ":leaf" in context["error_trace"]
    assert "secret" not in str(context)


def test_repeating_worker_errors_keep_counts_and_recovery_without_flooding():
    from unittest.mock import Mock

    from xian_dex_automation.logging_setup import RetryReporter

    logger = Mock()
    now = [0]
    reporter = RetryReporter(logger, clock=lambda: now[0])
    for _ in range(20):
        reporter.failed(ValueError("secret-upstream"), config_generation=7)
    assert logger.error.call_count == 1
    now[0] = 60
    reporter.failed(ValueError("secret-upstream"), config_generation=7)
    assert logger.error.call_count == 2
    context = logger.error.call_args.kwargs["extra"]
    assert context["attempts"] == 21 and context["suppressed"] == 19
    assert "secret-upstream" not in str(context)
    reporter.recovered()
    reporter.recovered()
    assert logger.info.call_count == 1


def test_untrusted_values_are_bounded_before_formatting():
    class Unprintable:
        def __str__(self):
            raise AssertionError("str must not run")

        def __repr__(self):
            raise AssertionError("repr must not run")

    record = logging.LogRecord("worker", logging.ERROR, __file__, 1, "%s", (Unprintable(),), None)
    record.reason = "x" * 1_000_000
    record.request_id = Unprintable()
    record.errno = 1 << 1_000_000
    record.status_code = float("nan")
    output = ServiceFormatter().format(record)
    assert len(output.encode()) <= 8192 and "<Unprintable>" in output
    assert "<large-int>" in output and "<nonfinite-float>" in output
    record.msg = "%1000000000s"
    assert "width limit" in ServiceFormatter().format(record)
    record.msg = "%*s"
    record.args = (1000000000, "x")
    assert "width limit" in ServiceFormatter().format(record)
    record.msg, record.args = Unprintable(), ()
    assert "<Unprintable>" in ServiceFormatter().format(record)


def test_stack_locations_do_not_read_source(monkeypatch):
    import linecache

    def forbidden(*args, **kwargs):
        raise AssertionError("source lookup must not run")

    monkeypatch.setattr(linecache, "getline", forbidden)
    try:
        raise ValueError("private")
    except ValueError as exc:
        context = exception_context(exc)
    assert "test_stack_locations" in context["error_trace"]


def test_bidi_format_controls_cannot_disguise_log_text():
    record = logging.LogRecord(
        "worker", logging.WARNING, __file__, 1, "safe\u202eevil\u2066", (), None
    )
    output = ServiceFormatter().format(record)
    assert "\u202e" not in output and "\u2066" not in output
    assert r"\u202e" in output and r"\u2066" in output


def test_normal_parameterized_log_message_remains_readable():
    record = logging.LogRecord(
        "worker", logging.INFO, __file__, 1, "%s completed %d attempts", ("job", 7), None
    )
    assert "job completed 7 attempts" in ServiceFormatter().format(record)


def test_http_status_from_response_and_cause_omits_response_data():
    class Response:
        status_code = 503
        text = "secret-response-body"
        url = "https://user:secret@rpc.test/?key=secret"

        def __repr__(self):
            raise AssertionError("response repr must not run")

    cause = ValueError("secret-exception")
    cause.response = Response()
    outer = RuntimeError("secret-wrapper")
    outer.__cause__ = cause
    context = exception_context(outer)
    assert context["status_code"] == 503
    assert "secret" not in str(context)
