"""The publish declarations, checked against both ends of the hand-off.

`chip_chat.databricks.publish` is a list of eleven tables, two SQL statements
per table and a transport rule, consumed by a job that talks to two systems
neither of which exists in CI. So the assertions here are the ones
`test_gold.py` makes, aimed at a seam rather than at a layer.

The first kind is agreement. The publish says a table has these columns;
`chip_chat.snowflake.schema` -- the other transcription of RFC-001 §04, made on
the serving side -- independently says the same, and so do the Unity Catalog
layout, the Terraform and the notebooks. `publish.py` may not import a sibling
package, which is what makes the agreement worth asserting rather than
assuming.

The second kind is the properties that make the hand-off safe to run against a
live serving layer without running it: the swap is one statement and it is
``INSERT OVERWRITE``; every column list is written out rather than positional;
nothing is written outside the staging schema; the two halves of every transport
are inverses; and no projection recomputes a timestamp it is supposed to be
carrying. That last one is RFC-001 §10 turned into a test -- a publish that
restamped `derived_at` would present a mart republished from an unchanged gold
table as fresh, which is the one outcome §10 names.

The two things these cannot check are the live accounts and the connector's own
reading of the SQL, and that is what `databricks/notebooks/publish_verify.py`
is for.
"""

import re
from pathlib import Path

import pytest

from chip_chat.databricks import catalog, gold, publish, silver
from chip_chat.snowflake import account, schema

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "databricks" / "notebooks" / "snowflake_publish.py"
VERIFY = REPO / "databricks" / "notebooks" / "publish_verify.py"
TERRAFORM = REPO / "infra" / "terraform" / "databricks_publish.tf"
GRANTS = REPO / "snowflake" / "sql" / "03_grants.sql"
DATABASE_SQL = REPO / "snowflake" / "sql" / "02_database.sql"

IDS = [candidate.table for candidate in publish.TARGETS]


def resolve(layer: str, stream: str, table: str) -> str:
    """Qualify a lakehouse table the way the notebook does."""
    return f"{catalog.CATALOG}.{layer}_{stream}.{table}"


def code(notebook: str) -> str:
    """Return a notebook with its markdown cells removed.

    The prose in these notebooks argues with the reader and several paragraphs
    name the thing the code deliberately does *not* do, so a check that reads
    the text has to read the code rather than the argument around it.
    """
    return "\n".join(
        line for line in notebook.splitlines() if not line.startswith("# MAGIC")
    )


# --- Agreement with the serving layer ----------------------------------------


def test_every_published_table_is_one_snowflake_declares() -> None:
    """A target with no table on the far side lands rows into nothing."""
    declared = {table.name for table in schema.TABLES}
    for candidate in publish.TARGETS:
        assert candidate.table in declared, (
            f"{candidate.table} is published and chip_chat.snowflake.schema "
            "does not declare it"
        )


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_columns_are_the_ones_the_serving_table_declares(
    candidate: publish.Target,
) -> None:
    """The transcription, held to the other transcription.

    `sql/08_marts.sql`'s header argues for the duplication: the producer holds
    itself to §04 in `make ci` and so does the consumer, and a rename that only
    one of them made is a failing test on both sides rather than a nightly job
    that lands a column into nothing.
    """
    assert publish.column_names(candidate) == schema.columns_of(candidate.table)


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_table_is_published_into_the_schema_that_holds_it(
    candidate: publish.Target,
) -> None:
    assert candidate.schema == schema.table(candidate.table).schema
    assert candidate.qualified == schema.table(candidate.table).qualified()


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_required_columns_are_the_ones_declared_not_null(
    candidate: publish.Target,
) -> None:
    """The pre-flight check and the target's own constraint are one list.

    The notebook counts nulls in these before it writes anything. A column the
    target requires and this list forgets is a publish that fails half way
    through a swap with a message naming neither the table nor the column.
    """
    declared = tuple(
        column.name for column in schema.table(candidate.table).columns if column.required
    )
    assert publish.required_columns(candidate) == declared


def test_every_serving_table_is_published_or_argued_for() -> None:
    """Seventeen tables exist and eleven are published. The six are the point.

    `demo_visitors` holds all three columns a visitor may edit and is the one
    account table a visitor writes to, so a nightly overwrite would delete every
    edit made that day; `personas` and `persona_fixtures` are reference rows the
    generator emits once. All three reach Snowflake through
    `chip_chat.snowflake.load`, run as CHIP_CHAT_ADMIN.

    Three more arrived with #46's write path and each is unpublished for its own
    reason:

    `action_receipts` is the retry-key store, written live by the four write
    procedures and by nothing else. It is `demo_visitors`' argument in its
    strongest form -- an overnight overwrite would forget which keys a visitor
    had already spent, and the next retry of a call made before the job ran
    would place a second real order. Nothing in the lakehouse could produce its
    rows in any case: they are a record of what the serving layer did.

    `rewards` and `rewards_terms` are harvested catalogue data and *should*
    cross this seam eventually, which is cc-99cn. They are not `Target`s yet
    because a target names a silver table to read: the harvest produces the
    rewards line-up, and the four published numbers in `rewards_terms` are
    parsed out of the terms text by `chip_chat.data_gen.rewards` rather than
    conformed into a silver table anybody could select from. A target pointing
    at a table that does not exist is a nightly job that fails, which is worse
    than a table that is honestly empty -- and `place_order` refuses to accrue
    while it is empty rather than guessing, so the emptiness is loud.
    """
    published = {candidate.table for candidate in publish.TARGETS}
    unpublished = {table.name for table in schema.TABLES} - published
    assert unpublished == {
        "demo_visitors",
        "personas",
        "persona_fixtures",
        "action_receipts",
        "rewards",
        "rewards_terms",
    }


def test_the_account_tables_are_the_tables_the_marts_came_from() -> None:
    """What the publish carries out of ACCOUNTS is `MART_INPUTS`, exactly."""
    assert publish.ACCOUNT_TABLES == schema.MART_INPUTS
    assert publish.ACCOUNT_TABLES == account.PUBLISHED_ACCOUNT_TABLES


def test_nothing_editable_is_ever_published() -> None:
    """No projection may carry a column a visitor owns.

    The containment RFC-001 §04 rests its answer to PRD Q2 on works only while
    the editable fields are somewhere nothing overwrites. A publish that carried
    `stated_preferences` would put a visitor's own text back to the generator's
    every night.
    """
    for candidate in publish.TARGETS:
        for editable in schema.EDITABLE_COLUMNS:
            assert editable not in publish.column_names(candidate), (
                f"{candidate.table} publishes {editable}, which a visitor edits"
            )


def test_the_publisher_may_write_everything_it_publishes() -> None:
    """Every target is one CHIP_CHAT_PUBLISH holds mutating privileges on.

    Two of the three schemas are open to it and the third is closed with three
    tables named. This is the check that the eleven targets and that boundary
    are the same eleven tables.
    """
    for candidate in publish.TARGETS:
        assert account.may_write_table(
            publish.PUBLISH_ROLE,
            candidate.schema,  # type: ignore[arg-type]
            candidate.table,
        ), f"{publish.PUBLISH_ROLE} cannot write {candidate.qualified}"


def test_the_publisher_cannot_reach_the_table_the_editable_columns_live_on() -> None:
    """The containment, asked of the grants rather than of the projections."""
    assert not account.may_write_table(publish.PUBLISH_ROLE, "ACCOUNTS", "demo_visitors")
    assert not account.access(publish.PUBLISH_ROLE, "ACCOUNTS").read


# --- Agreement with the lakehouse --------------------------------------------


def test_every_source_is_a_table_the_medallion_actually_builds() -> None:
    """A projection reading a table nothing conforms is a job that fails on the
    cluster rather than here."""
    conformed = {(table.stream, table.name) for table in silver.TABLES}
    computed = {(mart.stream, mart.name) for mart in gold.MARTS}
    for candidate in publish.TARGETS:
        key = (candidate.stream, candidate.table)
        built = conformed if candidate.layer == silver.LAYER else computed
        assert key in built, (
            f"{candidate.table} is published from {candidate.layer}_{candidate.stream} "
            "and nothing builds it there"
        )


def test_the_layers_and_streams_are_the_ones_unity_catalog_has() -> None:
    """`publish.py` may not import `catalog`, so the two agree by assertion."""
    for candidate in publish.TARGETS:
        assert candidate.layer in catalog.LAYERS
        assert candidate.stream in catalog.STREAMS


def test_the_catalogue_is_published_from_silver_and_never_from_bronze() -> None:
    """Bronze is what arrived; silver is what is true. A serving layer fed from
    bronze would serve the parse nobody conformed."""
    for candidate in publish.TARGETS:
        assert candidate.layer != "bronze"
    assert {candidate.layer for candidate in publish.targets_in("CATALOGUE")} == {
        silver.LAYER
    }
    assert {candidate.layer for candidate in publish.targets_in("MARTS")} == {gold.LAYER}


@pytest.mark.parametrize(
    "candidate", list(publish.targets_in("MARTS")), ids=[m.name for m in gold.MARTS]
)
def test_every_mart_column_published_is_one_the_mart_computes(
    candidate: publish.Target,
) -> None:
    """The publish cannot carry a column gold does not produce."""
    computed = set(gold.column_names(gold.mart(candidate.table)))
    assert set(publish.column_names(candidate)) <= computed


def test_the_four_marts_are_the_four_the_rfc_names() -> None:
    published = tuple(candidate.table for candidate in publish.targets_in("MARTS"))
    assert published == tuple(gold.RFC_COLUMNS)


def test_the_recommendations_table_is_not_published() -> None:
    """#37's fifth gold table has no serving table to land in.

    RFC-001 §04 fixes four marts and `CHIP_CHAT.MARTS` holds four. Publishing it
    would mean adding a table to the serving schema, which is a decision about
    where `get_recommendations` reads from rather than a line in this list.
    """
    assert "recommendations" not in {c.table for c in publish.TARGETS}


# --- Order --------------------------------------------------------------------


def test_the_publish_order_is_the_order_the_ddl_creates_the_tables_in() -> None:
    """A run killed halfway leaves a consistent set of generations.

    Snowflake enforces no foreign key, so the order buys nothing at write time.
    What it buys is that the catalogue lands before the order lines that name
    items in it, rather than lines pointing at items that have not arrived.
    """
    declared = [table.name for table in schema.TABLES]
    published = [candidate.table for candidate in publish.TARGETS]
    assert published == [name for name in declared if name in set(published)]


def test_no_two_targets_share_a_table_or_a_staging_table() -> None:
    tables = [candidate.qualified for candidate in publish.TARGETS]
    staged = [publish.staging(candidate) for candidate in publish.TARGETS]
    assert len(set(tables)) == len(tables)
    assert len(set(staged)) == len(staged)


def test_target_lookup_refuses_a_name_nothing_publishes() -> None:
    with pytest.raises(KeyError, match="is not published"):
        publish.target("demo_visitors")


# --- The transports -----------------------------------------------------------


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_every_column_declares_a_transport_that_exists(
    candidate: publish.Target,
) -> None:
    for column in candidate.columns:
        assert column.transport in publish.TRANSPORTS


def test_a_column_with_an_invented_transport_is_refused() -> None:
    """Both halves refuse it, rather than one emitting a bare column name and
    landing a Spark type nobody chose into a Snowflake column somebody did."""
    invented = publish.Target(
        table="invented",
        schema="MARTS",
        layer="gold",
        stream="synthetic",
        columns=(publish.Column("demo_id", transport="telepathy"),),
        key=("demo_id",),
        why="never declared",
    )
    with pytest.raises(ValueError, match="telepathy"):
        publish.select(invented, resolve)
    with pytest.raises(ValueError, match="telepathy"):
        publish.swap(invented)


def test_every_array_column_crosses_as_json() -> None:
    """The four ARRAY columns, found from the serving schema rather than listed.

    A fifth array column added to the DDL and left on the DIRECT transport would
    reach the connector as a Spark array and land as whatever it decided, which
    is the kind of thing that works until it does not.
    """
    for candidate in publish.TARGETS:
        declared = {
            column.name: column.sql_type
            for column in schema.table(candidate.table).columns
        }
        for column in candidate.columns:
            if declared[column.name] == "ARRAY":
                assert column.transport == publish.JSON_ARRAY, (
                    f"{candidate.table}.{column.name} is an ARRAY in Snowflake "
                    f"and crosses as {column.transport}"
                )


def test_every_timestamp_column_crosses_as_a_formatted_string() -> None:
    """The connector maps a Spark timestamp onto TIMESTAMP_LTZ, which is zoned.

    Landing one in a TIMESTAMP_NTZ column applies whatever the session's
    timezone happens to be, and every timestamp in this database is UTC and
    carries no zone. So they cross as text with an explicit format at both ends.
    """
    for candidate in publish.TARGETS:
        declared = {
            column.name: column.sql_type
            for column in schema.table(candidate.table).columns
        }
        for column in candidate.columns:
            if declared[column.name] == "TIMESTAMP_NTZ":
                assert column.transport == publish.UTC_TIMESTAMP


def test_the_two_timestamp_formats_are_one_format_written_twice() -> None:
    """Spark's pattern and Snowflake's, field for field.

    They are two spellings of the same thing and there is no test a computer can
    run that compares them directly, so this compares the shape: same
    separators, same order, six digits of fraction on both sides.
    """
    spark = publish.SPARK_TIMESTAMP_FORMAT
    snowflake = publish.SNOWFLAKE_TIMESTAMP_FORMAT
    assert spark == "yyyy-MM-dd HH:mm:ss.SSSSSS"
    assert snowflake == "YYYY-MM-DD HH24:MI:SS.FF6"
    assert re.sub(r"[A-Za-z0-9]", "", spark) == re.sub(r"[A-Za-z0-9]", "", snowflake)
    assert spark.count("S") == 6
    assert snowflake.endswith("FF6")


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_two_halves_of_a_transport_are_inverses(
    candidate: publish.Target,
) -> None:
    """Whatever Spark wraps a column in, Snowflake unwraps.

    A column that is transformed on one side and copied on the other is the
    failure this pairing exists to make impossible: `to_json` without
    `PARSE_JSON` lands a JSON string in an ARRAY column, and `PARSE_JSON`
    without `to_json` fails on a value that was never text.
    """
    read = publish.select(candidate, resolve)
    written = publish.swap(candidate)
    for column in candidate.columns:
        if column.transport == publish.JSON_ARRAY:
            assert f"to_json({column.name})" in read
            assert f"PARSE_JSON({column.name})::ARRAY" in written
        elif column.transport == publish.UTC_TIMESTAMP:
            assert f"date_format({column.name}, " in read
            assert f"TO_TIMESTAMP_NTZ({column.name}, " in written
        else:
            assert f"to_json({column.name})" not in read
            assert f"PARSE_JSON({column.name})" not in written


# --- The statements -----------------------------------------------------------


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_projection_reads_the_table_it_declares(
    candidate: publish.Target,
) -> None:
    read = publish.select(candidate, resolve)
    assert resolve(candidate.layer, candidate.stream, candidate.table) in read
    assert read.startswith("SELECT")


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_projection_produces_every_column_and_no_others(
    candidate: publish.Target,
) -> None:
    read = publish.select(candidate, resolve)
    body = read.split("FROM")[0]
    produced = tuple(
        part.strip().rsplit(" AS ", 1)[-1].strip()
        for part in body.removeprefix("SELECT").split(",\n")
    )
    assert produced == publish.column_names(candidate)


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_swap_is_one_insert_overwrite(candidate: publish.Target) -> None:
    """One statement, and the specific one.

    The connector's `Utils.runQuery` opens its own JDBC connection per call, so
    a BEGIN in one call is not the session the next call lands in -- a
    multi-statement transaction cannot be spread across two of them. INSERT
    OVERWRITE truncates and inserts inside a single transaction on its own,
    which is what makes a mid-publish read see one generation or the other.
    """
    written = publish.swap(candidate)
    assert written.startswith(f"INSERT OVERWRITE INTO {candidate.qualified} (")
    assert ";" not in written
    assert "BEGIN" not in written
    assert "COMMIT" not in written
    assert written.count("INSERT") == 1


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_swap_never_drops_or_renames_the_target(
    candidate: publish.Target,
) -> None:
    """Everything hanging off the target survives a publish.

    #43's row access policy, #45's column comments and the declared keys are all
    attached to the table object. A publish that replaced the object would take
    them with it, and a silently detached row access policy is a breach nobody
    sees.
    """
    written = publish.swap(candidate)
    for destructive in ("DROP", "SWAP WITH", "ALTER TABLE", "CREATE OR REPLACE"):
        assert destructive not in written


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_swap_names_every_column_rather_than_relying_on_position(
    candidate: publish.Target,
) -> None:
    """A column added to the middle of the serving table by a CREATE OR ALTER
    would shift every value one place to the right in a positional insert, and
    land a price in a calorie count."""
    written = publish.swap(candidate)
    listed = written.split("(", 1)[1].split(")", 1)[0]
    assert tuple(part.strip() for part in listed.split(",")) == publish.column_names(
        candidate
    )


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_the_swap_reads_the_staging_table_and_writes_the_serving_one(
    candidate: publish.Target,
) -> None:
    written = publish.swap(candidate)
    assert publish.staging(candidate) in written
    assert written.index(candidate.qualified) < written.index(publish.staging(candidate))


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_a_staging_table_lives_in_the_staging_schema_and_says_whose_it_is(
    candidate: publish.Target,
) -> None:
    """Never beside its target: CHIP_CHAT_READ holds SELECT ON FUTURE TABLES in
    all three serving schemas, so an incoming generation parked in one of them
    would be an unscoped copy of a visitor-scoped table with no policy on it."""
    staged = publish.staging(candidate)
    assert staged.startswith(f"{publish.DATABASE}.{publish.STAGING_SCHEMA}.")
    assert candidate.schema in staged
    assert candidate.table.upper() in staged
    assert publish.drop_staging(candidate).endswith(staged)


def test_the_staging_schema_is_the_one_snowflake_creates() -> None:
    assert publish.STAGING_SCHEMA == account.STAGING_SCHEMA
    assert publish.DATABASE == account.DATABASE
    assert publish.PUBLISH_ROLE in account.LANE_ROLES
    assert publish.PUBLISH_WAREHOUSE == account.PUBLISH_WAREHOUSE


# --- derived_at, which is RFC-001 §10 -----------------------------------------


@pytest.mark.parametrize("candidate", publish.TARGETS, ids=IDS)
def test_no_projection_computes_a_timestamp_it_is_meant_to_be_carrying(
    candidate: publish.Target,
) -> None:
    """The one rule that makes a stale mart honest.

    RFC-001 §10 asks for a failed nightly job to serve stale marts WITH their
    timestamp and never silently as fresh. A publish that stamped
    `current_timestamp()` into `derived_at` would make a mart republished from
    an unchanged gold table look recomputed tonight -- which is exactly the
    outcome §10 names, arriving through the mechanism meant to prevent it.
    """
    both = publish.select(candidate, resolve) + publish.swap(candidate)
    for clock in ("current_timestamp", "now()", "current_date", "CURRENT_TIMESTAMP"):
        assert clock not in both
    for carried in publish.CARRIED_ONLY:
        if carried in publish.column_names(candidate):
            assert f"date_format({carried}, " in publish.select(candidate, resolve)


def test_every_mart_carries_derived_at() -> None:
    """A mart with nowhere to put a timestamp cannot be served stale honestly."""
    for candidate in publish.targets_in("MARTS"):
        assert "derived_at" in publish.column_names(candidate)


def test_every_harvested_table_carries_the_date_its_rows_were_fetched() -> None:
    """`harvested_at` is the catalogue's version of the same promise, and
    RFC-001 §08 requires a quoted price to have a date on it."""
    for candidate in publish.targets_in("CATALOGUE"):
        columns = publish.column_names(candidate)
        if "source_url" in columns:
            assert "harvested_at" in columns


# --- The connection -----------------------------------------------------------


def test_the_connector_is_pointed_at_the_publish_lane() -> None:
    options = publish.options("acct.snowflakecomputing.com", "CHIP_CHAT_PUBLISHER", "S")
    assert options["sfRole"] == publish.PUBLISH_ROLE
    assert options["sfWarehouse"] == publish.PUBLISH_WAREHOUSE
    assert options["sfDatabase"] == publish.DATABASE
    assert options["sfTimezone"] == publish.SPARK_TIMEZONE


def test_the_options_never_carry_a_credential() -> None:
    """The private key is not a parameter and is not returned.

    The notebook adds `pem_private_key` at the point of use, so nothing here and
    nothing a failing test prints has ever held one.
    """
    options = publish.options("acct.snowflakecomputing.com", "CHIP_CHAT_PUBLISHER", "S")
    for name in options:
        assert "key" not in name.lower()
        assert "password" not in name.lower()


@pytest.mark.parametrize("missing", ["url", "user", "schema"])
def test_a_blank_connection_detail_is_refused(missing: str) -> None:
    """A connector handed a blank URL fails minutes later with a JDBC error that
    names neither the job nor the missing value."""
    arguments = {"url": "acct", "user": "CHIP_CHAT_PUBLISHER", "schema_name": "S"}
    arguments["schema_name" if missing == "schema" else missing] = ""
    with pytest.raises(ValueError, match=missing):
        publish.options(**arguments)


# --- What a run cost ----------------------------------------------------------


def test_a_publish_shorter_than_the_minimum_is_billed_the_minimum() -> None:
    """Snowflake bills at least sixty seconds per warehouse resume, and an
    eleven-table publish can finish inside that. An estimate without the floor
    would report less than the account is charged."""
    assert publish.warehouse_credits(1) == publish.warehouse_credits(
        publish.WAREHOUSE_MINIMUM_SECONDS
    )
    assert publish.warehouse_credits(3600) == pytest.approx(
        publish.WAREHOUSE_CREDITS_PER_HOUR
    )
    assert publish.warehouse_credits(7200) > publish.warehouse_credits(3600)


def test_a_negative_run_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be awake"):
        publish.warehouse_credits(-1)


# --- The notebooks ------------------------------------------------------------


def test_the_notebook_writes_nothing_outside_the_staging_schema() -> None:
    """Every connector write in the job is aimed at a staging table.

    The serving tables are reached by fully qualified name inside the swap, and
    never as a session default -- a default that is correct today is a default
    somebody relies on tomorrow.
    """
    source = code(NOTEBOOK.read_text())
    assert 'option("dbtable", staging)' in source
    assert source.count('option("dbtable"') == 1
    assert "publish.options(URL, USER, publish.STAGING_SCHEMA)" in source


def test_the_notebook_checks_the_clock_before_it_moves_anything() -> None:
    """The UTC_TIMESTAMP transport is only correct while Spark renders UTC."""
    source = code(NOTEBOOK.read_text())
    assert "spark.sql.session.timeZone" in source
    assert "publish.SPARK_TIMEZONE" in source
    assert source.index("spark.sql.session.timeZone") < source.index("publish_one")


def test_the_notebook_refuses_to_publish_an_empty_source() -> None:
    """Publishing an empty silver table would empty the serving layer for every
    conversation until tomorrow night."""
    source = code(NOTEBOOK.read_text())
    assert "if rows == 0:" in source


def test_the_notebook_drops_a_staging_table_only_after_its_swap() -> None:
    """A staging table still sitting in CHIP_CHAT.STAGING is a run that did not
    finish, and is worth being able to look inside."""
    source = code(NOTEBOOK.read_text())
    assert source.index("publish.swap(target)") < source.index(
        "publish.drop_staging(target)"
    )


def test_the_notebook_never_prints_the_credential() -> None:
    source = code(NOTEBOOK.read_text()) + code(VERIFY.read_text())
    for line in source.splitlines():
        if line.strip().startswith("print("):
            assert "PRIVATE_KEY" not in line
            assert "pem_private_key" not in line


def test_the_verify_notebook_checks_every_acceptance_criterion() -> None:
    """Four of #39's five are claims about a live system; the fifth is a
    measurement the publish job emits."""
    source = code(VERIFY.read_text())
    assert "COUNT(*)" in source
    assert "HAVING COUNT(*) > 1" in source  # a partially truncated table
    assert "INFORMATION_SCHEMA.TABLES" in source  # a staging table left behind
    assert "email_notifications" in source  # the alert reaches a human
    assert "quartz_cron_expression" in source  # the publish is scheduled
    assert "derived_at IS NULL" in source


def test_both_notebooks_read_the_key_out_of_a_scope_rather_than_a_widget() -> None:
    """A secret passed as a job parameter is a secret in the run's own history."""
    for notebook in (NOTEBOOK, VERIFY):
        source = code(notebook.read_text())
        assert "dbutils.secrets.get" in source
        assert "publish.PRIVATE_KEY_SECRET" in source
        assert 'widgets.text("private_key' not in source


# --- The Terraform ------------------------------------------------------------


def test_the_terraform_uploads_the_module_the_notebooks_import() -> None:
    source = TERRAFORM.read_text()
    assert "databricks/src/chip_chat/databricks/publish.py" in source
    assert "databricks/notebooks/snowflake_publish.py" in source
    assert "databricks/notebooks/publish_verify.py" in source


def test_the_publish_job_is_scheduled_and_ships_paused() -> None:
    """Declared with a cron a person can read, and not firing until somebody
    turns it on. `databricks_recommender.tf` argues the arrangement in full."""
    source = TERRAFORM.read_text()
    assert "quartz_cron_expression = var.databricks_publish_cron" in source
    assert 'databricks_publish_schedule_enabled ? "UNPAUSED" : "PAUSED"' in source


def test_the_job_alerts_a_human_when_a_run_fails() -> None:
    """RFC-001 §10 requires the alert and #39 requires it in this ticket.

    On the job rather than in the notebook: a run that dies before reaching any
    line of `snowflake_publish.py` is exactly the run nobody hears about
    otherwise.
    """
    source = TERRAFORM.read_text()
    assert "email_notifications" in source
    assert "on_failure = [var.databricks_publish_alert_email]" in source


def test_the_job_runs_on_a_single_node_cluster_that_stops() -> None:
    """The cost guardrail every job in this directory is held to: single-node,
    job compute, never an always-on all-purpose cluster."""
    source = TERRAFORM.read_text()
    assert "num_workers   = 0" in source
    assert "databricks_cluster_policy.job_single_node.id" in source
    assert "timeout_seconds     = var.databricks_publish_timeout_seconds" in source
    assert "existing_cluster_id" not in source


def test_the_secret_scope_is_created_empty() -> None:
    """No private key enters Terraform state, which is the argument
    `sql/04_users.sql` makes about RSA_PUBLIC_KEY and the checked-in SQL."""
    source = TERRAFORM.read_text()
    assert "databricks_secret_scope" in source
    assert "databricks_secret " not in source
    assert 'permission = "READ"' in source


def test_the_scope_name_is_the_one_the_module_defaults_to() -> None:
    variables = (REPO / "infra" / "terraform" / "variables.tf").read_text()
    assert f'default     = "{publish.SECRET_SCOPE}"' in variables
    assert publish.PRIVATE_KEY_SECRET in variables


# --- The Snowflake side -------------------------------------------------------


def test_the_staging_schema_is_created_and_granted_to_one_role() -> None:
    created = DATABASE_SQL.read_text()
    granted = GRANTS.read_text()
    assert f"CREATE SCHEMA IF NOT EXISTS CHIP_CHAT.{publish.STAGING_SCHEMA}" in created
    for role in account.LANE_ROLES:
        line = f"IN SCHEMA CHIP_CHAT.{publish.STAGING_SCHEMA}   TO ROLE {role}"
        if role == publish.PUBLISH_ROLE:
            assert line in granted
        else:
            assert line not in granted


def test_the_three_account_tables_are_granted_by_name() -> None:
    """And the other three are not granted at all."""
    granted = GRANTS.read_text()
    for table_name in publish.ACCOUNT_TABLES:
        assert f"ON TABLE CHIP_CHAT.ACCOUNTS.{table_name}" in granted, (
            f"nothing grants the publisher {table_name}"
        )
    for table_name in ("demo_visitors", "personas", "persona_fixtures"):
        assert f"ON TABLE CHIP_CHAT.ACCOUNTS.{table_name}" not in granted
