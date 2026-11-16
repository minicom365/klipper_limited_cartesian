# 3D-Printer Mainboard Schematic, Power & Acoustic Forensics Guide

Once `parse-gpio-trace` and `analyze-klippy-stats` establish exact MCU pin waveforms and CPU wakeup duty cycles, use this checklist to map digital MCU behavior onto **3D-printer mainboard schematics, daughterboards, and mechanical frame acoustics**.

---

## 1. Separating Klipper Pin Lifecycles Across Reset Boundaries

When a physical symptom (hum, buzz, coil whine, LED flicker) persists during heater cooldown (`M104 S0` / `M140 S0`) and disappears only upon **`FIRMWARE_RESTART`**, test every configured pin against Klipper's lifecycle table:

| Klipper Config Section | Cleared by `M104 S0` / `M140 S0`? | Cleared by `M107`? | Cleared by `FIRMWARE_RESTART`? | Typical Post-Print Behavior |
| :--- | :---: | :---: | :---: | :--- |
| `[extruder]` / `[heater_bed]` (`MCU_pwm`) | **Yes** (`<= 2.1 s` unpatched sub-5% tail; `<= 0.3 s` zero-flush patch; then `SF_DONE` = `0` edges forever) | No | Yes | Cannot cause noise lasting minutes during cooldown once `ODR` reaches `0`. |
| `[fan]` (part cooling) | No | **Yes** | Yes | Stops on `M107`. |
| `[heater_fan]` (hotend cooling) | **No** (stays `100%` or `fan_speed` PWM while `T_extruder > heater_temp` [default `50 °C`]) | No | Momentarily resets during reboot, then re-engages if `T > 50 °C` | Runs throughout cooldown until `T < 50 °C`. |
| `[output_pin]` / `[led]` / `[pwm_cycle_time]` | **No** (`TURN_OFF_HEATERS` does **not** touch auxiliary pins) | No | **Yes** (reverts to boot `value:`) | Continues `10 Hz` (`cycle_time: 0.100`) or `1 kHz` toggling **indefinitely** unless print-end/cancel macros explicitly set `VALUE=0`! |
| `[display]` (I2C/SPI character or OLED backpack) | **No** | No | **Yes** (re-initializes controller) | Transmits periodic screen refresh bursts every `0.5–1.0 s` indefinitely. |

---

## 2. Mainboard Schematic & Component Datasheet Audit

### A. Heater & Fan Low-Side MOSFET Gate Topology
1. **5 V CMOS/TTL Gate Buffer Stage (`74HC` / `74HCT` buffer between `3.3 V` MCU GPIO and MOSFET gate)**:
   - Check schematic netlist from MCU pin -> buffer input -> buffer output -> series gate resistor ($10\text{–}100\text{ }\Omega$) -> MOSFET gate + pull-down ($4.7\text{–}10\text{ k}\Omega$).
   - When MCU `ODR == 0`, buffer $V_{OL} \le 0.1\text{ V} \ll V_{GS(th,\text{min})}$ (`~1.2–1.7 V`): MOSFET channel current is strictly zero (`µA` leakage only)—ruling out continuous heater current when `parse-gpio-trace` shows `0` post-off edges.
   - When MCU `ODR == 1`, `5 V` buffer $V_{OH} \approx 4.9\text{ V}$ saturates logic-level MOSFETs cleanly. Direct `3.3 V` GPIO drive without a buffer requires verifying $R_{DS(\text{on})}$ at $V_{GS} = 3.0\text{ V}$ in the MOSFET datasheet to prevent linear-mode overheating.
2. **Missing Fan/Coil Flyback Schottky Diode**:
   - Original OEM printer boards frequently place a parallel Schottky diode (`SS14`/`SS34`/`1N5819`) across `FAN+` and `FAN-` headers, whereas compact replacement boards sometimes omit it.
   - Even if a user mechanically stalls a cooling fan rotor by hand, PWM switching or brush commutation inductance without a local Schottky clamp injects voltage spikes onto the `+12 V / +24 V` rail.
3. **Repurposed Endstop Pins Used for Auxiliary PWM**:
   - Endstop inputs (`X-STOP`, `Y-STOP`, `E0-STOP`) commonly wire a hardware RC filter ($0.1\text{ }\mu\text{F}$ ceramic capacitor to `GND` + `10 kΩ` pull-up to `+3.3 V`) directly on the MCU header pin.
   - Driving soft-PWM (`[output_pin ... pwm: True]`) through an RC-filtered endstop pin produces repetitive capacitor charging surges and shifts daughterboard MOSFET gates slowly through their linear region, coupling switching buzz into shared ground returns.

### B. `+5 V` Buck Regulator PFM Ripple & Piezoelectric MLCC Whine
Compact aftermarket printer boards often step `+24 V` down to `+5 V` using high-frequency asynchronous buck ICs paired with **`10–22 µF` Class-II (`X5R`/`X7R`) MLCC ceramic output capacitors**, whereas older OEM boards often used fixed-frequency regulators with **`220–330 µF` tantalum or electrolytic capacitors**:
- **Mechanism**: Periodic `+5 V` load bursts (from I2C/OLED display backpacks, status LED PWM, or bus NACK retries) push light-load buck regulators into **pulse-skipping / PFM mode** at `1.5–6 kHz`.
- **Transducer**: High-value `X5R`/`X7R` MLCCs deform piezoelectrically under `5 V` ripple, vibrating the PCB copper planes, while unpotted `22 µH` buck inductors vibrate magnetostrictively. Tantalum and polymer-electrolytic capacitors are non-piezoelectric and naturally damp this acoustic mode.

---

## 3. Heated-Bed & Base-Cavity ("Cave-Like") Acoustic Localization

### Why 3D-Printer Whine Sounds Omnidirectional
Printers with an enclosed lower electronics bay sitting beneath a `200–300 mm` aluminum/glass heated bed form a parallel-plate + cylindrical/rectangular **acoustic cavity**:
- Tones between `1.5 kHz` and `4.0 kHz` have wavelengths (`8.5–22 cm`) matching the bay width and bed-to-base clearance, exciting **standing waves** that radiate uniformly around the perimeter gap rather than from a single point.
- Human hearing has maximum localization ambiguity in the `1.5–4.0 kHz` crossover band between interaural time difference (ITD) and interaural level difference (ILD), producing a diffuse "cave-like" hum even when all fans are held still and steppers are disabled (`M84`).

### 60-Second Non-Destructive Isolation Procedure
1. **Software G-Code Elimination (15 s)**:
   - While the noise is active during cooldown, send `SET_PIN` / `SET_LED` commands setting **all** auxiliary PWM pins to `0`. Immediate silence proves an unstopped `[output_pin]`/`[led]` path.
   - Temporarily comment `[display]` (or unplug the `EXP1`/I2C cable). Immediate silence proves `+5 V` buck PFM / daughterboard boost excitation from display bus bursts.
2. **Waveguide Point Localization (30 s)**:
   - Hold one end of a `30–40 cm` non-conductive silicone tube or paper straw to your ear and scan the open tip `5 mm` above: (1) the mainboard `+5 V` buck inductor and `22 µF` output MLCC, (2) the rear of the display/buzzer daughterboard, and (3) the underside center of the heated bed. The standing-wave illusion collapses immediately at the true source.
3. **Insulated Mechanical Damping Check (15 s)**:
   - Lightly press the `+5 V` buck inductor and output MLCCs with a dry wooden chopstick or plastic stylus. A pitch shift confirms passive piezoelectric/magnetostrictive radiation.

### Hardware & Software Mitigations
- **Software**: Add explicit `SET_PIN PIN=<aux> VALUE=0` calls inside `CANCEL_PRINT`, `END_PRINT`, and overridden `TURN_OFF_HEATERS` macros; avoid assigning `pwm: True` outputs to RC-filtered endstop pins; eliminate unnecessary idle I2C polling or NACK loops.
- **Hardware**: Feed `+5 V` from a fixed-frequency external buck BEC or add a `220–470 µF` low-ESR polymer/tantalum bulk capacitor across `+5 V` and `GND` to suppress PFM ripple and damp MLCC piezo excitation; add a Schottky flyback diode across switched fan headers; apply neutral-cure electronics silicone to noisy buck inductors.
