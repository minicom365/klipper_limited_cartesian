# Klipper ↔ External MCU Emulator Communication & Silicon Trials

This reference documents ten recurring failure modes encountered when coupling a live **`klippy.py` host process** (`serialqueue` + `clocksync` + `reactor`) to an **untouched cross-compiled `out/klipper.elf`** inside an external MCU emulator such as Renode, cross-compiling multi-branch size matrices, and submitting Klipper PRs from Windows PowerShell + WSL.

---

## Trial 1 — `CONFIG_USBSERIAL` Enumeration Hang & Bootloader `VectorTableOffset` Halt

### Failure Modes
1. **USB CDC Hang**: Physical 32-bit printer mainboards normally compile with `CONFIG_USBSERIAL=y` (`PA11`/`PA12` USB full-speed device). Headless instruction-set emulators do not enumerate host Linux `/dev/ttyACM*` devices without USB/IP kernel forwarding, leaving `klippy` stuck in `Wait for identify_response`.
2. **Bootloader Offset CPU Halt (`PC and SP are equal to zero`)**: On STM32 families (`src/stm32/Kconfig`), `make olddefconfig` defaults to an `8 KiB` (`0x08002000`) or `28 KiB` (`0x08007000`) bootloader offset unless `CONFIG_LOW_LEVEL_OPTIONS=y` and **`CONFIG_STM32_FLASH_START_0000=y`** are explicitly set. If `.resc` specifies `sysbus.cpu VectorTableOffset 0x08000000` while `.text` starts at `0x08002000`, Cortex-M reset loads `SP=0x00000000, PC=0x00000000`, halts the CPU immediately, and `usart1` only logs misleading `Received a character, but the receiver is not enabled, dropping` warnings!

### Deterministic Fix
1. Compile the exact target MCU family (`CONFIG_MACH_STM32F103=y`, `CONFIG_MACH_STM32F4=y`, etc.) with `CONFIG_LOW_LEVEL_OPTIONS=y`, **`CONFIG_STM32_FLASH_START_0000=y`**, and **`CONFIG_SERIAL=y`** (`CONFIG_STM32_SERIAL_USART1=y`, `250000` baud).
   - If testing a bootloader-offset ELF (`0x08002000` / `0x08007000`), `resolve-armcm-hooks` automatically detects the ELF's load base and emits the matching `sysbus.cpu VectorTableOffset 0x0800xxxx` line at the top of `resc_snippet`.
2. `klippy/serialqueue.py` runs identical 64-byte CRC-16 message blocks, `identify` dictionary negotiation, `clocksync` linear regression, and `trsync` timeouts over UART as over USB CDC.
3. Bridge `sysbus.usart1` in `.resc` via:
   ```text
   emulation CreateUartPtyTerminal "klipper_uart" "/tmp/klipper_mcu_pty" true
   connector Connect sysbus.usart1 klipper_uart
   ```
4. Point `[mcu] serial: /tmp/klipper_mcu_pty` in the test `printer.cfg` and launch `klippy.py test.cfg -I /tmp/klippy_gcode_pty -l /tmp/klippy.log`.

---

## Trial 2 — `src/generic/armcm_timer.c` One-Shot `SysTick` Infinite IRQ Storm

### Failure Mode
`klipper.elf` boots, enables `SysTick`, and immediately freezes at `0%` UART output while Renode CPU load hits `100%` inside `SysTick_Handler`.

### Root Cause in Klipper C & Thumb-2 LTO Assembly
In `src/generic/armcm_timer.c`:
1. `timer_set_diff(uint32_t value)` arms a **one-shot** Cortex-M interrupt by writing:
   ```c
   SysTick->LOAD = value; // [0xE000E000, #20] = diff
   SysTick->VAL  = 0;     // [0xE000E000, #24] = 0 (latches RELOAD on real ARM silicon)
   SysTick->LOAD = 0;     // [0xE000E000, #20] = 0 (disables auto-reload after firing)
   ```
2. `timer_kick()` (which `-flto` also inlines into `sched_add_timer`, `timer_init`, and `sched_main`) writes:
   ```c
   SysTick->LOAD = 0;
   SysTick->VAL  = 0;
   SCB->ICSR = SCB_ICSR_PENDSTSET_Msk;
   ```
Renode's `CortexM` `NVIC`/`LimitTimer` updates its underlying `Limit` immediately when `SysTick->LOAD` (`offset 0x14 = #20`) is written to `0`. Setting `Limit = 0` while `ENABLE | TICKINT` is active triggers an infinite zero-virtual-time interrupt storm.

### Zero-Rebuild Fix (`resolve-armcm-hooks`)
Never modify `armcm_timer.c` (which would alter binary timing and LTO register allocation). Instead, run:
```bash
uv run .agents/skills/klipper-mcu-emulation-forensics/scripts/klipper_hil_cli.py resolve-armcm-hooks \
  --elf out/klipper.elf --objdump-bin arm-none-eabi-objdump --limit 32 --output /tmp/hooks.json
```
Paste the emitted `sysbus.cpu AddHook` commands into `.resc` **before** `start`:
- In `SysTick_Handler` (`timer_set_diff`), the hook copies the `diff` register into the zero register right before the second `str r_zero, [r_base, #20]` so Renode retains `Limit = diff`.
- In every LTO-inlined `timer_kick` site, the hook sets `r_zero = 64` (`< 1 µs` at `72 MHz`) before `str r_zero, [r_base, #20]` and restores `r_zero = 0` right after `str r_zero, [r_base, #24]` whenever `r_zero` is a callee-saved register (`r4–r11`) reused by the caller loop.

---

## Trial 3 — `clock_setup()`, `DWT->CYCCNT`, and `ADC out of range` Boot Halts

Stock MCU `.repl` files omit three peripherals required by `src/stm32/` and `klippy`:

1. **`RCC` (`0x40021000`)**: `stm32f1.c` spins waiting for `HSERDY` (`CR` bit 17), `PLLRDY` (`CR` bit 25), and `SWS` (`CFGR` bits `[3:2]`). Mirror `HSEON`/`PLLON`/`SW` writes immediately in a `PythonPeripheral`.
2. **`DWT->CYCCNT` (`0xE0001004`)**: `timer_read_time()` reads `DWT->CYCCNT`. If unmapped (`0`), `timer_dispatch_many()` sees zero elapsed ticks and calls `try_shutdown("Rescheduled timer in the past")`. Compute `(int(ElapsedVirtualTime.TotalSeconds * 72000000.0) - base_cycles) & 0xFFFFFFFF`.
3. **`ADC1` (`0x40012400`)**: Within `1 s` of `identify`, `klippy` queries configured `[extruder]` and `[heater_bed]` thermistor pins. Returning `0` or `4095` immediately trips `MCU 'mcu' shutdown: ADC out of range`. Map `ADC1` to set `SR = 0x12` (`EOC | STRT`) on `CR2.SWSTART` (`1 << 22`) and return channel-specific 12-bit counts reloaded from `/tmp/klipper_hil_adc.txt` (`0=3980,13=3980`).

All three peripherals are pre-configured in [`../assets/stm32f103_klipper_generic.repl`](../assets/stm32f103_klipper_generic.repl).

---

## Trial 4 — Telnet Monitor `0xFF` IAC Corruption & Echoed Address Contamination

### Failure Modes
1. **IAC First-Token Drop**: Connecting to `renode --port <N>` with `socket.create_connection` and immediately transmitting `sysbus.cpu AddHook ...` or `sysbus ReadDoubleWord ...` silently drops the first command because Renode prepends RFC 854 Telnet negotiation bytes (`0xFF 0xFB 0x01 ...`) onto the TCP input stream.
2. **Echoed Command Hex Contamination**: When chaining multiple register reads (`sysbus ReadDoubleWord 0x40005400; sysbus ReadDoubleWord 0x40005404; ...`), Renode echoes the command line before printing results. A naive `re.findall(r"0x[0-9A-Fa-f]+", reply)` captures the **echoed register addresses** (`0x40005400`) whenever output splits across TCP reads or waits are too short (`< 150 ms`).

### Deterministic Fix
1. Always pass startup `.resc` scripts directly on the `renode` command line (`renode --disable-xwt --port 19991 session.resc`).
2. For runtime queries, use `probe-renode` and read **`result_hex_values`** (matched strictly via standalone lines `(?m)^\s*(0x[0-9A-Fa-f]+)\s*$`), which excludes echoed prompt addresses and scales wait time automatically with semicolon-chained command count.

---

## Trial 5 — `clocksync.py` Variance vs. `Timeout with MCU` (`SetGlobalQuantum`)

- If `SetGlobalQuantum` is too large (`> 0.002000`, `2 ms`), `klippy/clocksync.py` observes bursty UART timestamps and triggers `Timer too close` when scheduling `oid_gpiocmds` or `trsync`.
- If `SetGlobalQuantum` is too small (`< 0.000020`, `20 µs`), single-core emulation speed drops below `1.0x` real time while `klippy.py` advances on host `CLOCK_MONOTONIC`, tripping `Timeout with MCU 'mcu'`.
- **Validated Sweet Spot**: `sysbus.cpu PerformanceInMips 95` with `emulation SetGlobalQuantum "0.000500"` (`500 µs`). Wait until `klippy.log` prints `"Starting heater checks"` plus `1.5 s` before writing G-code commands to `-I /tmp/klippy_gcode_pty`.

---

## Trial 6 — `ControlPID` Derivative Kick (`-Kd * dT/dt`) Masking `MCU_pwm` Edge Bugs

### Failure Mode
In `klippy/extras/heaters.py`, `MCU_pwm.set_pwm()` suppresses MCU queue updates when:
```python
(read_time < self.next_pwm_time or not self.last_pwm_value)
and abs(value - self.last_pwm_value) < self.min_pwm_change  # default 0.05
```
If an unpatched heater has `0.0 < last_pwm_value < 0.05` right when `M104 S0` (`value = 0.0`) arrives, `abs(0.0 - last_pwm_value) < 0.05` skips sending `0.0` to the MCU, leaving `src/gpiocmds.c` pulsing at `10 Hz` until `MAX_SCHEDULE_TIME` (`5.0 s`, typically `~1.6–2.1 s` remaining) expires and returns `SF_DONE` (whereas enforcing `value == 0.` flushes within `pwm_delay = 0.3 s`).

However, if a test boots at `25 °C` and jumps `/tmp/klipper_hil_adc.txt` to `199.5 °C` right before `M104 S200`:
- `ControlPID.temperature_update()` computes `temp_deriv = (199.5 - 25.0) / dt`, smoothed over `pid_smooth_time` (`~2 s`).
- `-Kd * temp_deriv` drives `bounded_co = 0.0` for several seconds, so `last_pwm_value` stays `0.0` and never enters `(0.0, 0.05)`!

### Deterministic Settling Protocol
1. Use `synth-thermistor-adc` to compute the exact ADC integer for `T_steady = T_setpoint - 0.55 °C` (e.g. `199.45 °C`).
2. Write `T_steady` to `/tmp/klipper_hil_adc.txt` **before** launching `klippy.py`.
3. After `"Starting heater checks"`, wait **`6.5 s`** with `target = 0` (`prev_temp_deriv -> 0.000` while `temp_integ` stays clamped at `0.0`).
4. Issue `M104 S<T_setpoint + 1.0>` for `2.2 s` (`pwm >= 0.05` arms heater ON), step down to `M104 S<T_steady + 0.25>` for `0.75 s` (`0.02 < pwm < 0.045`, arming `next_pwm_time = t + 4.7 s`), and then issue `M104 S0` to observe the exact physical `ODR`/`BSRR` edge difference.

---

## Trial 7 — `src/stm32/i2c.c` (`RM0008` I2Cv1) `rc_w0` `I2C_SR1_AF` Latch, `EV8_2` Race & `400 kHz` Verification

### Hardware State-Machine Physics (`STM32F1` / `STM32F2` / `STM32F4`)
1. **`rc_w0` `I2C_SR1_AF` Latch -> Next-Transfer MCU Hard Shutdown**:
   - Per `RM0008` Section 26.6.6, `I2C_SR1_AF` (`0x0400`, Acknowledge Failure) is an `rc_w0` flag cleared **only** by software writing `0` (`i2c->SR1 = ~I2C_SR1_AF`, i.e., `0xFBFF`) or disabling `PE=0`. Neither `STOP` nor `START` clears `AF`.
   - If `i2c_wait()` returns `I2C_BUS_NACK` without clearing `AF`, the **next** `i2c_start()` writes `CR1 = START | PE` (which requires physical $t_{HD;STA} \approx 4.0\text{ }\mu\text{s}$ at `100 kHz` / `0.8 µs` at `400 kHz` before `SB=1` and `SR2.MSL=1` assert) and immediately polls `i2c_wait(i2c, I2C_SR1_SB, 0, timeout)` `< 0.2 µs` later.
   - Stale `AF=1` causes `i2c_wait` to abort before `SB=1`; if `i2c_start()` ignores `i2c_wait(SB)`'s return value, it writes `DR = addr` while not master and hits `if (!(sr2 & I2C_SR2_MSL)) shutdown("Failed to send i2c addr")`.
2. **`EV8_2` Final-Byte NACK Race During `i2c_stop()`**:
   - On a 1-byte write (or final byte), `DR` moves immediately into the empty shift register (`TXE=1`), so `i2c_send_byte()` returns `I2C_BUS_SUCCESS` while bits `7..0` are still shifting out on `SDA`.
   - When the slave NACKs on the 9th clock inside `i2c_stop()` (`i2c_wait(i2c, 0, I2C_SR1_TXE, timeout)`), hardware clears `TXE=0` and asserts `AF=1` on the same peripheral event. Checking `(sr1 & clear) == 0` *before* `sr1 & I2C_SR1_AF` masks the NACK as `SUCCESS` and leaves `AF=1` latched!
3. **Standard (`100 kHz`) vs. Fast Mode (`400 kHz`) Register Verification**:
   - At `PCLK1 = 36 MHz` (`STM32F103`): `100 kHz` sets `CR2=36, CCR=0x00B4 (180), TRISE=37`; `400 kHz` (`I2C_CCR_FS | DIV_ROUND_UP(pclk, 400000 * 3)`) sets `CR2=36, CCR=0x801E (FS | 30), TRISE=11`.
   - Use [`../assets/stm32_rm0008_i2c1_overlay.repl`](../assets/stm32_rm0008_i2c1_overlay.repl) (`ADDR_NACK` and `FINAL_DATA_NACK` stimulus via `/tmp/klipper_hil_i2c_fault.txt`) and `analyze-klippy-stats` (`mcu_awake`) to verify both timing registers and transient NACK recovery end-to-end.

---

## Trial 8 — Overriding Stock Renode Peripherals & `.repl`/`.resc` Literal `\n` Traps

### Failure Modes
1. **Address Overlap / Invalid `i2c1: none` Syntax**: Stock `platforms/cpus/stm32f103.repl` already maps `i2c1: I2C.STM32F4_I2C @ sysbus 0x40005400`. Writing `i2c1: none` in a `.repl` file or registering a second peripheral at `0x40005400` aborts `LoadPlatformDescription` before `CreateUartPtyTerminal` executes.
2. **Literal `\n` Breaking IronPython One-Liners**: Inside Renode `.resc` hook strings (`SetHookBeforePeripheralWrite ... "...\n..."`) and `.repl` `'''...'''` blocks, Renode's parser expands `\n` into a raw newline prior to Python compilation, raising `SyntaxError: EOL while scanning string literal`.

### Deterministic Fix
1. Load the base platform `.repl` first, unregister the stock peripheral in `.resc` via **`sysbus Unregister sysbus.i2c1`**, and then load the overlay `.repl` (`machine LoadPlatformDescription @.../stm32_rm0008_i2c1_overlay.repl`).
2. Never write `\n` inside `.resc` or `.repl` Python script strings—always concatenate **`+ chr(10)`**.

---

## Trial 9 — Multi-Branch ELF Size Matrices (`32 KiB` Constrained Targets & Dirty-Tree Bleed)

### Failure Modes
1. **Dirty Working-Tree Bleed Across `git checkout -B`**: Running `git checkout -B feature-b origin/master` while `src/` holds uncommitted edits from `feature-a` silently carries `feature-a`'s hunks into `feature-b`, corrupting both commits and inflating `arm-none-eabi-size` deltas (`+96 B` reported instead of true `+16 B`).
2. **Constrained `32 KiB` Flash Overflow (`STM32F103x6`, `STM32F042`)**: Running bare `make olddefconfig` on `CONFIG_HAVE_LIMITED_CODE_SIZE=y` (`32 KiB` flash) enables all optional SPI/I2C sensors and overflows `.text` even on untouched `origin/master`.

### Deterministic Fix
1. Before building or patching each candidate branch in an isolated clone, always run `git checkout -B <branch> origin/master` followed by **`git reset --hard origin/master`** and `git clean -fd`, and verify `git show --stat HEAD` + two-way `git merge --no-edit` conflict freedom.
2. When measuring `32 KiB` constrained STM32 targets (`STM32F103x6`), match Klipper CI's `test/configs/stm32f042.config` trims (`CONFIG_WANT_SOFTWARE_I2C=n`, `CONFIG_WANT_LIS2DW=n`, `CONFIG_WANT_LDC1612=n`, `CONFIG_WANT_HX71X=n`, `CONFIG_WANT_CS1237=n`, `CONFIG_WANT_ADS1220=n`, `CONFIG_WANT_ADS131M0X=n`, `CONFIG_WANT_TRIGGER_ANALOG=n`, `CONFIG_WANT_NEOPIXEL=n`).

---

## Trial 10 — Windows PowerShell ↔ WSL & GitHub CLI (`gh`) Escape Pitfalls

### Failure Modes
1. **PowerShell Backtick Escape & `0x00` NUL Truncation**: In PowerShell double-quoted strings (``gh pr comment --body "... \`set_pwm()\` ... \`0.\` ..."``), backtick (`` ` ``) is PowerShell's escape character—not backslash (`\`). `` \`set_pwm()\` `` corrupts into `\set_pwm()\`, and `` `0 `` expands to an **ASCII `NUL` (`0x00`) byte**, truncating the C-string argument passed to `gh.exe` mid-sentence!
2. **`wsl -e bash -c` Nested Quote Stripping**: Passing inline `python3 -c "... \" ..."` inside `wsl -e bash -c "..."` from PowerShell strips inner quotes and fails with `unexpected EOF`.
3. **Concurrent Workspace Git Lock Contention & Token Exposure**: Running builds/checkouts in an active working tree collides on `.git/index.lock`, while embedding OAuth tokens in remote URLs exposes secrets in `.git/config` or process arguments.

### Deterministic Fix
1. **Zero Inline Markdown Strings**: Always write PR bodies and comments to UTF-8 files (no BOM), validate them with **`klipper_hil_cli.py audit-pr-markdown`**, and submit exclusively via `gh pr create --body-file <path>` or `gh api --method PATCH ... --input <json_path>`.
2. **Script Files Over `-c` One-Liners**: Write standalone `.py` scripts and invoke `wsl -e bash -c "python3 /path/to/script.py"`.
3. **Isolated Clone + In-Memory Header Push**: Clone into `/tmp/klipper_pr_work` (or use `git --no-optional-locks` for read-only queries) and push via `git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic <base64>" push` sourced from ephemeral environment RAM.
