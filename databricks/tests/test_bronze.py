"""The bronze declarations, checked against everything they have to agree with.

`chip_chat.databricks.bronze` is a table of paths, formats and reader options
that a pipeline consumes on a cluster nobody runs in CI. So the assertions here
are all of the same kind: the declaration says X, and something else in this
repository — the harvest's landing-zone prefixes, the generator's table list,
the Unity Catalog layout, the Terraform, the notebook — independently says X
too. A drift between any pair of them is a pipeline that fails minutes into an
update with a message about a missing path, or worse, one that succeeds and
lands nothing.

Two of these are worth reading twice.

`test_the_synthetic_sources_are_exactly_the_generators_tables` is the one that
catches a table added to `data-gen/` and forgotten here: the generator would
write it, and bronze would silently not ingest it.

`test_a_jsonl_table_is_one_object_per_line_and_a_manifest_is_not` checks the
only reader option in this module whose mistake is silent. `multiLine` set
wrongly does not error — a multi-line document read line by line simply becomes
an entire file of rescued data — so it is checked against bytes the real writer
produced rather than against a comment.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from chip_chat.data_gen.records import DEFAULT_PREFIX as SYNTHETIC_PREFIX
from chip_chat.data_gen.records import TABLES as SYNTHETIC_TABLES
from chip_chat.databricks import bronze, catalog
from chip_chat.harvest.analysis import AnalysisCache
from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.sources.chipotle.tables import write_tables

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "databricks" / "notebooks" / "bronze_ingest.py"
VERIFY = REPO / "databricks" / "notebooks" / "bronze_verify.py"
TERRAFORM = REPO / "infra" / "terraform" / "databricks_bronze.tf"

CHECKPOINTS = "abfss://lakehouse@example.dfs.core.windows.net/_autoloader"
RAW = "abfss://raw@example.dfs.core.windows.net"


@dataclass(frozen=True, slots=True)
class _Row:
    """A stand-in table row, so the writer under test needs no real population."""

    order_id: str


# --- Agreement with the Unity Catalog layout --------------------------------


def test_the_streams_are_the_ones_unity_catalog_has() -> None:
    """`bronze.STREAMS` is a copy, because bronze.py may not import a sibling."""
    assert bronze.STREAMS == catalog.STREAMS


def test_bronze_is_a_layer_of_the_medallion() -> None:
    assert bronze.LAYER in catalog.LAYERS


@pytest.mark.parametrize("stream", catalog.STREAMS)
def test_the_schema_name_is_the_one_terraform_created(stream: catalog.Stream) -> None:
    assert bronze.schema_name(stream) == catalog.schema("bronze", stream).name


def test_an_unknown_stream_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        bronze.schema_name("real")


# --- Agreement with the landing zone ----------------------------------------


def test_raw_documents_reads_the_fetch_once_caches_index() -> None:
    """The pointers carrying source_url and harvested_at, and nothing else."""
    cache = DocumentCache(InMemoryBlobStore())
    assert cache.index_prefix.rstrip("/") == bronze.source("raw_documents").path


def test_raw_bodies_reads_the_content_addressed_blobs() -> None:
    cache = DocumentCache(InMemoryBlobStore())
    key = cache.content_key("a" * 64)
    assert key.startswith(bronze.source("raw_bodies").path + "/")


def test_document_analyses_reads_the_analysis_cache() -> None:
    analyses = AnalysisCache(InMemoryBlobStore())
    key = analyses.key("a" * 64, "prebuilt-layout", "2024-11-30")
    assert key.startswith(bronze.source("document_analyses").path + "/")


def test_the_synthetic_sources_are_exactly_the_generators_tables() -> None:
    """A table added to data-gen/ and not here would be written and not read."""
    ingested = {
        candidate.table
        for candidate in bronze.sources_for("synthetic")
        if candidate.table != "population_manifest"
    }
    assert ingested == set(SYNTHETIC_TABLES)


def test_every_synthetic_source_globs_a_file_the_generator_writes() -> None:
    written = {f"{name}.jsonl" for name in SYNTHETIC_TABLES} | {"manifest.json"}
    for candidate in bronze.sources_for("synthetic"):
        assert candidate.path == SYNTHETIC_PREFIX
        assert candidate.glob in written


def test_a_jsonl_table_is_one_object_per_line_and_a_manifest_is_not() -> None:
    """The one option whose mistake is silent, checked against real bytes.

    `write_tables` is what the generator and every parser write through. A
    `.jsonl` table is one compact object per line and must be read with
    `multiLine` off; the manifest beside it is indented and must be read with
    it on. Reading either the wrong way does not error — it produces a file of
    rescued data — so the two are asserted from the writer's own output.
    """
    blobs = InMemoryBlobStore()
    rows = [_Row(order_id="ord-0000001"), _Row(order_id="ord-0000002")]
    written = write_tables(blobs, "sample", [("orders", rows)], {"rows": 2, "seed": 7})

    table = blobs.read(written["orders"])
    assert table is not None
    assert len(table.decode().strip().splitlines()) == len(rows)
    assert json.loads(table.decode().splitlines()[0])["order_id"] == "ord-0000001"

    manifest = blobs.read(written["manifest"])
    assert manifest is not None
    assert len(manifest.decode().splitlines()) > 1

    for candidate in bronze.sources_for("synthetic"):
        assert candidate.multiline is (candidate.glob == "manifest.json")


# --- The declarations themselves --------------------------------------------


def test_every_source_belongs_to_a_known_stream() -> None:
    for candidate in bronze.SOURCES:
        assert candidate.stream in bronze.STREAMS


def test_table_names_are_unique_within_a_schema() -> None:
    for stream in bronze.STREAMS:
        names = [candidate.table for candidate in bronze.sources_for(stream)]
        assert len(names) == len(set(names))


def test_no_source_is_called_quarantine() -> None:
    """The quarantine view lives in the same schema and would collide."""
    assert bronze.QUARANTINE_TABLE not in {c.table for c in bronze.SOURCES}


def test_both_streams_are_ingested() -> None:
    """ "Both streams" is the whole of the issue's title."""
    for stream in bronze.STREAMS:
        assert list(bronze.sources_for(stream))


def test_the_harvested_corpus_is_the_three_things_the_issue_names() -> None:
    """HTML/JSON responses, Document Intelligence extractions, PDFs."""
    assert {candidate.table for candidate in bronze.sources_for("harvested")} == {
        "raw_documents",
        "raw_bodies",
        "document_analyses",
    }


def test_the_bodies_are_read_as_bytes_and_nothing_else_is() -> None:
    binary = [c for c in bronze.SOURCES if not c.is_parsed]
    assert [c.table for c in binary] == ["raw_bodies"]


def test_source_lookup_refuses_a_table_nothing_produces() -> None:
    with pytest.raises(KeyError, match="no bronze source produces"):
        bronze.source("menu_items")


def test_sources_for_refuses_an_unknown_stream() -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        list(bronze.sources_for("harvestd"))


# --- Reader options ---------------------------------------------------------


def test_every_table_gets_its_own_schema_location() -> None:
    """Two tables sharing one would each believe they had read the other's
    files, and the second would land empty rather than fail."""
    locations = [
        bronze.schema_location(CHECKPOINTS, candidate) for candidate in bronze.SOURCES
    ]
    assert len(locations) == len(set(locations))


def test_the_schema_location_is_under_the_schema_and_the_table() -> None:
    candidate = bronze.source("orders")
    assert bronze.schema_location(CHECKPOINTS, candidate) == (
        f"{CHECKPOINTS}/bronze_synthetic/orders"
    )


@pytest.mark.parametrize(
    "uri",
    ["wasbs://raw@example.blob.core.windows.net", "/mnt/raw", "s3://raw"],
)
def test_only_the_dfs_endpoint_is_accepted(uri: str) -> None:
    """wasbs:// reaches the same bytes without the hierarchical namespace."""
    with pytest.raises(ValueError, match="abfss://"):
        bronze.landing_path(uri, bronze.SOURCES[0])
    with pytest.raises(ValueError, match="abfss://"):
        bronze.schema_location(uri, bronze.SOURCES[0])


def test_the_landing_path_joins_without_doubling_the_slash() -> None:
    candidate = bronze.source("raw_documents")
    assert bronze.landing_path(RAW + "/", candidate) == f"{RAW}/raw/index"


def test_new_columns_are_tolerated_on_every_parsed_source() -> None:
    for candidate in bronze.SOURCES:
        if not candidate.is_parsed:
            continue
        options = bronze.autoloader_options(candidate, checkpoint_uri=CHECKPOINTS)
        assert options["cloudFiles.schemaEvolutionMode"] == "addNewColumns"


def test_the_binary_source_evolves_not_at_all() -> None:
    """`binaryFile` refuses every other mode, and its schema is four columns."""
    options = bronze.autoloader_options(
        bronze.source("raw_bodies"), checkpoint_uri=CHECKPOINTS
    )
    assert options["cloudFiles.schemaEvolutionMode"] == "none"


def test_a_parsed_source_rescues_rather_than_coerces() -> None:
    options = bronze.autoloader_options(
        bronze.source("raw_documents"), checkpoint_uri=CHECKPOINTS
    )
    assert options["cloudFiles.inferColumnTypes"] == "true"
    assert options["cloudFiles.rescuedDataColumn"] == bronze.RESCUED_DATA
    assert options["multiLine"] == "true"
    assert "harvested_at TIMESTAMP" in options["cloudFiles.schemaHints"]


def test_a_binary_source_has_no_parsing_options_at_all() -> None:
    options = bronze.autoloader_options(
        bronze.source("raw_bodies"), checkpoint_uri=CHECKPOINTS
    )
    assert options["cloudFiles.format"] == "binaryFile"
    assert "cloudFiles.rescuedDataColumn" not in options
    assert "multiLine" not in options
    assert "cloudFiles.schemaHints" not in options


def test_a_shared_directory_is_narrowed_by_a_glob() -> None:
    """Unprefixed: it is a generic file-source option, and Auto Loader refuses
    an unknown key in its own namespace at stream start rather than at plan
    time."""
    options = bronze.autoloader_options(
        bronze.source("orders"), checkpoint_uri=CHECKPOINTS
    )
    assert options["pathGlobFilter"] == "orders.jsonl"
    assert "cloudFiles.pathGlobFilter" not in options


def test_nothing_drops_a_record_and_nothing_reingests_a_file() -> None:
    """The three options that would break an acceptance criterion.

    `DROPMALFORMED` and `FAILFAST` would lose the record or fail the update,
    and the issue asks for neither. `allowOverwrites` would re-ingest a file
    rewritten at the same path, which is the duplication criterion two
    forbids.
    """
    for candidate in bronze.SOURCES:
        options = bronze.autoloader_options(candidate, checkpoint_uri=CHECKPOINTS)
        assert "cloudFiles.allowOverwrites" not in options
        assert options.get("mode") not in ("DROPMALFORMED", "FAILFAST")


def test_the_citation_fields_are_pinned_where_they_live() -> None:
    """RFC-001 §08 needs both to survive into a response payload."""
    hints = bronze.source("raw_documents").schema_hints
    assert "source_url STRING" in hints
    assert "harvested_at TIMESTAMP" in hints


def test_money_is_landed_as_written_rather_than_cast() -> None:
    """Casting a string to a decimal is a transformation, and bronze does not."""
    hints = " ".join(bronze.source("orders").schema_hints)
    assert "total" not in hints


def test_the_always_null_columns_are_pinned() -> None:
    """Inference over a column with no values in it produces no usable type."""
    hints = bronze.source("demo_visitors").schema_hints
    for column in ("thread_id", "home_store_override", "stated_preferences"):
        assert any(hint.startswith(f"{column} ") for hint in hints)


# --- The columns every row carries ------------------------------------------


def test_every_row_carries_its_path_and_when_it_was_read() -> None:
    for candidate in bronze.SOURCES:
        columns = " ".join(bronze.metadata_columns(candidate))
        assert bronze.INGESTED_AT in columns
        assert bronze.SOURCE_PATH in columns
        assert bronze.SOURCE_MODIFIED_AT in columns
        assert bronze.SOURCE_SIZE_BYTES in columns


def test_only_a_parsed_row_can_be_flagged_quarantined() -> None:
    parsed = bronze.metadata_columns(bronze.source("orders"))
    binary = bronze.metadata_columns(bronze.source("raw_bodies"))
    assert any(bronze.QUARANTINED in column for column in parsed)
    assert not any(bronze.QUARANTINED in column for column in binary)


def test_the_quarantine_projection_is_the_same_shape_everywhere() -> None:
    """It is a union, so every arm has to project the same four columns."""
    shapes = {
        bronze.quarantine_columns(candidate)[1:]
        for candidate in bronze.SOURCES
        if candidate.is_parsed
    }
    assert len(shapes) == 1


def test_bytes_cannot_be_quarantined() -> None:
    with pytest.raises(ValueError, match="cannot quarantine"):
        bronze.quarantine_columns(bronze.source("raw_bodies"))
    with pytest.raises(ValueError, match="cannot quarantine"):
        bronze.quarantine_predicate(bronze.source("raw_bodies"))


def test_a_row_with_no_identity_is_quarantined() -> None:
    """The rescued data column alone lets a truncated document through.

    A JSON file that does not parse as a whole arrives as a row of nulls with
    nothing rescued, indistinguishable from a legitimately sparse record except
    that nothing can name it. Verified on `dbw-chip-chat`, 2026-08-26.
    """
    for candidate in bronze.SOURCES:
        if not candidate.is_parsed:
            continue
        predicate = bronze.quarantine_predicate(candidate)
        assert f"{bronze.RESCUED_DATA} IS NOT NULL" in predicate
        for column in candidate.identity:
            assert f"{column} IS NULL" in predicate


def test_every_source_declares_an_identity() -> None:
    """Without one there is no way to say a row was ingested twice, and no way
    to tell a corrupt document from a sparse one."""
    for candidate in bronze.SOURCES:
        assert candidate.identity


def test_a_parsed_identity_column_is_pinned_or_is_a_reader_column() -> None:
    """An identity inferred rather than hinted could arrive as the wrong type
    on one run and break the duplicate check on the next."""
    for candidate in bronze.SOURCES:
        if not candidate.is_parsed:
            continue
        hinted = {hint.split()[0] for hint in candidate.schema_hints}
        assert set(candidate.identity) <= hinted, candidate.table


# --- The notebook and the Terraform -----------------------------------------


@pytest.fixture(scope="module")
def notebook() -> str:
    assert NOTEBOOK.exists(), f"{NOTEBOOK} is missing"
    return NOTEBOOK.read_text()


def test_the_pipeline_is_written_against_lakeflow_and_not_dlt(notebook: str) -> None:
    """Delta Live Tables became Lakeflow Spark Declarative Pipelines in 2026.

    Read over the code lines only. The markdown above them names the module it
    is not written against, which is the point of saying so.
    """
    code = "\n".join(
        line for line in notebook.splitlines() if not line.startswith("# MAGIC")
    )
    assert "from pyspark import pipelines as dp" in code
    assert "import dlt" not in code


@pytest.mark.parametrize("path", [NOTEBOOK, VERIFY])
def test_a_markdown_cell_holds_no_code(path: Path) -> None:
    """A `%md` cell is markdown all the way down, and this fails silently.

    Databricks reads a cell beginning `# MAGIC %md` as one markdown block:
    Python written below it in the same cell is rendered, not run. Nothing
    errors. The pipeline simply defines no tables and the update fails with
    `NO_TABLES_IN_PIPELINE`, which reads like the decorators are wrong.
    Observed on `dbw-chip-chat`, 2026-08-26 (gh-33).
    """
    for index, cell in enumerate(path.read_text().split("# COMMAND ----------")):
        lines = [line.rstrip() for line in cell.splitlines()]
        if not any(line.startswith("# MAGIC %md") for line in lines):
            continue
        code = [line for line in lines if line and not line.startswith(("#", "# MAGIC"))]
        assert not code, (
            f"{path.name} cell {index} is markdown and holds code: {code[:3]}"
        )


def test_the_verify_job_asserts_the_criteria_rather_than_reporting_them() -> None:
    """A notebook that prints its findings and exits zero proves nothing."""
    source = VERIFY.read_text()
    assert "raise AssertionError" in source
    assert "dbutils.notebook.exit" in source


def test_the_notebook_reads_the_configuration_terraform_supplies(
    notebook: str,
) -> None:
    for key in (
        "chip_chat.raw_uri",
        "chip_chat.catalog",
        "chip_chat.checkpoint_uri",
        "chip_chat.lib_path",
    ):
        assert f'spark.conf.get("{key}")' in notebook


@pytest.fixture(scope="module")
def terraform() -> str:
    assert TERRAFORM.exists(), f"{TERRAFORM} is missing"
    return TERRAFORM.read_text()


def test_terraform_supplies_every_key_the_notebook_reads(
    terraform: str, notebook: str
) -> None:
    for key in (
        "chip_chat.raw_uri",
        "chip_chat.catalog",
        "chip_chat.checkpoint_uri",
        "chip_chat.lib_path",
    ):
        assert f'"{key}"' in terraform, f"{key} is read by the notebook and unset"
        assert f'spark.conf.get("{key}")' in notebook


def test_terraform_uploads_the_two_modules_the_notebook_imports(
    terraform: str,
) -> None:
    """They are stdlib-only so that this upload is all the packaging needed."""
    assert "databricks/src/chip_chat/databricks/bronze.py" in terraform
    assert "databricks/src/chip_chat/databricks/catalog.py" in terraform


def test_the_pipeline_is_triggered_rather_than_continuous(terraform: str) -> None:
    """A continuous pipeline holds a cluster open, which is the cost trap."""
    assert "continuous   = false" in terraform or "continuous = false" in terraform
