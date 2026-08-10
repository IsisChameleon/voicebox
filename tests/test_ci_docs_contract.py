import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_active_project_surfaces_do_not_reference_unapproved_type_checker() -> None:
    forbidden = re.compile(r"\b" + "py" + r"right\b", re.IGNORECASE)
    roots = [
        ROOT / ".github",
        ROOT / "docs" / "architecture",
        ROOT / "docs" / "walkthroughs",
        ROOT / "src",
        ROOT / "tests",
    ]
    files = [ROOT / "CLAUDE.md", ROOT / "README.md", ROOT / "pyproject.toml", ROOT / "uv.lock"]
    files.extend(path for root in roots for path in root.rglob("*") if path.is_file())

    offenders = [
        str(path.relative_to(ROOT))
        for path in files
        if forbidden.search(path.read_text(errors="ignore"))
    ]

    assert offenders == []


@pytest.mark.parametrize(
    "workflow",
    sorted((ROOT / ".github" / "workflows").glob("*.yaml")),
    ids=lambda path: path.name,
)
def test_workflows_use_supported_python(workflow: Path) -> None:
    assert "uv python install 3.11" in workflow.read_text()


@pytest.mark.parametrize(
    "command",
    [
        "uv run playwright install --with-deps chromium",
        "uv run pytest",
        "node --test tests/shim_pending_inbound.test.mjs",
    ],
)
def test_required_build_check_runs_verification(command: str) -> None:
    build = (ROOT / ".github" / "workflows" / "build.yaml").read_text()
    assert command in build


@pytest.mark.parametrize("command", ["uv run ruff check", "uv run ruff format --diff"])
def test_quality_workflow_runs_ruff(command: str) -> None:
    quality = (ROOT / ".github" / "workflows" / "format.yaml").read_text()
    assert command in quality


def test_architecture_diagram_documents_supported_cdp_attach() -> None:
    diagram = (ROOT / "diagrams" / "index.html").read_text()
    assert "playwright-cli attach --cdp http://localhost:9222" in diagram
    assert "PLAYWRIGHT_MCP_CDP_ENDPOINT" not in diagram
    assert "PLAYWRIGHT_MCP_ISOLATED" not in diagram
    assert '<span class="strike">playwright-cli</span>' not in diagram


def test_architecture_diagram_documents_event_stream_contract() -> None:
    diagram = (ROOT / "diagrams" / "index.html").read_text()
    assert "listen(cursor)" in diagram
    assert "{events, cursor}" in diagram


def test_architecture_diagram_documents_stop_artifacts() -> None:
    diagram = (ROOT / "diagrams" / "index.html").read_text()
    assert "record_dir" in diagram
    assert "events.json" in diagram
    assert "metrics.json" in diagram
    assert "stereo WAV" in diagram
