# Mistral Vibe CLI Block

<!-- block-metadata:start -->
[![Block version: unversioned](https://img.shields.io/badge/block-unversioned-lightgrey)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->


## Role

`mistral_vibe_cli` executes the local Mistral Vibe CLI in programmatic mode and emits stdout.

## Files

- `block.py`: prompt assembly, Vibe CLI process execution, stdout/stderr handling, runtime metadata, and UI rendering.
- `model.json`: default input/output ports, CLI config, and runtime capabilities.
- `inspector_panel.html`: block-owned inspector UI for instruction and CLI settings.
- `block_modal.html`: output-instruction modal UI with attributes and last-command tabs.
- `assets/css/block_modal.css`: modal layout and instruction editor styles.
- `assets/js/block_modal.js`: modal tab keyboard/click behavior.
- `node_card.html`: block-owned canvas card body.
- `tests/F5.22_mistral_vibe_cli_block.py`: behavior tests for centralized and zeromq_active execution.

## Ports

- Inputs:
  - `in` (`id: 1`): optional input; accepts generic messages, text, and JSON.
- Outputs:
  - `out` (`id: 1`): emits Mistral Vibe CLI stdout as `text/plain`.

Additional output ports are supported. Each output can define its own `instruction`, and the block executes once per output.

## Configuration

- `vibe_binary`: local Mistral Vibe CLI executable. Default: `vibe`.
- `max_turns`: optional execution turn limit. `0` means the option is omitted.
- `max_price`: optional price limit passed to `--max-price`.
- `output_format`: `text`, `json`, or `streaming`. Default `text` omits `--output`.
- `enabled_tools`: optional tool list, one per line or comma separated. Each value is passed with `--enabled-tools`.
- `extra_args`: optional shell-style argument string appended after the structured Vibe options.
- `timeout_sec`: subprocess timeout.
- `max_prompt_chars`: local prompt guard before process launch.

## Runtime Behavior

For each output, `execute_runtime()` builds one prompt from all non-empty named inputs plus the output instruction, then runs:

```text
vibe --prompt <prompt> [--max-turns N] [--max-price P] [--enabled-tools tool...] [--output format] [extra args...]
```

The block publishes stdout on the output port. Stderr, exit code, duration, prompt size, and the last command are persisted in runtime metadata.

The same implementation runs in One Shot Simulation (`centralized`) and Active Runtime (`zeromq_active`) through the generic block executor.

## UI Behavior

The inspector and modal expose instruction, binary path, optional Vibe options, timeout, prompt guard, ports, latest runtime state, and a read-only `Last cmd` tab. Editable instructions and CLI settings stay local until the user clicks **Apply**.

The modal declares `data-block-runtime-refresh="autonomous"`; block-owned JS preserves tab selection, draft instructions, and command diagnostics while runtime polling is active.

## Maintenance Notes

Mistral Vibe CLI behavior belongs in this block. Do not add Mistral Vibe-specific branches to the orchestrator or active runtime worker; use the generic block executor contract instead.

## Compatibility policy

[compatibility.json](compatibility.json) records HackInvent's verified BloxSmith versions and test evidence. Only the versions listed above have been verified, using the block-owned suites in a **bundled-block test installation**. This is not a certification of managed-package installation, every browser/OS, or live provider availability. Other framework versions are unverified, not necessarily incompatible.

The block-version badge follows `model.json`, not a published Git tag. `unversioned` means that no block release version is declared; no number is inferred from the framework version. The framework still uses `model.json` for its runtime/install contract; the tester-owned JSON does not replace it. Official integration tests run in the private `bloxmith-blocs` workspace. Test helpers and the proprietary framework are not bundled in this public block repository.
