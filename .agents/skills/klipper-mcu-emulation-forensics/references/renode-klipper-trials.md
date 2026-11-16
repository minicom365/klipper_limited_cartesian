# Klipper ↔ External MCU Emulator Communication & Silicon Trials

This reference documents six recurring failure modes encountered when coupling a live **`klippy.py` host process** (`serialqueue` + `clocksync` + `reactor`) to an **untouched cross-compiled `out/klipper.elf`** inside an external MCU emulator such as Renode, along with deterministic zero-rebuild fixes.

---

## Trial 1 — `CONFIG_USBSERIAL` Enumeration Hang vs. `CONFIG_SERIAL` UART PTY

### Failure Mode
Physical 32-bit printer mainboards normally compile with `CONFIG_USBSERIAL=y` (`PA11`/`PA12` USB full-speed device). Headless instruction-set emulators do not enumerate host Linux `/dev/ttyACM*` devices without USB/IP kernel forwarding, leaving `klippy` stuck in `Wait for identify_response`.

### Deterministic Fix
Compile the exact target MCU family (`CONFIG_MACH_STM32F103=y`, `CONFIG_MACH_STM32F4=y`, etc.) with **`CONFIG_SERIAL=y`** on `USART1`/`USART2`/`USART3` (`250000` baud).
- `klippy/serialqueue.py` runs identical 64-byte CRC-16 message blocks, `identify` dictionary negotiation, `clocksync` linear regression, and `trsync` timeouts over UART as over USB CDC.
- Bridge `sysbus.usart1` in `.resc` via:
  ```text
  emulation CreateUartPtyTerminal "klipper_uart" "/tmp/klipper_mcu_pty" true
  connector Connect sysbus.usart1 klipper_uart
  ```
- Point `[mcu] serial: /tmp/klipper_mcu_pty` in the test `printer.cfg` and launch `klippy.py test.cfg -I /tmp/klippy_gcode_pty -l /tmp/klippy.log`.

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

## Trial 4 — Telnet Monitor `0xFF` IAC Negotiation Corrupting First Command

### Failure Mode
Connecting to `renode --port <N>` with `socket.create_connection` and immediately transmitting `sysbus.cpu AddHook ...` or `sysbus ReadDoubleWord ...` silently drops the first command because Renode prepends RFC 854 Telnet negotiation bytes (`0xFF 0xFB 0x01 ...`) onto the TCP input stream.

### Deterministic Fix
1. Always pass startup `.resc` scripts directly on the `renode` command line (`renode --disable-xwt --port 19991 session.resc`).
2. For runtime queries, use `probe-renode` (which drains initial socket bytes, prepends `16` ASCII spaces before every command, and strips `0xFF` IAC sequences on receive).

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

## Trial 7 — `src/stm32/i2c.c` Synchronous Polling & `I2C_SR1_AF` `mcu_awake` Inflation

In `src/stm32/i2c.c`:
- `i2c_setup()` hardcodes `100 kHz` standard mode regardless of higher `i2c_speed` config entries on several STM32 families.
- `i2c_wait()` busy-spins in foreground task context (`mcu_awake` rises sharply in `klippy.log` `Stats:` lines). If an auxiliary I2C display backpack NACKs (`I2C_SR1_AF`), unpatched error paths or slow character-by-character backpack transfers keep the MCU awake up to `25–30%` of every second even during idle cooldown.
- Always run `analyze-klippy-stats` alongside `parse-gpio-trace` to correlate `mcu_awake` jumps with `[display]` or sensor I2C bus activity.
