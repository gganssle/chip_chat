"""The Azure Functions host, read as text.

``api/functions/function_app.py`` imports ``azure.functions`` and reaches for
the Snowflake driver, and neither is in this workspace's lockfile -- deliberately,
for the reason ``api/functions/requirements.txt`` gives. So it is read here the
way ``databricks/tests/test_recommender.py`` reads its wrapper and
``infra/tests`` read the Terraform: as source, for the claims that can be
checked without a Functions worker.

What is checked is what a review would otherwise have to remember: that there
are four routes and no fifth, that the three preconditions are all present, that
the file writes no SQL of its own, and that the two runtime-only dependencies
stay out of the lockfile.
"""

import ast
import json
from pathlib import Path

import pytest

from chip_chat.api.ops import OPS_UNAVAILABLE_MESSAGE, SESSION_HEADER
from chip_chat.otel import OpsAction
from chip_chat.snowflake.procedures import IDENTITY_VARIABLE

_FUNCTIONS = Path(__file__).resolve().parents[1] / "functions"
_HOST = _FUNCTIONS / "function_app.py"


def source() -> str:
    """The host, as text."""
    return _HOST.read_text(encoding="utf-8")


def tree() -> ast.Module:
    """The host, parsed. A syntax error here is a deployment that never starts."""
    return ast.parse(source(), filename=str(_HOST))


def routes() -> dict[str, ast.FunctionDef]:
    """Every function carrying an ``@app.route`` decorator, by route name."""
    found: dict[str, ast.FunctionDef] = {}
    for node in tree().body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "route" and isinstance(keyword.value, ast.Constant):
                    found[str(keyword.value.value)] = node
    return found


# --- four routes, and no fifth ---------------------------------------------


def test_the_host_parses() -> None:
    assert tree().body


def test_there_is_one_route_per_write_action() -> None:
    assert set(routes()) == {action.value for action in OpsAction}


@pytest.mark.parametrize("action", list(OpsAction), ids=lambda a: a.value)
def test_every_route_is_a_post(action: OpsAction) -> None:
    """A write is not a GET. A GET is retried by things that do not ask first."""
    decorator = routes()[action.value].decorator_list[0]
    assert isinstance(decorator, ast.Call)
    methods = next(k for k in decorator.keywords if k.arg == "methods")
    assert ast.literal_eval(methods.value) == ("POST",)


def test_the_routes_carry_no_visitor_identifier_in_their_path() -> None:
    """Identity is a bound session, never a segment of a URL."""
    for route in routes():
        assert "{" not in route


# --- the three preconditions ------------------------------------------------


def test_the_ops_key_is_compared_in_constant_time() -> None:
    assert "hmac.compare_digest" in source()


def test_an_unset_ops_key_refuses_rather_than_allows() -> None:
    """The other way round is how a write path ends up open."""
    assert "return bool(expected) and hmac.compare_digest" in source()


def test_the_visitor_comes_off_the_session_header() -> None:
    assert "request.headers.get(SESSION_HEADER" in source()
    assert SESSION_HEADER not in source().replace("SESSION_HEADER", "")


def test_the_turn_is_rejoined_under_the_tool_span() -> None:
    """``ops.*`` is a child of ``tool.*``; a host that opened a root span would
    emit the write into a trace nobody will find."""
    assert "continue_turn(dict(request.headers), parent=SpanName.TOOL)" in source()


def test_a_missing_trace_context_refuses_the_write() -> None:
    text = source()
    assert "TRACE_CONTEXT_REQUIRED" in text
    assert text.index("except TurnContextError") < text.index("except OpsRejectedError")


# --- no SQL of its own ------------------------------------------------------


@pytest.mark.parametrize(
    "keyword", ["INSERT", "UPDATE ", "DELETE", "MERGE", "SELECT", "CREATE"]
)
def test_the_host_writes_no_sql(keyword: str) -> None:
    """Issue #63: each function calls its stored procedure -- no ad-hoc SQL."""
    assert keyword not in source()


def test_the_only_statement_is_a_procedure_call() -> None:
    assert 'f"CALL {procedure_name}' in source()


def test_the_procedure_shape_is_read_off_the_declaration() -> None:
    """Which arguments need ``PARSE_JSON`` is #46's answer, not this file's."""
    text = source()
    assert 'declared.sql_type == "VARIANT"' in text
    assert "declaration = procedure(procedure_name" in text


def test_the_session_variable_is_named_once_in_the_repository() -> None:
    """The host binds ``IDENTITY_VARIABLE``, never the literal it happens to be."""
    text = source()
    assert "SET {IDENTITY_VARIABLE}" in text
    assert f"SET {IDENTITY_VARIABLE}" not in text.replace("{IDENTITY_VARIABLE}", "")


def test_the_interpolated_identifier_is_allow_listed_first() -> None:
    """``SET`` takes no bound parameter, so the pattern is what makes it safe."""
    text = source()
    assert "_DEMO_ID.match(demo_id)" in text
    assert text.index("_DEMO_ID.match(demo_id)") < text.index("SET {IDENTITY_VARIABLE}")


# --- the failure RFC-001 section 10 gives copy to ---------------------------


def test_an_outage_answers_with_the_specified_message() -> None:
    assert "OPS_UNAVAILABLE_MESSAGE" in source()
    assert "503" in source()


def test_the_specified_message_is_not_written_out_a_second_time() -> None:
    """One definition. A second copy is a second sentence to keep in step."""
    assert OPS_UNAVAILABLE_MESSAGE not in source()


def test_a_rejection_is_answered_with_a_200() -> None:
    """``sql/12_procedures.sql``: a rejection is a returned object, not a fault."""
    assert "return _json(200, rejected.as_result())" in source()


def test_an_unassembled_service_is_an_outage_rather_than_a_crash() -> None:
    assert "raise OpsUnavailableError(str(failure)) from failure" in source()


# --- the identifiers it authenticates with ----------------------------------


def test_the_host_names_the_user_and_role_the_sql_creates() -> None:
    """Three users exist and they are not interchangeable."""
    text = source()
    sql = _FUNCTIONS.parents[1] / "snowflake" / "sql"
    users = (sql / "04_users.sql").read_text(encoding="utf-8")
    roles = (sql / "00_roles.sql").read_text(encoding="utf-8")

    assert 'OPS_USER: Final = "CHIP_CHAT_OPS"' in text
    assert "CREATE USER IF NOT EXISTS CHIP_CHAT_OPS" in users
    assert 'WRITE_ROLE: Final = "CHIP_CHAT_WRITE"' in text
    assert "CREATE ROLE IF NOT EXISTS CHIP_CHAT_WRITE" in roles


def test_the_host_does_not_run_on_the_publish_warehouse() -> None:
    """Only the nightly publish (#39) may name CHIP_CHAT_PUBLISH_WH."""
    assert '"CHIP_CHAT_PUBLISH_WH"' not in source()


def test_no_key_material_is_defaulted() -> None:
    """A private key with a fallback is a private key somebody committed."""
    assert 'os.environ["SNOWFLAKE_PRIVATE_KEY"]' in source()
    assert 'get("SNOWFLAKE_PRIVATE_KEY"' not in source()


# --- what the host installs, and what it does not ---------------------------


def test_the_runtime_only_dependencies_are_declared_for_the_host() -> None:
    requirements = (_FUNCTIONS / "requirements.txt").read_text(encoding="utf-8")
    assert "azure-functions" in requirements
    assert "snowflake-connector-python" in requirements


def test_the_functions_runtime_sdk_stays_out_of_the_workspace() -> None:
    """Every developer's virtualenv would otherwise carry it for one file.

    This used to be parametrized over ``azure-functions`` *and*
    ``snowflake-connector-python``, and the second half was retired by
    ``cc-lpy4`` rather than deleted quietly. The argument was never about the
    driver being unwelcome; it was that nothing in the workspace imported it, so
    a workspace dependency would have been every developer paying for a file no
    test ran. ``chip_chat.api.connect`` imports it now -- it is what
    ``build_service`` opens the read connection with -- and a dependency the
    deployed image needs and the manifest does not declare is a dependency that
    is missing. See ``docs/decisions/snowflake-connection-factory.md``.

    ``azure-functions`` is untouched: it is the Functions runtime's own SDK, it
    exists to be imported by a worker that installs
    ``api/functions/requirements.txt``, and no workspace package imports it.
    """
    root = _FUNCTIONS.parents[1]
    for manifest in root.glob("*/pyproject.toml"):
        assert "azure-functions" not in manifest.read_text(encoding="utf-8")


def test_the_driver_is_declared_by_the_one_package_that_imports_it() -> None:
    """`api/` and nowhere else: the ops host and the chat app are the two callers."""
    root = _FUNCTIONS.parents[1]
    declaring = {
        manifest.parent.name
        for manifest in root.glob("*/pyproject.toml")
        if "snowflake-connector-python" in manifest.read_text(encoding="utf-8")
    }

    assert declaring == {"api"}


def test_the_host_configuration_is_valid_json() -> None:
    settings = json.loads((_FUNCTIONS / "host.json").read_text(encoding="utf-8"))
    assert settings["version"] == "2.0"
