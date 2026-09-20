#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Role: Verifies Mistral Vibe CLI block behavior.
# File Name: F5.22_mistral_vibe_cli_block.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-05-19
# -----------------------------------------------------------------------------

"""F5.22 - Mistral Vibe CLI block.

The test injects a fake `vibe` executable and verifies that the block calls
Mistral Vibe CLI with `--prompt`, publishes stdout, renders its block-owned UI,
and works in both centralized and zeromq_active runtimes.
"""

# Test cases:
# - FB1/FB2/FB7 - Run text -> Mistral Vibe CLI -> display in centralized runtime and verify `vibe --prompt` receives a prompt containing inputs and instruction.
# - FB1/FB2/FB7 - Run the same graph in zeromq_active runtime and verify stdout publication through the generic active worker.
# - FB3/FB6 - Configure max turns, max price, tools, output format, and extra args, then verify the subprocess command includes them.
# - FB4/FB6 - Execute a second output instruction and verify each output stores command metadata and stdout.
# - FB5 - Reject an oversized prompt before launching the Mistral Vibe CLI.
# - UI - Render modal/inspector/node-card and verify block-owned tabs, bindings, assets, and last-command display.

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blocs.mistral_vibe_cli.block import MistralVibeCliBlock
from bloxsmith_app.block_runtime import BlockRuntimeContext
from bloxsmith_app.block_ui import render_block_inspector_panel, render_block_modal, render_block_node_card
from ui_smoke_common import (
    create_run_api,
    data_edge,
    display_node,
    expect,
    graph_payload,
    isolated_server,
    text_node,
    wait_for_run_terminal,
)
from urllib.parse import quote
from block_test_packages import install_test_package, release_key, surface_payload


@contextmanager
def fake_vibe_cli(response_text: str = "fake vibe response"):
    """Expose a fake `vibe` binary that records argv and prints a response."""

    with TemporaryDirectory(prefix="bloxsmith-fake-vibe-") as tmp:
        temp_dir = Path(tmp)
        capture_path = temp_dir / "vibe_calls.jsonl"
        binary_path = temp_dir / "vibe"
        binary_path.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

capture_path = Path(os.environ["CW_FAKE_VIBE_CAPTURE"])
capture_path.parent.mkdir(parents=True, exist_ok=True)
capture = {"argv": sys.argv[1:]}
if "--prompt" in sys.argv:
    index = sys.argv.index("--prompt")
    if index + 1 < len(sys.argv):
        capture["prompt"] = sys.argv[index + 1]
with capture_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(capture, ensure_ascii=False) + "\\n")
print(os.environ.get("CW_FAKE_VIBE_RESPONSE", "fake vibe response"))
sys.exit(int(os.environ.get("CW_FAKE_VIBE_EXIT", "0")))
""",
            encoding="utf-8",
        )
        binary_path.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        old_capture = os.environ.get("CW_FAKE_VIBE_CAPTURE")
        old_response = os.environ.get("CW_FAKE_VIBE_RESPONSE")
        old_exit = os.environ.get("CW_FAKE_VIBE_EXIT")
        os.environ["PATH"] = f"{temp_dir}{os.pathsep}{old_path}"
        os.environ["CW_FAKE_VIBE_CAPTURE"] = str(capture_path)
        os.environ["CW_FAKE_VIBE_RESPONSE"] = response_text
        os.environ["CW_FAKE_VIBE_EXIT"] = "0"
        try:
            yield capture_path
        finally:
            os.environ["PATH"] = old_path
            if old_capture is None:
                os.environ.pop("CW_FAKE_VIBE_CAPTURE", None)
            else:
                os.environ["CW_FAKE_VIBE_CAPTURE"] = old_capture
            if old_response is None:
                os.environ.pop("CW_FAKE_VIBE_RESPONSE", None)
            else:
                os.environ["CW_FAKE_VIBE_RESPONSE"] = old_response
            if old_exit is None:
                os.environ.pop("CW_FAKE_VIBE_EXIT", None)
            else:
                os.environ["CW_FAKE_VIBE_EXIT"] = old_exit


def mistral_vibe_node(*, two_outputs: bool = False, max_prompt_chars: int = 250000) -> dict:
    """Return a test Mistral Vibe CLI node document."""

    outputs = [
        {
            "id": 1,
            "name": "out",
            "title": "Out",
            "emits": ["message/*", "text/plain"],
            "multiplicity": "many",
            "instruction": "Return a concise Mistral Vibe answer from @in.",
        }
    ]
    if two_outputs:
        outputs.append(
            {
                "id": 2,
                "name": "summary",
                "title": "Summary",
                "emits": ["message/*", "text/plain"],
                "multiplicity": "many",
                "instruction": "Summarize @in.",
            }
        )
    return {
        "id": "mistral-vibe-1",
        "kind": "mistral_vibe_cli",
        "title": "Mistral Vibe CLI test",
        "position": {"x": 360, "y": 120},
        "inputs": [
            {"id": 1, "name": "in", "title": "In", "accepts": ["message/*", "text/plain"], "multiplicity": "many"}
        ],
        "outputs": outputs,
        "config": {
            "vibe_binary": "vibe",
            "max_turns": 5,
            "max_price": "1.25",
            "output_format": "json",
            "enabled_tools": "read\nwrite,search",
            "extra_args": "--agent plan",
            "timeout_sec": 10,
            "max_prompt_chars": max_prompt_chars,
        },
    }


def run_mistral_vibe_case(runtime_mode: str) -> None:
    """TC1/TC2 - Verify Mistral Vibe CLI execution and output propagation in one runtime mode."""

    with fake_vibe_cli(response_text=f"fake vibe {runtime_mode}") as capture_path:
        with isolated_server() as server:
            # Surfaces are release assets: a bundled kind serves none of them.
            model = install_test_package(server, "mistral_vibe_cli")
            key = quote(release_key(model), safe="")
            served = lambda payload, suffix: next(
                asset["path"] for asset in payload["assets"] if asset["path"].endswith(suffix))
            document = graph_payload(
                f"F5 Mistral Vibe CLI {runtime_mode}",
                [
                    text_node("text-1", "Texte Vibe", "hello vibe", 80, 120),
                    mistral_vibe_node(),
                    display_node("display-1", "Affichage", 680, 120),
                ],
                [
                    data_edge("edge-text-vibe", "text-1", 1, "mistral-vibe-1", 1),
                    data_edge("edge-vibe-display", "mistral-vibe-1", 1, "display-1", 1),
                ],
            )
            created = create_run_api(server, document, runtime_mode=runtime_mode)
            run = wait_for_run_terminal(server, str(created.get("run_id") or ""), timeout_sec=20)

        calls = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        expect(run.get("status") == "success", f"The Mistral Vibe CLI {runtime_mode} run must succeed.")
        expect(run.get("output_values", {}).get("mistral-vibe-1:1", {}).get("value").strip() == f"fake vibe {runtime_mode}", "stdout Mistral Vibe CLI incorrect.")
        argv = calls[-1].get("argv", []) if calls else []
        expect("--prompt" in argv, "Mistral Vibe CLI must be called with --prompt.")
        expect("--max-turns" in argv and "5" in argv, "The maximum number of turns must be forwarded.")
        expect("--max-price" in argv and "1.25" in argv, "The price limit must be forwarded.")
        expect(argv.count("--enabled-tools") == 3, "Each enabled tool must be forwarded with --enabled-tools.")
        expect("--output" in argv and "json" in argv, "The output format must be forwarded.")
        expect("--agent" in argv and "plan" in argv, "The additional arguments must be forwarded.")
        prompt = str(calls[-1].get("prompt") or "")
        expect("hello vibe" in prompt, "The Mistral Vibe CLI prompt must contain the 'input texte.")
        expect("Return a concise Mistral Vibe answer" in prompt, "The Mistral Vibe CLI prompt must contain the 'instruction.")
        logs = "\n".join(run.get("node_logs", {}).get("mistral-vibe-1", []))
        expect("[mistral-vibe-cmd]" in logs and " --prompt " in logs, "The logs must expose the Mistral Vibe CLI command with --prompt.")


def test_multiple_outputs_and_prompt_guard() -> None:
    """TC3 - Execute multiple output instructions and reject an oversized prompt."""

    with fake_vibe_cli(response_text="multi") as capture_path:
        with isolated_server() as server:
            document = graph_payload(
                "F5 Mistral Vibe CLI multi",
                [
                    text_node("text-1", "Texte Vibe", "multi hello", 80, 120),
                    mistral_vibe_node(two_outputs=True),
                ],
                [data_edge("edge-text-vibe", "text-1", 1, "mistral-vibe-1", 1)],
            )
            created = create_run_api(server, document, runtime_mode="centralized")
            run = wait_for_run_terminal(server, str(created.get("run_id") or ""), timeout_sec=20)

        calls = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        expect(run.get("status") == "success", "The multi-output Mistral Vibe CLI run must succeed.")
        expect(len(calls) == 2, "Mistral Vibe CLI must be called once per output.")
        expect(run.get("output_values", {}).get("mistral-vibe-1:2", {}).get("value").strip() == "multi", "Output 2 must publish stdout.")
        result = run.get("results", {}).get("mistral-vibe-1", {})
        expect("last_mistral_vibe_command" in str(result), "The metadata must keep the last Mistral Vibe CLI command.")

    block = MistralVibeCliBlock()
    result = block.execute_runtime(
        BlockRuntimeContext(
            run_id="unit-run",
            node_id="mistral-vibe-guard",
            kind="mistral_vibe_cli",
            title="Mistral Vibe guard",
            config={"vibe_binary": "vibe", "timeout_sec": 10, "max_prompt_chars": 8},
            inputs={"in": "0123456789"},
            input_content_types={"in": "text/plain"},
            input_message="0123456789",
            input_ports=(),
            output_ports=(SimpleNamespace(id=1, name="out", instruction="too long"),),
            root_dir=ROOT,
            run_dir=ROOT,
        )
    )
    expect(result.status == "failed", "A prompt that is too long must be refused before execution.")
    expect("trop long" in result.error, "The error message must explain the prompt limit.")


def test_mistral_vibe_cli_ui_contract() -> None:
    """TC4 - Render Mistral Vibe CLI block-owned modal, inspector, node-card, and assets."""

    node = mistral_vibe_node()
    rendered = render_block_modal("mistral_vibe_cli", {"node": node, "runtime": {}})
    html = str(rendered.get("html") or "")
    assets = rendered.get("assets") or []
    css = (ROOT / "blocs/mistral_vibe_cli/assets/css/block_modal.css").read_text(encoding="utf-8")
    js = (ROOT / "blocs/mistral_vibe_cli/assets/js/block_modal.js").read_text(encoding="utf-8")

    expect("cw-mistral-vibe-modal" in html, "The Mistral Vibe CLI modal must come from the block.")
    expect('data-block-runtime-refresh="autonomous"' in html, "The Mistral Vibe CLI modal must own its runtime refresh.")
    expect('data-mistral-vibe-tab-id="output-1"' in html, "The modal must expose the output instruction tab.")
    expect('data-mistral-vibe-tab-id="attributes"' in html, "The modal must expose the Attributs tab.")
    expect('data-mistral-vibe-tab-id="last-cmd"' in html, "The modal must expose the Last cmd tab.")
    expect('data-block-output-field="instruction"' in html, "L'instruction doit rester liee a output.instruction.")
    expect('data-block-config-field="vibe_binary"' in html, "The Vibe binary must be editable.")
    expect('data-block-config-field="max_turns"' in html, "Max turns must be editable.")
    expect('data-block-config-field="max_price"' in html, "Max price must be editable.")
    expect('data-block-config-field="output_format"' in html, "Output format must be editable.")
    expect('data-block-config-field="enabled_tools"' in html, "Enabled tools must be editable.")
    expect('data-block-config-field="extra_args"' in html, "The additional arguments must be editable.")
    expect(".mistral-vibe-modal-panel[hidden]" in css, "The CSS must hide the inactive panels.")
    expect("export function mount" in js, "The JS must mount the modal through the block UI registry.")

    inspector = render_block_inspector_panel("mistral_vibe_cli", {"node": node})
    inspector_html = str(inspector.get("html") or "")
    expect("cw-mistral-vibe-inspector" in inspector_html, "L'Mistral Vibe CLI inspector must come from the block.")
    expect('data-block-output-field="instruction"' in inspector_html, "L'inspector doit editer l'instruction.")

    card = render_block_node_card("mistral_vibe_cli", {"node": node})
    card_html = str(card.get("html") or "")
    expect("data-mistral-vibe-cli-node-card" in card_html, "The Mistral Vibe CLI node card must come from the block.")


def main() -> None:
    test_mistral_vibe_cli_ui_contract()
    test_multiple_outputs_and_prompt_guard()
    run_mistral_vibe_case("centralized")
    run_mistral_vibe_case("zeromq_active")
    print("[ok] F5.22_mistral_vibe_cli_block")


if __name__ == "__main__":
    main()
