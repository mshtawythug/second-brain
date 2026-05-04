from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_TSX = REPO_ROOT / "quartz_overrides/quartz/components/Graph.tsx"
GRAPH_INLINE = REPO_ROOT / "quartz_overrides/quartz/components/scripts/graph.inline.ts"
GRAPH_SCSS = REPO_ROOT / "quartz_overrides/quartz/styles/graph.scss"
LAYOUT = REPO_ROOT / "quartz_overrides/quartz.layout.ts"


def _selector_block(text: str, selector: str) -> str:
    start = text.index(selector)
    body_start = text.index("{", start)
    depth = 0
    for index in range(body_start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[body_start + 1 : index]
    raise AssertionError(f"Could not find complete block for {selector}")


def _selector_blocks(text: str, selector: str) -> list[str]:
    blocks: list[str] = []
    start = 0
    while True:
        try:
            start = text.index(selector, start)
        except ValueError:
            return blocks
        body_start = text.index("{", start)
        depth = 0
        for index in range(body_start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[body_start + 1 : index])
                    start = index + 1
                    break
        else:
            raise AssertionError(f"Could not find complete block for {selector}")


def test_graph_component_preserves_existing_buttons_and_adds_workbench() -> None:
    text = GRAPH_TSX.read_text(encoding="utf-8")

    assert 'class="local-graph-fullscreen-icon"' in text
    assert 'class="brain-stock-graph-icon"' in text
    assert 'class="global-graph-icon"' in text
    assert 'class="brain-graph-workbench-icon"' in text
    assert 'class="brain-graph-workbench-outer"' in text
    assert 'class="brain-graph-workbench-container"' in text
    assert 'aria-label="Graph Diagnostic Workbench"' in text


def test_graph_config_exposes_workbench_mode_without_replacing_global() -> None:
    text = GRAPH_TSX.read_text(encoding="utf-8")

    assert "workbenchGraph" in text
    assert "diagnosticWorkbench" in text
    assert "globalGraph:" in text
    assert "localGraph:" in text


def test_inline_renderer_has_workbench_state_and_diagnostics() -> None:
    text = GRAPH_INLINE.read_text(encoding="utf-8")

    for snippet in (
        "type GraphRenderMode",
        "type GraphWorkbenchMode",
        "computeGraphDiagnostics",
        "renderWorkbenchShell",
        "updateWorkbenchInspector",
        "selectedNodeId",
        "derivedCount",
        "incomingCount",
        "outgoingCount",
        "workbenchCanvasPane",
        "brain-graph-canvas-pane",
        "canvasHost.clientWidth",
        "canvasHost.appendChild(app.canvas)",
        "ResizeObserver",
        "resizeObserver.observe(workbenchCanvasPane)",
        "resizeObserver?.disconnect()",
    ):
        assert snippet in text


def test_workbench_shell_is_created_before_canvas_measurement() -> None:
    text = GRAPH_INLINE.read_text(encoding="utf-8")

    shell_index = text.index("renderWorkbenchShell()")
    sizing_index = text.index("const canvasHost = workbenchCanvasPane ?? graph")
    append_index = text.index("canvasHost.appendChild(app.canvas)")

    assert shell_index < sizing_index < append_index


def test_workbench_styles_exist() -> None:
    text = GRAPH_SCSS.read_text(encoding="utf-8")

    for selector in (
        ".brain-graph-workbench-icon",
        ".brain-graph-workbench-outer",
        ".brain-graph-workbench-container",
        ".brain-graph-workbench-shell",
        ".brain-graph-mode-rail",
        ".brain-graph-inspector",
        ".brain-graph-toolbar",
    ):
        assert selector in text


def test_workbench_modal_uses_fullscreen_positioning_like_existing_graph_views() -> None:
    text = GRAPH_SCSS.read_text(encoding="utf-8")

    outer_blocks = _selector_blocks(text, ".graph > .brain-graph-workbench-outer")
    container = _selector_block(
        text,
        ".graph > .brain-graph-workbench-outer > .brain-graph-workbench-container",
    )

    assert any(
        all(
            declaration in block
            for declaration in (
                "position: fixed;",
                "z-index: 9999;",
                "left: 0;",
                "top: 0;",
                "width: 100vw;",
                "height: 100%;",
                "overflow: hidden;",
            )
        )
        for block in outer_blocks
    )

    assert "position: fixed;" in container
    assert "box-sizing: border-box;" in container
    assert "border:" in container
    assert "border-radius:" in container


def test_workbench_inspector_is_opaque_above_constrained_canvas() -> None:
    text = GRAPH_SCSS.read_text(encoding="utf-8")

    shell = _selector_block(text, ".brain-graph-workbench-shell")
    inspector = _selector_block(text, ".brain-graph-inspector")
    canvas_pane = _selector_block(text, ".brain-graph-canvas-pane")
    canvas = _selector_block(text, ".brain-graph-canvas-pane > canvas")

    assert "grid-template-areas:" in shell
    assert '"rail graph inspector"' in shell
    assert "isolation: isolate;" in shell

    assert "grid-area: inspector;" in inspector
    assert "background-color: #202124;" in inspector
    assert "position: relative;" in inspector
    assert "z-index: 3;" in inspector
    assert "min-width: 18rem;" in inspector

    assert "grid-area: graph;" in canvas_pane
    assert "contain: paint;" in canvas_pane
    assert "overflow: hidden;" in canvas_pane
    assert "z-index: 1;" in canvas_pane
    assert "width: 100% !important;" not in canvas
    assert "height: 100% !important;" not in canvas
    assert "max-width: 100%;" in canvas
    assert "max-height: 100%;" in canvas
    assert "overflow: hidden;" in canvas


def test_layout_preserves_existing_views_and_configures_workbench() -> None:
    text = LAYOUT.read_text(encoding="utf-8")

    assert "localGraph:" in text
    assert "globalGraph:" in text
    assert "workbenchGraph:" in text
    assert "diagnosticWorkbench: true" in text
