# -----------------------------------------------------------------------------
# Role: Implements the Mistral Vibe CLI block runtime and UI contract.
# File Name: block.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-05-19
# -----------------------------------------------------------------------------

from __future__ import annotations

from html import escape
from typing import Any
import re
import shlex
import subprocess
import time

from bloxsmith_app.block_api import (
    BlockDefinition,
    BlockRuntimeContext,
    BlockRuntimeOutput,
    BlockRuntimeResult,
    render_inspector_template,
    render_node_card_template,
    TEXT_PLAIN,
)


DEFAULT_VIBE_BINARY = "vibe"
DEFAULT_OUTPUT_FORMAT = "text"
DEFAULT_TIMEOUT_SEC = 300
DEFAULT_MAX_PROMPT_CHARS = 250_000
MAX_TIMEOUT_SEC = 7200
MAX_PROMPT_CHARS = 1_000_000
OUTPUT_FORMATS = ("text", "json", "streaming")


# Functional behavior:
# FB1 - Build one Mistral Vibe prompt from current inputs and the target output instruction.
# FB2 - Execute Mistral Vibe CLI in programmatic mode with `--prompt` and emit stdout.
# FB3 - Add Vibe CLI options only when configured: max turns, max price, enabled tools, output format, and extra args.
# FB4 - Run once per user output, so added output ports can own separate instructions.
# FB5 - Refuse oversized prompts before launching the local Mistral Vibe CLI process.
# FB6 - Capture command, stderr, exit code, duration, timeout state, and prompt size in runtime metadata and logs.
# FB7 - Work through the generic block executor in both centralized and zeromq_active runtimes.

class MistralVibeCliBlock(BlockDefinition):
    """Autonomous block implementation for `MistralVibeCliBlock`."""
    kind = "mistral_vibe_cli"

    def render_node_card(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the Mistral Vibe CLI canvas card from the block-owned template."""

        config = self._ui_config(node)
        max_turns = str(config["max_turns"]) if int(config["max_turns"]) > 0 else "auto"
        return render_node_card_template(
            block=self,
            node=node,
            node_classes=["mistral-vibe-node"],
            replacements={
                "title": node.get("title") or self.default_title(),
                "instruction": self._truncate(config["instruction"] or "Instruction vide", 62),
                "binary": config["vibe_binary"],
                "max_turns": max_turns,
            },
        )

    def render_modal(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the Mistral Vibe CLI modal with instruction, attributes, and last command tabs."""

        payload = payload or {}
        title = str(node.get("title") or self.default_title())
        config = self._ui_config(node)
        template = (self.directory / "block_modal.html").read_text(encoding="utf-8")
        replacements = {
            "node_id": escape(str(node.get("id") or ""), quote=True),
            "node_title": escape(title),
            "node_kind": escape(self.kind, quote=True),
            "node_kind_title": escape(str(self.model.get("title") or self.default_title())),
            "modal_tabs_html": self._render_modal_tabs(node, title, config, payload),
        }
        html = template
        for key, value in replacements.items():
            html = html.replace(f"{{{{ {key} }}}}", str(value))
        return {"html": html, "context": {"node_id": str(node.get("id") or ""), "node_kind": self.kind}}

    def render_inspector_panel(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the Mistral Vibe CLI inspector with generic field bindings."""

        config = self._ui_config(node)
        template = (self.directory / "inspector_panel.html").read_text(encoding="utf-8")
        html = render_inspector_template(
            template=(
                template
                .replace("{{ instruction }}", escape(config["instruction"]))
                .replace("{{ instruction_output_id }}", str(config["instruction_output_id"]))
                .replace("{{ vibe_binary }}", escape(config["vibe_binary"], quote=True))
                .replace("{{ max_turns }}", str(config["max_turns"]))
                .replace("{{ max_price }}", escape(config["max_price"], quote=True))
                .replace("{{ output_format_options }}", self._render_output_format_options(config["output_format"]))
                .replace("{{ enabled_tools }}", escape(config["enabled_tools"]))
                .replace("{{ extra_args }}", escape(config["extra_args"], quote=True))
                .replace("{{ timeout_sec }}", str(config["timeout_sec"]))
                .replace("{{ max_prompt_chars }}", str(config["max_prompt_chars"]))
            ),
            node={**node, "type": self.kind, "kind": self.kind},
            payload=payload,
        )
        return {"html": html, "context": {"node_id": str(node.get("id") or ""), "full_panel": True}}

    def execute_runtime(self, context: BlockRuntimeContext) -> BlockRuntimeResult:
        """Execute Mistral Vibe CLI for each target output in the current runtime context."""

        config = self.normalize_config(context.config)
        outputs: list[BlockRuntimeOutput] = []
        logs: list[str] = []
        last_message = ""
        failed_records: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []

        for output_port in context.output_ports:
            record = self._execute_output_prompt(context, output_port=output_port, config=config)
            records.append(record)
            port_id = int(record["port_id"])
            port_name = str(record.get("port_name") or "")
            stdout = str(record.get("stdout") or "")
            stderr = str(record.get("stderr") or "")
            exit_code = int(record.get("exit_code") or 0)
            command = str(record.get("command") or "")
            logs.append(f"[mistral-vibe-cmd] {context.node_id}.{port_name or port_id}: {command}")
            logs.append(f"[mistral-vibe-exit] {context.node_id}.{port_name or port_id}: exit_code={exit_code}")
            logs.extend(f"[mistral-vibe-stderr] {line}" for line in stderr.splitlines())
            outputs.append(
                BlockRuntimeOutput(
                    port_id=port_id,
                    port_name=port_name,
                    value=stdout,
                    content_type=TEXT_PLAIN,
                    status="success" if exit_code == 0 else "failed",
                    exit_code=exit_code,
                    metadata={
                        "stderr": stderr,
                        "duration": record.get("duration"),
                        "last_mistral_vibe_command": command,
                        "prompt_chars": record.get("prompt_chars"),
                    },
                )
            )
            if stdout:
                last_message = stdout
            if exit_code != 0:
                failed_records.append(record)

        if failed_records:
            first = failed_records[0]
            error = str(first.get("stderr") or first.get("error") or "Mistral Vibe CLI execution failed.").strip()
            logs.append(f"[mistral-vibe-error] {context.node_id}: {error or 'execution failed.'}")
            return BlockRuntimeResult(
                status="failed",
                outputs=outputs,
                logs=logs,
                error=error,
                exit_code=int(first.get("exit_code") or 1),
                last_message=error or last_message,
                worker_received=error or last_message or "-",
                metadata=self._runtime_metadata(config, records),
            )

        logs.append(f"[done] Mistral Vibe CLI {context.node_id}: {len(outputs)} output(s) emis.")
        return BlockRuntimeResult(
            status="success",
            outputs=outputs,
            logs=logs,
            last_message=last_message,
            content_type=TEXT_PLAIN,
            worker_received=last_message or "-",
            metadata=self._runtime_metadata(config, records),
        )

    def normalize_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        """Return safe Mistral Vibe CLI runtime configuration from raw node config."""

        raw = config if isinstance(config, dict) else {}
        return {
            "vibe_binary": str(raw.get("vibe_binary") or DEFAULT_VIBE_BINARY).strip() or DEFAULT_VIBE_BINARY,
            "max_turns": self._normalize_int(raw.get("max_turns"), default=0, minimum=0, maximum=10_000),
            "max_price": self._normalize_decimal_string(raw.get("max_price")),
            "output_format": self._normalize_output_format(raw.get("output_format")),
            "enabled_tools": str(raw.get("enabled_tools") or "").strip(),
            "extra_args": str(raw.get("extra_args") or "").strip(),
            "timeout_sec": self._normalize_int(
                raw.get("timeout_sec"),
                default=DEFAULT_TIMEOUT_SEC,
                minimum=1,
                maximum=MAX_TIMEOUT_SEC,
            ),
            "max_prompt_chars": self._normalize_int(
                raw.get("max_prompt_chars"),
                default=DEFAULT_MAX_PROMPT_CHARS,
                minimum=1,
                maximum=MAX_PROMPT_CHARS,
            ),
        }

    def _execute_output_prompt(
        self,
        context: BlockRuntimeContext,
        *,
        output_port: Any,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Build and send one prompt to `vibe --prompt` for the requested output port."""

        started = time.perf_counter()
        port_id = int(getattr(output_port, "id", 0) or 0)
        port_name = str(getattr(output_port, "name", "") or "")
        instruction = str(getattr(output_port, "instruction", "") or "").strip()
        prompt = self._build_prompt(context, instruction=instruction)
        try:
            command_args = self._command_args(config, prompt)
        except ValueError as exc:
            return self._error_record(
                port_id=port_id,
                port_name=port_name,
                command=self._format_command([config["vibe_binary"], "--prompt", "<prompt>"]),
                stderr=f"Arguments Mistral Vibe CLI invalides: {exc}",
                exit_code=2,
                started=started,
                prompt=prompt,
            )
        command = self._format_command(command_args)
        if not prompt.strip():
            return self._error_record(
                port_id=port_id,
                port_name=port_name,
                command=command,
                stderr="Prompt Mistral Vibe CLI vide.",
                exit_code=2,
                started=started,
                prompt=prompt,
            )
        if len(prompt) > int(config["max_prompt_chars"]):
            return self._error_record(
                port_id=port_id,
                port_name=port_name,
                command=command,
                stderr=(
                    "Prompt Mistral Vibe CLI trop long: "
                    f"{len(prompt)} caracteres > limite {config['max_prompt_chars']}."
                ),
                exit_code=2,
                started=started,
                prompt=prompt,
            )

        try:
            completed = subprocess.run(
                command_args,
                cwd=str(context.root_dir),
                text=True,
                capture_output=True,
                timeout=int(config["timeout_sec"]),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return self._error_record(
                port_id=port_id,
                port_name=port_name,
                command=command,
                stderr=self._decode_process_text(exc.stderr)
                or f"Timeout Mistral Vibe CLI apres {config['timeout_sec']}s.",
                stdout=self._decode_process_text(exc.stdout),
                exit_code=-1,
                started=started,
                prompt=prompt,
            )
        except OSError as exc:
            return self._error_record(
                port_id=port_id,
                port_name=port_name,
                command=command,
                stderr=f"Execution Mistral Vibe CLI impossible: {exc}",
                exit_code=1,
                started=started,
                prompt=prompt,
            )

        return {
            "port_id": port_id,
            "port_name": port_name,
            "command": command,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "exit_code": int(completed.returncode),
            "duration": round(time.perf_counter() - started, 3),
            "prompt_chars": len(prompt),
        }

    def _build_prompt(self, context: BlockRuntimeContext, *, instruction: str) -> str:
        """Compose a bounded prompt from named inputs and the output instruction."""

        input_sections: list[str] = []
        seen: set[str] = set()
        for input_port in context.input_ports:
            port_id = str(getattr(input_port, "id", "") or "")
            port_name = str(getattr(input_port, "name", "") or "").strip() or port_id
            if port_name in seen:
                continue
            seen.add(port_name)
            value = str(context.input_value(port_name, port_id) or "")
            if not value:
                continue
            input_sections.append(
                "\n".join(
                    [
                        f"[BEGIN INPUT {port_name}]",
                        value,
                        f"[END INPUT {port_name}]",
                    ]
                )
            )

        parts: list[str] = []
        if input_sections:
            parts.append("Voici les donnees recues.\n\n" + "\n\n".join(input_sections))
        if instruction.strip():
            parts.append("Instruction:\n\n" + instruction.strip())
        return "\n\n".join(parts).strip()

    def _command_args(self, config: dict[str, Any], prompt: str) -> list[str]:
        """Build safe subprocess args for one Mistral Vibe CLI invocation."""

        args = [config["vibe_binary"], "--prompt", prompt]
        if int(config["max_turns"]) > 0:
            args.extend(["--max-turns", str(config["max_turns"])])
        if config["max_price"]:
            args.extend(["--max-price", config["max_price"]])
        for tool_name in self._enabled_tools(config["enabled_tools"]):
            args.extend(["--enabled-tools", tool_name])
        if config["output_format"] != DEFAULT_OUTPUT_FORMAT:
            args.extend(["--output", config["output_format"]])
        if config["extra_args"]:
            args.extend(shlex.split(config["extra_args"]))
        return args

    def _runtime_metadata(self, config: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
        """Return execution metadata persisted with the runtime result."""

        last_command = ""
        for record in reversed(records):
            command = str(record.get("command") or "")
            if command:
                last_command = command
                break
        return {
            "vibe_binary": config["vibe_binary"],
            "max_turns": config["max_turns"],
            "max_price": config["max_price"],
            "output_format": config["output_format"],
            "enabled_tools": config["enabled_tools"],
            "extra_args": config["extra_args"],
            "timeout_sec": config["timeout_sec"],
            "max_prompt_chars": config["max_prompt_chars"],
            "last_mistral_vibe_command": last_command,
        }

    def _ui_config(self, node: dict[str, Any]) -> dict[str, Any]:
        """Return normalized values used by the inspector, modal, and node card."""

        raw_config = node.get("config") if isinstance(node.get("config"), dict) else {}
        config = self.normalize_config(raw_config)
        outputs = node.get("outputs") if isinstance(node.get("outputs"), list) else []
        first_output = next((port for port in outputs if isinstance(port, dict)), {})
        return {
            **config,
            "instruction": str(first_output.get("instruction") or ""),
            "instruction_output_id": int(first_output.get("id") or 1),
        }

    def _render_modal_tabs(
        self,
        node: dict[str, Any],
        title: str,
        config: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        """Render the tabbed Mistral Vibe CLI modal content."""

        outputs = [port for port in node.get("outputs", []) if isinstance(port, dict)]
        node_dom_id = self._safe_dom_id(str(node.get("id") or "mistral-vibe"))
        selected_tab_id = f"output-{self._dict_port_id(outputs[0], 1)}" if outputs else "attributes"
        tabs: list[str] = [
            self._render_tab(
                tab_id="attributes",
                tab_dom_id=f"mistral-vibe-{node_dom_id}-tab-attributes",
                panel_dom_id=f"mistral-vibe-{node_dom_id}-panel-attributes",
                title="Attributs",
                subtitle="Configuration et ports",
                selected=selected_tab_id == "attributes",
            )
        ]
        panels: list[str] = [
            self._render_attributes_panel(
                node_dom_id=node_dom_id,
                selected=selected_tab_id == "attributes",
                title=title,
                config=config,
                node=node,
                payload=payload,
            )
        ]

        for index, port in enumerate(outputs):
            port_id = self._dict_port_id(port, index + 1)
            tab_id_value = f"output-{port_id}"
            tab_dom_id = f"mistral-vibe-{node_dom_id}-tab-{self._safe_dom_id(str(port_id))}"
            panel_dom_id = f"mistral-vibe-{node_dom_id}-panel-{self._safe_dom_id(str(port_id))}"
            raw_name = str(port.get("name") or f"out{port_id}")
            port_title = str(port.get("title") or raw_name or f"Output {port_id}")
            instruction = str(port.get("instruction") or "")
            selected = selected_tab_id == tab_id_value
            tabs.append(
                self._render_tab(
                    tab_id=tab_id_value,
                    tab_dom_id=tab_dom_id,
                    panel_dom_id=panel_dom_id,
                    title=port_title,
                    subtitle=f"#{port_id} - {raw_name}",
                    selected=selected,
                )
            )
            panels.append(
                '<section class="mistral-vibe-modal-panel mistral-vibe-output-panel" data-mistral-vibe-modal-panel '
                f'data-mistral-vibe-tab-id="{escape(tab_id_value, quote=True)}" id="{escape(panel_dom_id, quote=True)}" '
                f'role="tabpanel" aria-labelledby="{escape(tab_dom_id, quote=True)}"{"" if selected else " hidden"}>'
                '<div class="mistral-vibe-output-layout">'
                '<div class="mistral-vibe-output-editor">'
                '<div class="mistral-vibe-output-header">'
                '<div>'
                '<span class="group-label">Prompt Mistral Vibe CLI</span>'
                f'<h3>{escape(port_title)}</h3>'
                f'<p>Commande executee: <code>{escape(self._command_preview_prefix(config))}</code></p>'
                '</div>'
                '</div>'
                '<div class="field-group mistral-vibe-instruction-field">'
                '<label>Instruction</label>'
                '<textarea data-mistral-vibe-modal-instruction data-block-output-field="instruction" '
                f'data-block-output-port-id="{escape(str(port_id), quote=True)}" rows="24" spellcheck="false" '
                'placeholder="Decris ce que Mistral Vibe doit produire avec les inputs recus.">'
                f'{escape(instruction)}'
                '</textarea>'
                '</div>'
                '</div>'
                '<aside class="mistral-vibe-reference-panel">'
                '<div class="ports-editor-header"><span class="group-label">Inputs disponibles</span></div>'
                '<p class="field-hint">Les inputs sont inclus automatiquement dans le prompt.</p>'
                f'{self._render_input_references(node)}'
                '</aside>'
                '</div>'
                '</section>'
            )

        tabs.append(
            self._render_tab(
                tab_id="last-cmd",
                tab_dom_id=f"mistral-vibe-{node_dom_id}-tab-last-cmd",
                panel_dom_id=f"mistral-vibe-{node_dom_id}-panel-last-cmd",
                title="Last cmd",
                subtitle="Commande Vibe",
                selected=False,
            )
        )
        panels.append(
            self._render_last_command_panel(
                panel_dom_id=f"mistral-vibe-{node_dom_id}-panel-last-cmd",
                tab_dom_id=f"mistral-vibe-{node_dom_id}-tab-last-cmd",
                selected=False,
                command=self._ui_last_command(payload),
            )
        )

        return (
            '<div class="mistral-vibe-modal-body" data-mistral-vibe-modal-tabs>'
            '<nav class="mistral-vibe-modal-tablist" role="tablist" aria-label="Configuration Mistral Vibe CLI">'
            + "".join(tabs)
            + '</nav>'
            + '<div class="mistral-vibe-modal-panels">'
            + "".join(panels)
            + '</div>'
            + '</div>'
        )

    def _render_attributes_panel(
        self,
        *,
        node_dom_id: str,
        selected: bool,
        title: str,
        config: dict[str, Any],
        node: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        """Render identity, runtime config, ports, and latest runtime state."""

        panel_id = f"mistral-vibe-{node_dom_id}-panel-attributes"
        tab_id = f"mistral-vibe-{node_dom_id}-tab-attributes"
        return (
            '<section class="mistral-vibe-modal-panel mistral-vibe-attributes-panel" data-mistral-vibe-modal-panel '
            f'data-mistral-vibe-tab-id="attributes" id="{escape(panel_id, quote=True)}" role="tabpanel" '
            f'aria-labelledby="{escape(tab_id, quote=True)}"{"" if selected else " hidden"}>'
            '<div class="mistral-vibe-attributes-grid">'
            '<section class="mistral-vibe-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Identite</span></div>'
            f'{self._render_title_field(title)}'
            '</section>'
            '<section class="mistral-vibe-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Configuration</span></div>'
            f'{self._render_config_fields(config)}'
            '</section>'
            '<section class="mistral-vibe-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Ports</span></div>'
            f'{self._render_generic_modal_ports(node)}'
            '</section>'
            '<section class="mistral-vibe-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Dernier etat</span></div>'
            f'{self._render_generic_modal_runtime(payload)}'
            '</section>'
            '</div>'
            '</section>'
        )

    def _render_config_fields(self, config: dict[str, Any]) -> str:
        """Render editable Mistral Vibe CLI settings."""

        return (
            '<div class="mistral-vibe-config-grid">'
            '<div class="field-group">'
            '<label>Binaire Vibe</label>'
            f'<input data-block-config-field="vibe_binary" type="text" autocomplete="off" '
            f'spellcheck="false" value="{escape(config["vibe_binary"], quote=True)}" />'
            '</div>'
            '<div class="field-group">'
            '<label>Max turns</label>'
            f'<input data-block-config-field="max_turns" data-block-value-type="integer" type="number" '
            f'min="0" max="10000" step="1" value="{config["max_turns"]}" />'
            '</div>'
            '<div class="field-group">'
            '<label>Max price</label>'
            f'<input data-block-config-field="max_price" type="text" autocomplete="off" spellcheck="false" '
            f'placeholder="vide = pas de limite" value="{escape(config["max_price"], quote=True)}" />'
            '</div>'
            '<div class="field-group">'
            '<label>Output format</label>'
            f'<select data-block-config-field="output_format">{self._render_output_format_options(config["output_format"])}</select>'
            '</div>'
            '<div class="field-group mistral-vibe-tools-field">'
            '<label>Enabled tools</label>'
            f'<textarea data-block-config-field="enabled_tools" rows="3" spellcheck="false" '
            f'placeholder="Un outil par ligne, ou separe par virgule.">{escape(config["enabled_tools"])}</textarea>'
            '</div>'
            '<div class="field-group">'
            '<label>Arguments additionnels</label>'
            f'<input data-block-config-field="extra_args" type="text" autocomplete="off" spellcheck="false" '
            f'placeholder="ex: --agent plan" value="{escape(config["extra_args"], quote=True)}" />'
            '</div>'
            '<div class="field-group">'
            '<label>Timeout secondes</label>'
            f'<input data-block-config-field="timeout_sec" data-block-value-type="integer" type="number" '
            f'min="1" max="{MAX_TIMEOUT_SEC}" step="1" value="{config["timeout_sec"]}" />'
            '</div>'
            '<div class="field-group">'
            '<label>Limite prompt</label>'
            f'<input data-block-config-field="max_prompt_chars" data-block-value-type="integer" type="number" '
            f'min="1" max="{MAX_PROMPT_CHARS}" step="1000" value="{config["max_prompt_chars"]}" />'
            '</div>'
            '</div>'
            '<p class="field-hint">Le runtime lance <code>vibe --prompt &lt;prompt&gt;</code>. Les options sont ajoutees seulement si renseignees.</p>'
        )

    def _render_title_field(self, title: str) -> str:
        """Render the editable title field without duplicating the modal Apply button."""

        return (
            '<div class="field-group">'
            '<label>Nom du bloc</label>'
            f'<input data-block-title-field type="text" autocomplete="off" value="{escape(title, quote=True)}" />'
            '</div>'
        )

    def _render_input_references(self, node: dict[str, Any]) -> str:
        """Render input names that will be included in the Vibe prompt."""

        inputs = node.get("inputs") if isinstance(node.get("inputs"), list) else []
        if not inputs:
            return '<div class="ports-editor-empty">Aucune entree disponible.</div>'
        rows: list[str] = []
        for index, port in enumerate(inputs):
            if not isinstance(port, dict):
                continue
            port_id = self._dict_port_id(port, index + 1)
            raw_name = str(port.get("name") or "").strip()
            title = str(port.get("title") or raw_name or f"Input {port_id}").strip()
            rows.append(
                '<div class="mistral-vibe-reference-row">'
                f'<code>@{escape(raw_name or str(port_id))}</code>'
                '<div>'
                f'<strong>{escape(title)}</strong>'
                f'<small>#{escape(str(port_id))} - {escape(raw_name or str(port_id))}</small>'
                '</div>'
                '</div>'
            )
        return '<div class="mistral-vibe-reference-list">' + "".join(rows) + '</div>'

    def _render_last_command_panel(
        self,
        *,
        panel_dom_id: str,
        tab_dom_id: str,
        selected: bool,
        command: str,
    ) -> str:
        """Render the read-only panel containing the latest Mistral Vibe command."""

        command_source_id = f"{escape(panel_dom_id, quote=True)}-source"
        empty_class = " hidden" if command else ""
        command_class = "" if command else " hidden"
        return (
            '<section class="mistral-vibe-modal-panel mistral-vibe-last-command-panel" data-mistral-vibe-modal-panel '
            f'data-mistral-vibe-tab-id="last-cmd" id="{escape(panel_dom_id, quote=True)}" role="tabpanel" '
            f'aria-labelledby="{escape(tab_dom_id, quote=True)}"{"" if selected else " hidden"}>'
            '<div class="mistral-vibe-last-command-layout">'
            '<div class="mistral-vibe-last-command-header">'
            '<div>'
            '<span class="group-label">Derniere commande</span>'
            '<h3>Last cmd</h3>'
            '<p>Commande Mistral Vibe CLI preparee par le runtime pour la derniere execution.</p>'
            '</div>'
            f'<button class="ghost-btn mistral-vibe-last-command-copy{command_class}" data-block-modal-copy="#{command_source_id}" type="button">Copier</button>'
            '</div>'
            f'<p class="mistral-vibe-last-command-empty{empty_class}">Aucune commande Mistral Vibe CLI enregistree pour ce bloc.</p>'
            f'<pre class="mistral-vibe-last-command-output{command_class}" id="{command_source_id}" data-block-modal-copy-source>{escape(command)}</pre>'
            '</div>'
            '</section>'
        )

    def _render_tab(
        self,
        *,
        tab_id: str,
        tab_dom_id: str,
        panel_dom_id: str,
        title: str,
        subtitle: str,
        selected: bool,
    ) -> str:
        """Render one modal tab button."""

        return (
            '<button class="mistral-vibe-modal-tab" data-mistral-vibe-modal-tab '
            f'data-mistral-vibe-tab-id="{escape(tab_id, quote=True)}" id="{escape(tab_dom_id, quote=True)}" '
            f'type="button" role="tab" aria-selected="{str(selected).lower()}" '
            f'aria-controls="{escape(panel_dom_id, quote=True)}" tabindex="{0 if selected else -1}">'
            f'<span>{escape(title)}</span>'
            f'<small>{escape(subtitle)}</small>'
            '</button>'
        )

    def _render_output_format_options(self, selected_format: str) -> str:
        """Render output format select options."""

        selected = self._normalize_output_format(selected_format)
        return "".join(
            f'<option value="{escape(option, quote=True)}"{" selected" if option == selected else ""}>{escape(option)}</option>'
            for option in OUTPUT_FORMATS
        )

    def _ui_last_command(self, payload: dict[str, Any]) -> str:
        """Return the latest Mistral Vibe CLI command from runtime payload metadata."""

        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        result = runtime.get("result") if isinstance(runtime.get("result"), dict) else {}
        command = str(result.get("last_mistral_vibe_command") or "").strip()
        if command:
            return command
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        command = str(metadata.get("last_mistral_vibe_command") or "").strip()
        if command:
            return command
        outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
        for output in reversed(list(outputs.values())):
            if not isinstance(output, dict):
                continue
            output_metadata = output.get("metadata") if isinstance(output.get("metadata"), dict) else {}
            command = str(
                output.get("last_mistral_vibe_command")
                or output_metadata.get("last_mistral_vibe_command")
                or ""
            ).strip()
            if command:
                return command
        return ""

    def _command_preview_prefix(self, config: dict[str, Any]) -> str:
        """Return a compact command prefix for UI copy."""

        return self._format_command(self._command_args(config, "<prompt>"))

    def _format_command(self, args: list[str]) -> str:
        """Return a shell-readable command string for logs and modal display."""

        return shlex.join(args)

    def _error_record(
        self,
        *,
        port_id: int,
        port_name: str,
        command: str,
        stderr: str,
        exit_code: int,
        started: float,
        prompt: str,
        stdout: str = "",
    ) -> dict[str, Any]:
        """Build a consistent failed execution record."""

        return {
            "port_id": port_id,
            "port_name": port_name,
            "command": command,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "duration": round(time.perf_counter() - started, 3),
            "prompt_chars": len(prompt),
        }

    def _enabled_tools(self, raw_value: str) -> list[str]:
        """Return deduplicated enabled tools from comma or line separated config."""

        tools: list[str] = []
        seen: set[str] = set()
        for token in re.split(r"[\n,]+", str(raw_value or "")):
            tool = token.strip()
            if not tool or tool in seen:
                continue
            seen.add(tool)
            tools.append(tool)
        return tools

    def _dict_port_id(self, port: dict[str, Any], fallback: int) -> int | str:
        """Return a stable port id for modal field bindings."""

        raw_id = port.get("id")
        try:
            parsed = int(raw_id)
        except (TypeError, ValueError):
            return str(raw_id or fallback)
        return parsed if parsed > 0 else fallback

    def _safe_dom_id(self, value: str) -> str:
        """Normalize a value so it can be embedded in modal DOM ids."""

        normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip())
        return normalized.strip("-") or "mistral-vibe"

    def _truncate(self, value: str, max_length: int) -> str:
        """Return a compact one-line preview for the node card."""

        text = str(value or "").replace("\n", " ").strip()
        return text if len(text) <= max_length else f"{text[: max_length - 1]}..."

    def _decode_process_text(self, raw_value: Any) -> str:
        """Decode subprocess timeout stdout/stderr payloads."""

        if isinstance(raw_value, str):
            return raw_value
        if raw_value is None:
            return ""
        if isinstance(raw_value, bytes):
            return raw_value.decode("utf-8", "replace")
        return str(raw_value)

    def _normalize_decimal_string(self, raw_value: Any) -> str:
        """Return a non-negative decimal string accepted by Mistral Vibe CLI."""

        value = str(raw_value or "").strip()
        if not value:
            return ""
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            return value
        return ""

    def _normalize_output_format(self, raw_value: Any) -> str:
        """Return a supported Mistral Vibe output format."""

        value = str(raw_value or DEFAULT_OUTPUT_FORMAT).strip().lower()
        return value if value in OUTPUT_FORMATS else DEFAULT_OUTPUT_FORMAT

    def _normalize_int(self, raw_value: Any, *, default: int, minimum: int, maximum: int) -> int:
        """Return a bounded integer config value."""

        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))
