from runtime import _redact_access_path


def test_access_log_path_redacts_query_strings() -> None:
    assert (
        _redact_access_path("/api/codex/threads?cursor=secret")
        == "/api/codex/threads?<redacted>"
    )


def test_access_log_path_escapes_control_characters() -> None:
    assert (
        _redact_access_path("/api/codex/threads\nforged")
        == r"/api/codex/threads\nforged"
    )
