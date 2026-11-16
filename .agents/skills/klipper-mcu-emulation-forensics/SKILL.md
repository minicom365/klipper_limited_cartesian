---
name: klipper-mcu-emulation-forensics
description: >-
  Hardware-in-the-loop (HIL) debugging and root-cause forensics for Klipper 3D
  printer firmware using external MCU emulators (Renode/QEMU), official board
  schematics, and component datasheets. Covers klippy-to-MCU PTY serialqueue
  bridging, Cortex-M armcm_timer SysTick one-shot silicon pipeline hooks,
  DWT/RCC/ADC PythonPeripherals, PID derivative-kick settling, microsecond GPIO
  ODR/BSRR waveform reconstruction, I2C busy-wait mcu_awake profiling, and
  power/acoustic transducer localization. Use when debugging Klipper MCU pin
  states, heater/fan PWM edge anomalies, timer shutdowns, or board-level noise.
license: GPL-3.0-or-later
compatibility: Python 3.9+, uv, arm-none-eabi-binutils, Renode 1.14+ (Linux/WSL/macOS)
metadata:
  version: 1.1.0
  domain: klipper-3d-printing
---

# Klipper MCU Emulation & Hardware Schematic Forensics

## Overview

Verifies real Klipper firmware (`out/klipper.elf`) coupled to a live `klippy.py` host process over a virtual UART PTY inside an external instruction-set/MMIO MCU emulator, cross-referenced against **printer mainboard schematics, MOSFET/buffer/regulator datasheets, and mechanical cavity acoustics**. Use this skill whenever host-only batch tests (`test/klippy/`) or Linux-process MCU builds (`CONFIG_MACH_LINUX`) cannot prove actual hardware register states, post-shutdown PWM edges, interrupt storms, or `mcu_awake` stalls.

## Dependencies

Optional companion skills (this skill runs 100% standalone in clean Klipper fork checkouts):

- **`hil-emulation-forensics`**: Domain-agnostic parent methodology for general embedded HIL forensics.
- **`systematic-debugging`**: Enforces baseline commit isolation and single-variable A/B control runs before patching `klippy/` or `src/`.
- **`reverse-engineer-anything`**: Assists with deep ELF symbol and cross-reference inspection when auditing LTO-inlined MCU handlers.
- **`anti-rea-hardening`**: Provides repository-wide pre-commit sanitization (`Pillar 8`) prior to publishing patches or PRs.

## Quick Start

1. Extract zero-rebuild `armcm_timer.c` one-shot `SysTick` hooks for your compiled `out/klipper.elf`:
   ```bash
   uv run .agents/skills/klipper-mcu-emulation-forensics/scripts/klipper_hil_cli.py resolve-armcm-hooks \
     --elf out/klipper.elf \
     --objdump-bin arm-none-eabi-objdump \
     --limit 32 \
     --output /tmp/klipper_armcm_hooks.json
   ```
2. Generate exact Steinhart-Hart thermistor ADC counts and `ControlPID` derivative-settling steps:
   ```bash
   uv run .agents/skills/klipper-mcu-emulation-forensics/scripts/klipper_hil_cli.py synth-thermistor-adc \
     --t1-r1 25.0:100000.0 \
     --t2-r2 150.0:1641.9 \
     --t3-r3 250.0:226.15 \
     --pullup 4700.0 \
     --adc-max 4095 \
     --temps 25.0,199.45,198.0,190.0,175.0 \
     --limit 64 \
     --output /tmp/klipper_adc_table.json
   ```
3. Reconstruct `1 µs` GPIO `ODR`/`BSRR`/`BRR` edge waveforms before and after `M104 S0` / `M140 S0`:
   ```bash
   uv run .agents/skills/klipper-mcu-emulation-forensics/scripts/klipper_hil_cli.py parse-gpio-trace \
     --trace /tmp/klipper_gpio_trace.log \
     --pins C:8:HEATER_E0,C:9:HEATER_BED,C:7:HOTEND_FAN,C:12:AUX_LED \
     --split-time 14.250000 \
     --limit 100000 \
     --output /tmp/klipper_gpio_report.json
   ```
4. Quantify `mcu_awake`, `mcu_task_avg`, and heater PWM variance from `klippy.log`:
   ```bash
   uv run .agents/skills/klipper-mcu-emulation-forensics/scripts/klipper_hil_cli.py analyze-klippy-stats \
     --log /tmp/klippy_hil.log \
     --split-time 14.25 \
     --limit 10000 \
     --output /tmp/klippy_stats_report.json
   ```

## Utility Scripts

All automation subcommands live in [`scripts/klipper_hil_cli.py`](./scripts/klipper_hil_cli.py) (Python 3 stdlib only; writes JSON to `--output` and requires explicit `--limit`).

| Subcommand | Purpose | Required Arguments |
| :--- | :--- | :--- |
| `resolve-armcm-hooks` | Disassembles `out/klipper.elf` (`-flto` safe) to auto-detect `VectorTableOffset` (`0x08000000` vs bootloader offsets) and emit `armcm_timer.c` `SysTick->LOAD = 0` `AddHook` lines. | `--elf`, `--objdump-bin`, `--limit`, `--output` |
| `synth-thermistor-adc` | Inverts Klipper 3-point thermistor tables (`pullup_resistor`, `adc_max`) into exact ADC counts with `ControlPID` `dT/dt` settling guidance. | `--t1-r1`, `--t2-r2`, `--t3-r3`, `--pullup`, `--adc-max`, `--temps`, `--limit`, `--output` |
| `parse-gpio-trace` | Reconstructs bit-level `ODR`/`BSRR`/`BRR` edges, pulse widths, duty cycles, and frequency variances split across a G-code event timestamp. | `--trace`, `--pins`, `--split-time`, `--limit`, `--output` |
| `analyze-klippy-stats` | Parses `klippy.log` `Stats` lines to compute pre/post `mcu_awake`, `mcu_task_avg`, and heater PWM means + variances. | `--log`, `--split-time`, `--limit`, `--output` |
| `probe-renode` | Telnet IAC-safe (`0xFF` stripped, `16`-space padded) monitor client separating standalone `result_hex_values` from echoed command addresses. | `--host`, `--port`, `--commands`, `--samples`, `--interval-ms`, `--limit`, `--output` |
| `fetch-datasheet` | Rate-limited (`1.0 req/s`) component datasheet/schematic fetcher with `RateLimitError` and retry backoff. | `--url`, `--dest`, `--limit`, `--output` |
| `sanitize-audit` | Audits files for leaked OS usernames, hostnames, emails, session UUIDs, and hardware `--deny-terms`. | `--target`, `--limit`, `--output` (optional `--deny-terms`) |
| `audit-pr-markdown` | Validates PR/comment markdown files against PowerShell backtick corruption (`\ident\`), `0x00` NUL truncation, BOM, and unbalanced code fences. | `--target`, `--limit`, `--output` |

## Workflow

### 1. Baseline Commit & Upstream Confounder Isolation
- Checkout the exact Klipper commit SHA from the date the issue was observed in an isolated `git worktree`.
- Diff `klippy/extras/heaters.py`, `src/gpiocmds.c`, `src/pwmcmds.c`, `src/generic/armcm_timer.c`, and `src/stm32/` against `origin/master` to separate unrelated upstream changes.
- Test unpatched vs. minimally patched `klippy` against the **identical** compiled `out/klipper.elf` binary.

### 2. Build Untouched `klipper.elf` for Direct Boot & Hardware UART (`CONFIG_SERIAL=y`)
- Even if the physical printer board uses USB CDC (`CONFIG_USBSERIAL=y`) and a bootloader (`0x08002000` / `0x08007000`), configure `CONFIG_LOW_LEVEL_OPTIONS=y`, **`CONFIG_STM32_FLASH_START_0000=y`** (or match `sysbus.cpu VectorTableOffset` to `resolve-armcm-hooks`'s `vector_table_offset`), and **`CONFIG_SERIAL=y`** on `USART1`/`USART2`/`USART3` (`250000` baud). Omitting `FLASH_START_0000` while booting at `0x08000000` halts Cortex-M reset (`PC and SP are equal to zero`) with misleading `usart1: receiver is not enabled` logs.
- When measuring multi-branch ELF size deltas (`arm-none-eabi-size`), always `git reset --hard origin/master` before each branch checkout to prevent dirty-tree bleed, and apply `test/configs/stm32f042.config` `CONFIG_WANT_*=n` trims on `32 KiB` `STM32F103x6`/`STM32F042` builds.
- Keep `out/klipper.elf` 100% unpatched—do **not** edit `src/generic/armcm_timer.c` or `src/stm32/` to make the emulator boot.

### 3. Launch Renode with Peripheral Stubs, Optional `RM0008` I2C1 Overlay & `armcm_timer` Hooks
- Load [`assets/stm32f103_klipper_generic.repl`](./assets/stm32f103_klipper_generic.repl) (and for STM32 I2Cv1 `CCR`/`TRISE` or `rc_w0` `I2C_SR1_AF` NACK testing, run `sysbus Unregister sysbus.i2c1` followed by [`assets/stm32_rm0008_i2c1_overlay.repl`](./assets/stm32_rm0008_i2c1_overlay.repl)) inside [`assets/klipper_hil_session.resc`](./assets/klipper_hil_session.resc). Read [`references/renode-klipper-trials.md`](./references/renode-klipper-trials.md) for all ten emulation, build, and PowerShell/WSL pitfalls:
  1. Inject the `AddHook` lines from `resolve-armcm-hooks` **before** `start` so `timer_set_diff()` and LTO-inlined `timer_kick()` do not trigger a `Limit=0` `SysTick` IRQ storm.
  2. Provide `RCC` PLL-ready bits, virtual-time `DWT->CYCCNT` (`72 MHz`), and hot-reloadable `ADC1` (`0x40012400`) initialized to ambient/setpoint counts so `klippy` never trips `ADC out of range` at boot.
  3. Set `PerformanceInMips 95` and `SetGlobalQuantum "0.000500"` (`500 µs`), connect `CreateUartPtyTerminal` to `sysbus.usart1`, and wait for `"Starting heater checks"` in `klippy.log` plus `1.5 s` before sending G-code to the `-I <gcode_pty>` input terminal.

### 4. Settle `ControlPID` Derivative State & Capture `1 µs` GPIO Traces
- Attach `SetHookBeforePeripheralWrite` to `gpioPortA/B/C` (`0x0C`, `0x10`, `0x14`).
- To reproduce small-signal `MCU_pwm` edge cases (`0 < last_pwm_value < min_pwm_change [0.05]`, where `MAX_SCHEDULE_TIME = 5.0 s` vs `pwm_delay = 0.3 s` governs residual pulses after `M104 S0` / `M140 S0`), initialize `/tmp/klipper_hil_adc.txt` near the steady temperature (`T_target - 0.55 °C`) at boot and wait `>= 6.5 s` with `target = 0` so `prev_temp_deriv` decays to `0.0` (`-Kd * dT/dt == 0`).
- Record virtual time right before sending `M104 S0` / `M140 S0`, step cooling ADC values once per second, and run `parse-gpio-trace` + `analyze-klippy-stats`.

### 5. Cross-Audit Printer Schematics, Auxiliary Pins & Enclosure Acoustics
- Use [`references/printer-board-hardware-audit.md`](./references/printer-board-hardware-audit.md) (including Section 4's **Zero-Heating Cold-Bench `25 °C` Instant Acoustic Reproduction Matrix**) to map every active pin and bus load against board schematics:
  - Distinguish transient heater `MCU_pwm` pulses (`<= 2.1 s` unpatched, `<= 0.3 s` `#7322`-class zero-enforcement) from `[output_pin]` / `[led]` / `[pwm_cycle_time]` channels (especially `cycle_time: 0.001` `1 kHz` fractional PWM vs silent `VALUE=1.0` DC!) that keep toggling indefinitely until `SET_PIN VALUE=0` or `FIRMWARE_RESTART`.
  - Check `mcu_awake` inflation and `rc_w0` `I2C_SR1_AF` NACK latch behavior (`src/stm32/i2c.c` `i2c_wait` / `EV8_2` final-byte NACK during `i2c_stop`), as well as `5 V` buck PFM ripple into piezoelectric output MLCCs and heated-bed enclosure standing-wave cavities (`1.5–4 kHz`).
- Run `sanitize-audit` before staging any skill, doc, or test fixture, and always pass PR/comment texts via UTF-8 files (`--body-file` / `gh api --input`) verified by `audit-pr-markdown` (never PowerShell inline `--body "..."`).

## Rate Limiting

- Emulator execution, ELF hook extraction, GPIO trace reconstruction, and `klippy.log` analysis execute 100% offline.
- External component datasheet downloads via `fetch-datasheet` enforce **1.0 request per second** via `time.monotonic()` and `<tempdir>/klipper_hil_ratelimit.state`, raising `RateLimitError` on `HTTP 429` and retrying `5xx` errors with exponential backoff.

## Common Mistakes

1. **Testing `MCU_pwm` shutdown bugs with instantaneous `25 °C -> 199.5 °C` ADC jumps**: `ControlPID` smoothed `dT/dt` drives `-Kd * dT/dt` strongly negative, clamping PWM to `0.0` so `last_pwm_value` never arms `(0, 0.05)`. Always pre-settle boot ADC for `>= 6.5 s`.
2. **Assuming `TURN_OFF_HEATERS` or `M104 S0` halts all board PWM activity**: Auxiliary `[output_pin]`, `[led]`, `[heater_fan]` (`temp > heater_temp`), and `[display]` I2C refreshes stay active during cooldown and only clear together upon `FIRMWARE_RESTART`.
3. **Hardcoding ELF instruction addresses across rebuilds**: GCC `-flto` shifts `SysTick_Handler` and inlined `timer_kick` addresses whenever Kconfig or compiler versions change. Always re-run `resolve-armcm-hooks` on each new `out/klipper.elf`.
