from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


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
        "uv run pyright src/voicebox",
        "node --test tests/shim_pending_inbound.test.mjs",
    ],
)
def test_required_build_check_runs_verification(command: str) -> None:
    build = (ROOT / ".github" / "workflows" / "build.yaml").read_text()
    assert command in build


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
