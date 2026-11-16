# [EN] Implements the Klipper-specific HIL emulation CLI toolkit (LTO-aware armcm_timer.c SysTick PC hook resolver, Klipper 3-point thermistor ADC synthesizer, microsecond GPIO ODR/BSRR trace analyzer with variance, klippy.log Stats mcu_awake/PWM profiler, IAC-safe Renode monitor client, rate-limited datasheet fetcher, and Pillar-8 sanitization auditor) using Python 3 stdlib only.
# [KO] 클리퍼 전용 HIL 에뮬레이션 CLI 도구 모음(LTO 인라이닝 대응 armcm_timer.c SysTick PC 훅 자동 추출기, 3점 서미스터 역산 ADC 생성기, 분산 포함 마이크로초 GPIO 파형 분석기, klippy.log Stats mcu_awake/PWM 프로파일러, IAC 안전 모니터 클라이언트, 속도 제한 데이터시트 수집기, 동적 살균 감사기)을 외부 의존성 배제를 위해 파이썬 3 표준 라이브러리만으로 구현한다.

"""Klipper MCU HIL Emulation & Hardware Forensics CLI utility."""

from __future__ import annotations

import argparse
import getpass
import json
import math
from pathlib import Path
import re
import select
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

SCRIPT_DIR = Path(__file__).resolve().parent


class RateLimitError(Exception):
    """Raised when an external datasheet/API endpoint returns HTTP 429."""


class FileLockRateLimiter:
    """Cross-process 1.0 req/s rate limiter backed by time.monotonic() and a temp state file."""

    REQUESTS_PER_SECOND = 1.0
    MIN_INTERVAL = 1.0 / REQUESTS_PER_SECOND
    MAX_RETRIES = 3

    def __init__(self) -> None:
        self._state_path = Path(tempfile.gettempdir()) / "klipper_hil_ratelimit.state"
        self._last_mono = 0.0

    def _wait_slot(self) -> None:
        now_mono = time.monotonic()
        elapsed_local = now_mono - self._last_mono
        if elapsed_local < self.MIN_INTERVAL:
            time.sleep(self.MIN_INTERVAL - elapsed_local)

        now_wall = time.time()
        try:
            if self._state_path.exists():
                last_wall = float(self._state_path.read_text(encoding="utf-8").strip())
                if 0.0 <= (now_wall - last_wall) < self.MIN_INTERVAL:
                    time.sleep(self.MIN_INTERVAL - (now_wall - last_wall))
        except (OSError, ValueError) as exc:
            print(f"[WARN] Rate-limit state read fallback ({self._state_path}): {exc}", file=sys.stderr)

        try:
            self._state_path.write_text(f"{time.time():.6f}\n", encoding="utf-8")
        except OSError as exc:
            print(f"[WARN] Rate-limit state write failed ({self._state_path}): {exc}", file=sys.stderr)
        self._last_mono = time.monotonic()

    def fetch_bytes(self, url: str) -> tuple[bytes, dict[str, str]]:
        if not url.startswith(("https://", "http://")):
            raise ValueError(f"Unsupported URL scheme for fetch-datasheet: {url}")

        for attempt in range(self.MAX_RETRIES + 1):
            self._wait_slot()
            req = urllib_request.Request(
                url,
                headers={"User-Agent": "klipper-mcu-emulation-forensics/1.0 (stdlib urllib; rate-limit=1rps)"},
            )
            try:
                with urllib_request.urlopen(req, timeout=30) as resp:
                    payload = resp.read()
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    return payload, headers
            except urllib_error.HTTPError as exc:
                body_preview = ""
                try:
                    body_preview = exc.read(1024).decode("utf-8", errors="replace")
                except OSError as read_exc:
                    body_preview = f"<unreadable body: {read_exc}>"

                if exc.code == 429:
                    raise RateLimitError(
                        f"HTTP 429 rate limit exceeded for {url} (configured={self.REQUESTS_PER_SECOND} req/s). "
                        f"Body: {body_preview}"
                    ) from exc
                if 500 <= exc.code < 600 and attempt < self.MAX_RETRIES:
                    backoff = 2**attempt
                    print(
                        f"Transient HTTP {exc.code} from {url}; retrying in {backoff}s "
                        f"(attempt {attempt + 1}/{self.MAX_RETRIES})...",
                        file=sys.stderr,
                    )
                    time.sleep(backoff)
                    continue
                raise RuntimeError(
                    f"Non-retriable HTTP {exc.code} from {url} (rate={self.REQUESTS_PER_SECOND} req/s): {body_preview}"
                ) from exc
            except urllib_error.URLError as exc:
                if attempt < self.MAX_RETRIES:
                    backoff = 2**attempt
                    print(
                        f"Network error contacting {url} ({exc.reason}); retrying in {backoff}s "
                        f"(attempt {attempt + 1}/{self.MAX_RETRIES})...",
                        file=sys.stderr,
                    )
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"Network failure fetching {url}: {exc}") from exc

        raise RuntimeError(f"Exhausted retries ({self.MAX_RETRIES}) for {url}")


ARM_REG_MAP: dict[str, int] = {
    "r0": 0,
    "r1": 1,
    "r2": 2,
    "r3": 3,
    "r4": 4,
    "r5": 5,
    "r6": 6,
    "r7": 7,
    "r8": 8,
    "r9": 9,
    "sl": 10,
    "r10": 10,
    "fp": 11,
    "r11": 11,
    "ip": 12,
    "r12": 12,
    "sp": 13,
    "lr": 14,
    "pc": 15,
}


def write_json_output(data: dict[str, Any], output_path: str) -> None:
    target = Path(output_path).expanduser().resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        print(f"Success! Data written to: {target}")
    except (OSError, TypeError, ValueError) as exc:
        print(f"Error writing JSON output to {target}: {exc}", file=sys.stderr)
        sys.exit(1)


def strip_telnet_iac(raw: bytes) -> bytes:
    """Remove RFC 854 Telnet IAC (0xFF) negotiation sequences from Renode monitor socket bytes."""
    cleaned = bytearray()
    idx = 0
    total = len(raw)
    while idx < total:
        if raw[idx] == 0xFF and idx + 1 < total:
            cmd = raw[idx + 1]
            if cmd in (0xFB, 0xFC, 0xFD, 0xFE):
                idx += 3
                continue
            if cmd == 0xFA:
                sub_end = raw.find(b"\xff\xf0", idx + 2)
                idx = (sub_end + 2) if sub_end != -1 else total
                continue
            idx += 2
            continue
        cleaned.append(raw[idx])
        idx += 1
    return bytes(cleaned)


def resolve_armcm_hooks(elf_path: str, objdump_bin: str, limit: int) -> dict[str, Any]:
    """Locate Klipper src/generic/armcm_timer.c SysTick->LOAD=0 sites across LTO-inlined functions."""
    if limit <= 0:
        raise ValueError("--limit must be a positive integer")
    elf_file = Path(elf_path).expanduser().resolve()
    if not elf_file.is_file():
        raise FileNotFoundError(f"Klipper ELF binary not found: {elf_file}")

    resolved_bin = shutil.which(objdump_bin) or objdump_bin
    try:
        proc = subprocess.run(
            [resolved_bin, "-d", str(elf_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"Failed to execute objdump binary '{objdump_bin}': {exc}") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"objdump exited with code {proc.returncode} on {elf_file}: {proc.stderr.strip()}"
        )

    fn_re = re.compile(r"^([0-9a-fA-F]+)\s+<([^>]+)>:")
    ins_re = re.compile(r"^\s*([0-9a-fA-F]+):\s+[0-9a-fA-F ]+\s+([a-zA-Z0-9_.]+)\s*(.*)$")
    scs_re = re.compile(r"^(r\d+|sl|fp|ip),\s*#3758153728\b.*0xe000e000", re.IGNORECASE)
    str_imm_re = re.compile(r"^(r\d+|sl|fp|ip),\s*\[(r\d+|sl|fp|ip),\s*#(\d+)\]")

    parsed: list[dict[str, Any]] = []
    current_fn = "<unknown>"
    for line in proc.stdout.splitlines():
        m_fn = fn_re.match(line)
        if m_fn:
            current_fn = m_fn.group(2)
            continue
        m_ins = ins_re.match(line)
        if m_ins:
            parsed.append(
                {
                    "addr": int(m_ins.group(1), 16),
                    "mnemonic": m_ins.group(2).lower(),
                    "operands": m_ins.group(3).strip(),
                    "fn": current_fn,
                }
            )

    if not parsed:
        raise RuntimeError(f"No disassembled instructions parsed from {elf_file}")

    # Auto-detect ELF flash load / vector table base (e.g. 0x08000000 vs 0x08002000 8KiB / 0x08007000 28KiB bootloader)
    min_code_addr = min(ins["addr"] for ins in parsed)
    vector_table_offset = min_code_addr & ~0x7FF

    hooks: list[dict[str, Any]] = []
    truncated = False

    for idx, item in enumerate(parsed):
        if not item["mnemonic"].startswith("mov"):
            continue
        m_scs = scs_re.match(item["operands"])
        if not m_scs:
            continue

        base_reg = m_scs.group(1).lower()
        window = parsed[idx + 1 : idx + 28]
        stores_20: list[tuple[int, dict[str, Any], str]] = []
        stores_24: list[tuple[int, dict[str, Any], str]] = []

        for rel_i, w_ins in enumerate(window):
            if w_ins["mnemonic"] in ("b.n", "b.w", "bx", "ldmia.w", "pop") and stores_20:
                break
            if w_ins["mnemonic"].startswith("str") and not w_ins["mnemonic"].startswith(("strb", "strh", "strd")):
                m_str = str_imm_re.match(w_ins["operands"])
                if m_str and m_str.group(2).lower() == base_reg:
                    src_reg = m_str.group(1).lower()
                    disp = int(m_str.group(3))
                    if disp == 20:
                        stores_20.append((rel_i, w_ins, src_reg))
                    elif disp == 24:
                        stores_24.append((rel_i, w_ins, src_reg))

        if len(stores_20) >= 2 and len(stores_24) >= 1:
            # timer_set_diff(diff) inside SysTick_Handler: LOAD=diff; VAL=0; LOAD=0
            diff_reg = stores_20[0][2]
            zero_reg = stores_20[1][2]
            val_store_ins = stores_24[0][1]
            diff_idx = ARM_REG_MAP[diff_reg]
            zero_idx = ARM_REG_MAP[zero_reg]
            hook_addr = val_store_ins["addr"]
            hooks.append(
                {
                    "klipper_site": "timer_set_diff (SysTick_Handler)",
                    "function": item["fn"],
                    "hook_address": f"0x{hook_addr:08x}",
                    "diff_register": diff_reg,
                    "zero_register": zero_reg,
                    "resc_command": (
                        f'sysbus.cpu AddHook 0x{hook_addr:08x} '
                        f'"self.SetRegisterUlong({zero_idx}, self.GetRegister({diff_idx}).RawValue)"'
                    ),
                }
            )
        elif len(stores_20) == 1 and len(stores_24) >= 1:
            # timer_kick() (standalone or LTO-inlined into sched_add_timer / timer_init / sched_main)
            rel_20, load_ins, zero_reg = stores_20[0]
            rel_24, _val_ins, val_reg = stores_24[0]
            zero_idx = ARM_REG_MAP[zero_reg]
            set_addr = load_ins["addr"]
            hooks.append(
                {
                    "klipper_site": "timer_kick (LOAD=64 guard)",
                    "function": item["fn"],
                    "hook_address": f"0x{set_addr:08x}",
                    "zero_register": zero_reg,
                    "resc_command": f'sysbus.cpu AddHook 0x{set_addr:08x} "self.SetRegisterUlong({zero_idx}, 64)"',
                }
            )
            if val_reg == zero_reg and (rel_24 + 1) < len(window):
                after_val_ins = window[rel_24 + 1]
                if zero_reg in ("r4", "r5", "r6", "r7", "r8", "r9", "sl", "fp"):
                    clr_addr = after_val_ins["addr"]
                    hooks.append(
                        {
                            "klipper_site": "timer_kick (restore callee-saved zero register)",
                            "function": item["fn"],
                            "hook_address": f"0x{clr_addr:08x}",
                            "zero_register": zero_reg,
                            "resc_command": (
                                f'sysbus.cpu AddHook 0x{clr_addr:08x} "self.SetRegisterUlong({zero_idx}, 0)"'
                            ),
                        }
                    )

        if len(hooks) >= limit:
            hooks = hooks[:limit]
            truncated = True
            break

    resc_lines = [f"sysbus.cpu VectorTableOffset 0x{vector_table_offset:08x}"] + [h["resc_command"] for h in hooks]
    return {
        "elf_path": str(elf_file),
        "vector_table_offset": f"0x{vector_table_offset:08x}",
        "bootloader_offset_warning": (
            None
            if vector_table_offset == 0x08000000
            else (
                f"ELF links at 0x{vector_table_offset:08x} (bootloader offset enabled). "
                f"Set 'sysbus.cpu VectorTableOffset 0x{vector_table_offset:08x}' in .resc or rebuild with "
                "CONFIG_STM32_FLASH_START_0000=y to avoid Renode 'PC and SP are equal to zero' CPU halt."
            )
        ),
        "total_instructions_scanned": len(parsed),
        "hooks_found": len(hooks),
        "truncated_by_limit": truncated,
        "limit": limit,
        "hooks": hooks,
        "resc_snippet": "\n".join(resc_lines),
    }


def _parse_temp_res_pair(pair_text: str) -> tuple[float, float]:
    parts = pair_text.split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected '<temp_c>:<resistance_ohm>', got '{pair_text}'")
    temp_c = float(parts[0])
    res_ohm = float(parts[1])
    if res_ohm <= 0.0:
        raise ValueError(f"Thermistor resistance must be > 0 ohm, got {res_ohm}")
    return temp_c, res_ohm


def synth_thermistor_adc(
    t1_r1: str,
    t2_r2: str,
    t3_r3: str,
    pullup_ohm: float,
    adc_max: int,
    temps_csv: str,
    limit: int,
) -> dict[str, Any]:
    """Match klippy/extras/thermistor.py Steinhart-Hart math to synthesize exact MCU ADC counts."""
    if limit <= 0:
        raise ValueError("--limit must be a positive integer")
    if pullup_ohm <= 0.0 or adc_max <= 0:
        raise ValueError("--pullup and --adc-max must be positive")

    t1, r1 = _parse_temp_res_pair(t1_r1)
    t2, r2 = _parse_temp_res_pair(t2_r2)
    t3, r3 = _parse_temp_res_pair(t3_r3)

    kelvin_offset = 273.15
    inv_t1 = 1.0 / (t1 + kelvin_offset)
    inv_t2 = 1.0 / (t2 + kelvin_offset)
    inv_t3 = 1.0 / (t3 + kelvin_offset)

    ln_r1, ln_r2, ln_r3 = math.log(r1), math.log(r2), math.log(r3)
    ln3_r1, ln3_r2, ln3_r3 = ln_r1**3, ln_r2**3, ln_r3**3

    inv_t12, inv_t13 = inv_t1 - inv_t2, inv_t1 - inv_t3
    ln_r12, ln_r13 = ln_r1 - ln_r2, ln_r1 - ln_r3
    ln3_r12, ln3_r13 = ln3_r1 - ln3_r2, ln3_r1 - ln3_r3

    denom = ln3_r12 - ln3_r13 * (ln_r12 / ln_r13)
    if abs(denom) < 1e-18:
        raise ValueError("Degenerate thermistor table calibration points")

    c3 = (inv_t12 - inv_t13 * (ln_r12 / ln_r13)) / denom
    c2 = (inv_t12 - c3 * ln3_r12) / ln_r12
    c1 = inv_t1 - c2 * ln_r1 - c3 * ln3_r1

    raw_temps = [float(tok.strip()) for tok in temps_csv.split(",") if tok.strip()]
    if not raw_temps:
        raise ValueError("--temps must provide at least one temperature")

    truncated = len(raw_temps) > limit
    selected_temps = raw_temps[:limit]

    samples: list[dict[str, Any]] = []
    for temp_c in selected_temps:
        inv_t = 1.0 / (temp_c + kelvin_offset)
        y_term = (c1 - inv_t) / (2.0 * c3)
        radicand = (c2 / (3.0 * c3)) ** 3 + y_term**2
        if radicand < 0.0:
            raise ValueError(f"Negative Steinhart-Hart radicand at {temp_c}C")
        x_term = math.sqrt(radicand)
        ln_r = math.copysign(abs(x_term - y_term) ** (1.0 / 3.0), x_term - y_term) - math.copysign(
            abs(x_term + y_term) ** (1.0 / 3.0), x_term + y_term
        )
        res_ohm = math.exp(ln_r)
        adc_frac = res_ohm / (pullup_ohm + res_ohm)
        adc_code = max(0, min(adc_max, int(round(adc_frac * adc_max))))
        samples.append(
            {
                "temp_c": round(temp_c, 4),
                "resistance_ohm": round(res_ohm, 3),
                "adc_fraction": round(adc_frac, 6),
                "adc_code": adc_code,
                "stimulus_entry_ch0": f"0={adc_code}",
            }
        )

    return {
        "steinhart_hart_coeffs": {"c1": c1, "c2": c2, "c3": c3},
        "pullup_resistor_ohm": pullup_ohm,
        "adc_max": adc_max,
        "klipper_pid_settling_protocol": {
            "min_idle_settle_sec": 6.5,
            "reason": (
                "Klipper ControlPID subtracts Kd * smoothed_dT_dt. Booting with ADC already near T_target "
                "and waiting >=6.5s with target=0 lets prev_temp_deriv decay to 0 while temp_integ stays clamped "
                "at 0, allowing deterministic testing of 0 < last_pwm_value < min_pwm_change (0.05)."
            ),
        },
        "truncated_by_limit": truncated,
        "limit": limit,
        "samples": samples,
    }


def _compute_wave_metrics(edges: list[tuple[float, int]]) -> dict[str, Any]:
    if not edges:
        return {
            "edge_count": 0,
            "span_sec": 0.0,
            "high_pulses": 0,
            "pulse_width_mean_sec": 0.0,
            "pulse_width_variance_sec2": 0.0,
            "freq_mean_hz": 0.0,
            "freq_variance_hz2": 0.0,
            "duty_cycle_est": 0.0,
            "first_edges": [],
            "last_edges": [],
        }

    span_sec = edges[-1][0] - edges[0][0] if len(edges) >= 2 else 0.0
    rising_times: list[float] = []
    high_widths: list[float] = []
    last_rise: float | None = None

    for t_sec, bit_val in edges:
        if bit_val == 1:
            rising_times.append(t_sec)
            last_rise = t_sec
        elif bit_val == 0 and last_rise is not None:
            width = t_sec - last_rise
            if width >= 0.0:
                high_widths.append(width)
            last_rise = None

    periods = [
        rising_times[i + 1] - rising_times[i]
        for i in range(len(rising_times) - 1)
        if rising_times[i + 1] > rising_times[i]
    ]
    freqs = [1.0 / p for p in periods if p > 0.0]

    pw_mean = statistics.fmean(high_widths) if high_widths else 0.0
    pw_var = statistics.variance(high_widths) if len(high_widths) >= 2 else 0.0
    freq_mean = statistics.fmean(freqs) if freqs else 0.0
    freq_var = statistics.variance(freqs) if len(freqs) >= 2 else 0.0
    duty_est = (sum(high_widths) / span_sec) if span_sec > 0.0 and high_widths else 0.0

    return {
        "edge_count": len(edges),
        "span_sec": round(span_sec, 6),
        "high_pulses": len(high_widths),
        "pulse_width_mean_sec": round(pw_mean, 6),
        "pulse_width_variance_sec2": round(pw_var, 9),
        "freq_mean_hz": round(freq_mean, 4),
        "freq_variance_hz2": round(freq_var, 6),
        "duty_cycle_est": round(min(1.0, max(0.0, duty_est)), 6),
        "first_edges": [{"t_sec": t, "level": b} for t, b in edges[:6]],
        "last_edges": [{"t_sec": t, "level": b} for t, b in edges[-6:]],
    }


def parse_gpio_trace(
    trace_path: str,
    pins_spec: str,
    split_time: float,
    limit: int,
) -> dict[str, Any]:
    """Decode STM32/Cortex-M GPIO ODR/BSRR/BRR write traces and classify Klipper post-off behavior."""
    if limit <= 0:
        raise ValueError("--limit must be a positive integer")
    trace_file = Path(trace_path).expanduser().resolve()
    if not trace_file.is_file():
        raise FileNotFoundError(f"GPIO trace file not found: {trace_file}")

    watched: dict[tuple[str, int], str] = {}
    for entry in pins_spec.split(","):
        tok = entry.strip()
        if not tok:
            continue
        parts = tok.split(":")
        if len(parts) != 3:
            raise ValueError(f"Invalid --pins token '{tok}'. Expected PORT:BIT:LABEL (e.g., C:8:HEATER_E0)")
        port_id = parts[0].strip().upper()
        bit_idx = int(parts[1].strip())
        if not (0 <= bit_idx <= 31):
            raise ValueError(f"GPIO bit index out of range [0..31]: {bit_idx}")
        watched[(port_id, bit_idx)] = parts[2].strip()

    if not watched:
        raise ValueError("At least one pin specification is required in --pins")

    odr_state: dict[str, int] = {}
    all_edges: dict[str, list[tuple[float, int]]] = {label: [] for label in watched.values()}
    pre_edges: dict[str, list[tuple[float, int]]] = {label: [] for label in watched.values()}
    post_edges: dict[str, list[tuple[float, int]]] = {label: [] for label in watched.values()}

    lines_processed = 0
    truncated = False

    with trace_file.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if lines_processed >= limit:
                truncated = True
                break
            cols = stripped.split()
            if len(cols) != 4:
                raise ValueError(f"Malformed GPIO trace at {trace_file}:{line_no}: '{stripped}'")
            try:
                t_sec = float(cols[0])
                port_id = cols[1].upper()
                offset = int(cols[2], 0)
                val = int(cols[3], 0)
            except ValueError as exc:
                raise ValueError(f"Numeric parse error at {trace_file}:{line_no} ('{stripped}'): {exc}") from exc

            old_odr = odr_state.get(port_id, 0)
            if offset == 0x0C:
                new_odr = val & 0xFFFF
            elif offset == 0x10:
                set_mask = val & 0xFFFF
                rst_mask = (val >> 16) & 0xFFFF
                new_odr = (old_odr | set_mask) & (~rst_mask & 0xFFFF)
            elif offset == 0x14:
                new_odr = old_odr & (~(val & 0xFFFF) & 0xFFFF)
            else:
                lines_processed += 1
                continue

            odr_state[port_id] = new_odr
            diff_mask = old_odr ^ new_odr
            if diff_mask:
                for (w_port, w_bit), label in watched.items():
                    if w_port == port_id and ((diff_mask >> w_bit) & 1):
                        level = (new_odr >> w_bit) & 1
                        ev = (round(t_sec, 6), level)
                        all_edges[label].append(ev)
                        if t_sec < split_time:
                            pre_edges[label].append(ev)
                        else:
                            post_edges[label].append(ev)
            lines_processed += 1

    pin_reports: dict[str, Any] = {}
    for (w_port, w_bit), label in sorted(watched.items(), key=lambda kv: kv[1]):
        final_level = (odr_state.get(w_port, 0) >> w_bit) & 1
        after_metrics = _compute_wave_metrics(post_edges[label])
        post_span = after_metrics["span_sec"]
        post_cnt = after_metrics["edge_count"]
        if post_cnt == 0:
            classification = "immediate_halt_zero_post_edges"
        elif post_span <= 0.45:
            classification = "bounded_pwm_delay_flush (<= ~0.3s)"
        elif post_span <= 5.2:
            classification = "bounded_max_schedule_time_tail (<= MAX_SCHEDULE_TIME 5.0s, SF_DONE)"
        else:
            classification = "persistent_unstopped_pwm_channel (survives heater off; requires explicit 0 or FIRMWARE_RESTART)"

        pin_reports[label] = {
            "port": w_port,
            "bit": w_bit,
            "final_odr_level": final_level,
            "klipper_post_split_classification": classification,
            "overall": _compute_wave_metrics(all_edges[label]),
            "before_split": _compute_wave_metrics(pre_edges[label]),
            "after_split": after_metrics,
        }

    return {
        "trace_file": str(trace_file),
        "lines_processed": lines_processed,
        "truncated_by_limit": truncated,
        "limit": limit,
        "split_time_sec": split_time,
        "pins": pin_reports,
    }


def _series_stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "mean": 0.0, "min": 0.0, "max": 0.0, "variance": 0.0}
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "variance": round(statistics.variance(values), 9) if len(values) >= 2 else 0.0,
    }


def analyze_klippy_stats(log_path: str, split_time: float, limit: int) -> dict[str, Any]:
    """Extract mcu_awake, mcu_task_avg, and heater pwm series + variances from klippy.log Stats lines."""
    if limit <= 0:
        raise ValueError("--limit must be a positive integer")
    log_file = Path(log_path).expanduser().resolve()
    if not log_file.is_file():
        raise FileNotFoundError(f"klippy.log not found: {log_file}")

    stats_re = re.compile(r"^Stats\s+([0-9.]+):(.*)$")
    awake_re = re.compile(r"\bmcu_awake=([0-9.]+)")
    task_avg_re = re.compile(r"\bmcu_task_avg=([0-9.]+)")
    task_std_re = re.compile(r"\bmcu_task_stddev=([0-9.]+)")
    heater_re = re.compile(
        r"\b([A-Za-z0-9_]+):\s*(?:target=[0-9.-]+\s+temp=[0-9.-]+|temp=[0-9.-]+\s+target=[0-9.-]+)\s+pwm=([0-9.]+)"
    )

    pre_awake: list[float] = []
    post_awake: list[float] = []
    pre_task_avg: list[float] = []
    post_task_avg: list[float] = []
    pre_task_std: list[float] = []
    post_task_std: list[float] = []
    heater_pwm_pre: dict[str, list[float]] = {}
    heater_pwm_post: dict[str, list[float]] = {}
    shutdown_reasons: list[str] = []

    stats_parsed = 0
    truncated = False
    first_ts: float | None = None

    with log_file.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if "MCU '" in line and "shutdown:" in line:
                shutdown_reasons.append(line)
            m_st = stats_re.match(line)
            if not m_st:
                continue
            if stats_parsed >= limit:
                truncated = True
                break

            abs_ts = float(m_st.group(1))
            if first_ts is None:
                first_ts = abs_ts
            rel_ts = abs_ts - first_ts
            body = m_st.group(2)

            is_pre = rel_ts < split_time
            m_aw = awake_re.search(body)
            if m_aw:
                (pre_awake if is_pre else post_awake).append(float(m_aw.group(1)))
            m_ta = task_avg_re.search(body)
            if m_ta:
                (pre_task_avg if is_pre else post_task_avg).append(float(m_ta.group(1)))
            m_ts = task_std_re.search(body)
            if m_ts:
                (pre_task_std if is_pre else post_task_std).append(float(m_ts.group(1)))
            for m_ht in heater_re.finditer(body):
                h_name = m_ht.group(1)
                pwm_val = float(m_ht.group(2))
                bucket = heater_pwm_pre if is_pre else heater_pwm_post
                bucket.setdefault(h_name, []).append(pwm_val)

            stats_parsed += 1

    heaters_summary: dict[str, Any] = {}
    for h_name in sorted(set(heater_pwm_pre.keys()) | set(heater_pwm_post.keys())):
        heaters_summary[h_name] = {
            "before_split_pwm": _series_stats(heater_pwm_pre.get(h_name, [])),
            "after_split_pwm": _series_stats(heater_pwm_post.get(h_name, [])),
        }

    return {
        "log_file": str(log_file),
        "stats_lines_parsed": stats_parsed,
        "truncated_by_limit": truncated,
        "limit": limit,
        "relative_split_time_sec": split_time,
        "mcu_shutdown_events": shutdown_reasons[:20],
        "mcu_awake": {
            "before_split": _series_stats(pre_awake),
            "after_split": _series_stats(post_awake),
        },
        "mcu_task_avg": {
            "before_split": _series_stats(pre_task_avg),
            "after_split": _series_stats(post_task_avg),
        },
        "mcu_task_stddev": {
            "before_split": _series_stats(pre_task_std),
            "after_split": _series_stats(post_task_std),
        },
        "heaters": heaters_summary,
    }


def _drain_socket_bytes(sock: socket.socket) -> bytes:
    """Read all currently buffered bytes from a TCP socket via select() without silent exception swallowing."""
    chunks = bytearray()
    while True:
        readable, _, _ = select.select([sock], [], [], 0.0)
        if not readable:
            break
        part = sock.recv(65536)
        if not part:
            break
        chunks.extend(part)
    return bytes(chunks)


def probe_renode(
    host: str,
    port: int,
    commands: str,
    samples: int,
    interval_ms: int,
    limit: int,
) -> dict[str, Any]:
    """Sample Renode monitor TCP socket safely with 16-space prefix and Telnet IAC stripping."""
    if limit <= 0 or samples <= 0:
        raise ValueError("--limit and --samples must be positive integers")
    capped_samples = min(samples, limit)
    truncated = samples > limit

    records: list[dict[str, Any]] = []
    try:
        with socket.create_connection((host, port), timeout=3.0) as sock:
            time.sleep(0.25)
            _ = _drain_socket_bytes(sock)

            for idx in range(capped_samples):
                t_wall = time.monotonic()
                cmd_count = max(1, len([c for c in commands.split(";") if c.strip()]))
                min_wait = max(0.08, 0.06 * cmd_count)
                payload = (" " * 16 + commands.strip() + "\r\n").encode("utf-8")
                sock.sendall(payload)
                time.sleep(max(min_wait, interval_ms / 1000.0))

                raw_reply = _drain_socket_bytes(sock)
                decoded = strip_telnet_iac(raw_reply).decode("utf-8", errors="replace").strip()
                # Standalone result lines (e.g. "0x000000B4") exclude echoed command addresses ("ReadDoubleWord 0x4000541C")
                result_hex_vals = re.findall(r"(?m)^\s*(0x[0-9A-Fa-f]+)\s*$", decoded)
                hex_vals = re.findall(r"0x[0-9A-Fa-f]+", decoded)
                vt_match = re.search(r"Elapsed Virtual Time:\s*([0-9:.]+)", decoded)
                records.append(
                    {
                        "sample_index": idx,
                        "monotonic_sec": round(t_wall, 6),
                        "virtual_time": vt_match.group(1) if vt_match else None,
                        "result_hex_values": result_hex_vals,
                        "hex_tokens": hex_vals,
                        "raw_response": decoded,
                    }
                )
    except OSError as exc:
        raise RuntimeError(f"Renode monitor connection/read failed for {host}:{port}: {exc}") from exc

    return {
        "endpoint": f"{host}:{port}",
        "commands": commands,
        "samples_requested": samples,
        "samples_collected": len(records),
        "truncated_by_limit": truncated,
        "limit": limit,
        "records": records,
    }


def sanitize_audit(target_path: str, deny_terms_csv: str, limit: int) -> tuple[dict[str, Any], int]:
    """Verify zero personal, host, credential, or hardware-specific deny-term leaks."""
    if limit <= 0:
        raise ValueError("--limit must be a positive integer")
    root = Path(target_path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Sanitization target does not exist: {root}")

    dynamic_tokens: dict[str, str] = {}
    for label, val in (
        ("os_username", getpass.getuser()),
        ("home_dirname", Path.home().name),
        ("hostname", socket.gethostname()),
    ):
        cleaned = (val or "").strip()
        if len(cleaned) >= 3 and cleaned.lower() not in ("root", "user", "admin", "ubuntu", "runner"):
            dynamic_tokens[label] = cleaned

    custom_terms = [t.strip() for t in deny_terms_csv.split(",") if t.strip()]

    regex_rules: list[tuple[str, re.Pattern[str]]] = [
        ("hardcoded_windows_user_path", re.compile(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9._-]+", re.IGNORECASE)),
        ("hardcoded_wsl_mnt_user_path", re.compile(r"/mnt/[a-z]/Users/[A-Za-z0-9._-]+", re.IGNORECASE)),
        (
            "api_or_pat_secret",
            re.compile(
                r"\b(?:sk-proj-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,})\b"
            ),
        ),
        ("private_key_pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
        (
            "personal_email_non_noreply",
            re.compile(
                r"\b[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com\b|example\.com\b|koconnor\.net\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
            ),
        ),
        (
            "session_uuid_v4",
            re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE),
        ),
    ]

    for label, tok in dynamic_tokens.items():
        regex_rules.append((f"dynamic_{label}", re.compile(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", re.IGNORECASE)))

    for term in custom_terms:
        regex_rules.append((f"deny_term:{term}", re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE)))

    files_to_scan = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)

    findings: list[dict[str, Any]] = []
    scanned_files = 0
    truncated = False

    for file_item in files_to_scan:
        try:
            text = file_item.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise RuntimeError(f"Failed reading {file_item} during sanitize-audit: {exc}") from exc

        scanned_files += 1
        rel_display = file_item.name if root.is_file() else str(file_item.relative_to(root)).replace("\\", "/")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for rule_name, pattern in regex_rules:
                match = pattern.search(line)
                if match:
                    findings.append(
                        {
                            "file": rel_display,
                            "line": line_no,
                            "rule": rule_name,
                            "matched_length": len(match.group(0)),
                        }
                    )
                    if len(findings) >= limit:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    report = {
        "status": "PASS" if len(findings) == 0 else "FAIL",
        "files_scanned": scanned_files,
        "dynamic_host_checks_count": len(dynamic_tokens),
        "custom_deny_terms_count": len(custom_terms),
        "total_findings": len(findings),
        "truncated_by_limit": truncated,
        "limit": limit,
        "findings": findings,
    }
    return report, (0 if len(findings) == 0 else 1)


def audit_pr_markdown(target_path: str, limit: int) -> tuple[dict[str, Any], int]:
    """Audit PR/comment markdown files for PowerShell/shell escaping corruption (\\ident\\, NUL truncation, unbalanced backticks, BOM/mojibake)."""
    if limit <= 0:
        raise ValueError("--limit must be a positive integer")
    root = Path(target_path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Markdown audit target does not exist: {root}")

    files = [root] if root.is_file() else sorted(p for p in root.rglob("*.md") if p.is_file())
    findings: list[dict[str, Any]] = []
    truncated = False

    def add_finding(rel_name: str, kind: str, detail: Any) -> bool:
        nonlocal truncated
        findings.append({"file": rel_name, "type": kind, "detail": detail})
        if len(findings) >= limit:
            truncated = True
            return True
        return False

    for fpath in files:
        raw_bytes = fpath.read_bytes()
        rel_name = fpath.name if root.is_file() else str(fpath.relative_to(root)).replace("\\", "/")
        if raw_bytes.startswith(b"\xef\xbb\xbf"):
            if add_finding(rel_name, "utf8_bom_present", "File starts with UTF-8 BOM (EF BB BF)"):
                break
        text = raw_bytes.decode("utf-8", errors="replace")
        if "\ufffd" in text:
            if add_finding(rel_name, "unicode_replacement_char", "Contains U+FFFD mojibake"):
                break

        ctrl = [hex(ord(ch)) for ch in text if ord(ch) < 32 and ch not in ("\r", "\n", "\t")]
        if ctrl:
            if add_finding(rel_name, "control_character_or_nul", ctrl[:10]):
                break

        fence_count = len(re.findall(r"^```", text, flags=re.MULTILINE))
        if fence_count % 2 != 0:
            if add_finding(rel_name, "unbalanced_code_fence", f"fence_count={fence_count}"):
                break

        without_fences = re.sub(r"```[\s\S]*?```", "", text)
        if text.rstrip().endswith("\\"):
            if add_finding(rel_name, "trailing_backslash_truncation", text[-80:]):
                break

        for p_idx, para in enumerate(re.split(r"\r?\n\r?\n", without_fences), start=1):
            # Remove valid multi-backtick (`` ... ``) and single-backtick (` ... `) code spans; any leftover ` is unbalanced
            leftover_ticks = re.sub(r"(``+)[^\r\n]*?\1|`[^`\r\n]+`", "", para)
            if "`" in leftover_ticks:
                if add_finding(rel_name, "unbalanced_inline_backticks", f"paragraph {p_idx} has unmatched backtick"):
                    break
        if truncated:
            break

        # Strip inline code spans and inline LaTeX ($...$) before checking prose for PowerShell \ident\ or `n/`0 corruption
        prose_only = re.sub(r"(``+)[^\r\n]*?\1|`[^`\r\n]+`|\$[^$\r\n]+\$", "", without_fences)
        bs_tokens = re.findall(r"\\[A-Za-z0-9_#().-]+\\", prose_only)
        if bs_tokens:
            if add_finding(rel_name, "powershell_backslash_wrapped_token", bs_tokens[:10]):
                break

        stray_ps = re.findall(r"`[nrt0]\b", prose_only)
        if stray_ps:
            if add_finding(rel_name, "stray_powershell_backtick_escape", stray_ps[:10]):
                break

    report = {
        "status": "PASS" if len(findings) == 0 else "FAIL",
        "files_scanned": len(files),
        "total_findings": len(findings),
        "truncated_by_limit": truncated,
        "limit": limit,
        "findings": findings,
    }
    return report, (0 if len(findings) == 0 else 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Klipper MCU Emulation & Hardware Schematic Forensics CLI (stdlib-only)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_hooks = sub.add_parser("resolve-armcm-hooks", help="Extract armcm_timer.c SysTick one-shot PC hooks from klipper.elf")
    p_hooks.add_argument("--elf", required=True, help="Path to compiled out/klipper.elf")
    p_hooks.add_argument("--objdump-bin", default="arm-none-eabi-objdump", help="Objdump binary name or path")
    p_hooks.add_argument("--limit", type=int, required=True, help="Maximum number of hooks to emit")
    p_hooks.add_argument("--output", required=True, help="Output JSON file path")

    p_ntc = sub.add_parser("synth-thermistor-adc", help="Synthesize Klipper Steinhart-Hart thermistor ADC counts")
    p_ntc.add_argument("--t1-r1", required=True, help="Point 1 as tempC:ohm (e.g. 25.0:100000)")
    p_ntc.add_argument("--t2-r2", required=True, help="Point 2 as tempC:ohm (e.g. 150.0:1641.9)")
    p_ntc.add_argument("--t3-r3", required=True, help="Point 3 as tempC:ohm (e.g. 250.0:226.15)")
    p_ntc.add_argument("--pullup", type=float, required=True, help="Klipper pullup_resistor in ohms (default 4700)")
    p_ntc.add_argument("--adc-max", type=int, required=True, help="MCU ADC_MAX count (e.g. 4095 for 12-bit STM32)")
    p_ntc.add_argument("--temps", required=True, help="Comma-separated target temperatures in Celsius")
    p_ntc.add_argument("--limit", type=int, required=True, help="Maximum temperature points to evaluate")
    p_ntc.add_argument("--output", required=True, help="Output JSON file path")

    p_gpio = sub.add_parser("parse-gpio-trace", help="Reconstruct GPIO ODR/BSRR edges and classify post-off behavior")
    p_gpio.add_argument("--trace", required=True, help="Path to <vtime> <port> <offset> <val> trace log")
    p_gpio.add_argument("--pins", required=True, help="Comma-separated PORT:BIT:LABEL specs")
    p_gpio.add_argument("--split-time", type=float, required=True, help="Virtual time (s) when heater/actuator off was sent")
    p_gpio.add_argument("--limit", type=int, required=True, help="Maximum trace lines to process")
    p_gpio.add_argument("--output", required=True, help="Output JSON file path")

    p_stats = sub.add_parser("analyze-klippy-stats", help="Profile mcu_awake, mcu_task_avg, and heater PWM from klippy.log")
    p_stats.add_argument("--log", required=True, help="Path to klippy.log")
    p_stats.add_argument("--split-time", type=float, required=True, help="Seconds from first Stats line separating pre/post event")
    p_stats.add_argument("--limit", type=int, required=True, help="Maximum Stats lines to parse")
    p_stats.add_argument("--output", required=True, help="Output JSON file path")

    p_mon = sub.add_parser("probe-renode", help="Sample Renode TCP monitor without Telnet IAC byte corruption")
    p_mon.add_argument("--host", default="127.0.0.1", help="Renode monitor host")
    p_mon.add_argument("--port", type=int, required=True, help="Renode monitor TCP port")
    p_mon.add_argument("--commands", required=True, help="Monitor commands (semicolon-separated)")
    p_mon.add_argument("--samples", type=int, required=True, help="Number of samples to capture")
    p_mon.add_argument("--interval-ms", type=int, required=True, help="Delay per sample in milliseconds")
    p_mon.add_argument("--limit", type=int, required=True, help="Hard cap on captured samples")
    p_mon.add_argument("--output", required=True, help="Output JSON file path")

    p_ds = sub.add_parser("fetch-datasheet", help="Fetch component datasheet/schematic at <= 1.0 req/s")
    p_ds.add_argument("--url", required=True, help="HTTPS URL of component datasheet")
    p_ds.add_argument("--dest", required=True, help="Destination file path")
    p_ds.add_argument("--limit", type=int, required=True, help="Maximum allowed byte size to write")
    p_ds.add_argument("--output", required=True, help="Output JSON metadata path")

    p_san = sub.add_parser("sanitize-audit", help="Verify zero personal/host/device-specific leaks")
    p_san.add_argument("--target", required=True, help="Directory or file path to scan")
    p_san.add_argument("--deny-terms", default="", help="Comma-separated board/printer/personal terms to forbid")
    p_san.add_argument("--limit", type=int, required=True, help="Maximum findings to record")
    p_san.add_argument("--output", required=True, help="Output JSON audit report path")

    p_prmd = sub.add_parser("audit-pr-markdown", help="Audit PR/comment markdown files for PowerShell backtick/NUL corruption")
    p_prmd.add_argument("--target", required=True, help="Markdown file or directory path to validate")
    p_prmd.add_argument("--limit", type=int, required=True, help="Maximum findings to record")
    p_prmd.add_argument("--output", required=True, help="Output JSON audit report path")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "resolve-armcm-hooks":
            result = resolve_armcm_hooks(args.elf, args.objdump_bin, args.limit)
            write_json_output(result, args.output)
        elif args.command == "synth-thermistor-adc":
            result = synth_thermistor_adc(
                args.t1_r1, args.t2_r2, args.t3_r3, args.pullup, args.adc_max, args.temps, args.limit
            )
            write_json_output(result, args.output)
        elif args.command == "parse-gpio-trace":
            result = parse_gpio_trace(args.trace, args.pins, args.split_time, args.limit)
            write_json_output(result, args.output)
        elif args.command == "analyze-klippy-stats":
            result = analyze_klippy_stats(args.log, args.split_time, args.limit)
            write_json_output(result, args.output)
        elif args.command == "probe-renode":
            result = probe_renode(
                args.host, args.port, args.commands, args.samples, args.interval_ms, args.limit
            )
            write_json_output(result, args.output)
        elif args.command == "fetch-datasheet":
            if args.limit <= 0:
                raise ValueError("--limit (max bytes) must be a positive integer")
            limiter = FileLockRateLimiter()
            raw_bytes, headers = limiter.fetch_bytes(args.url)
            truncated = len(raw_bytes) > args.limit
            saved_bytes = raw_bytes[: args.limit]
            dest_file = Path(args.dest).expanduser().resolve()
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_bytes(saved_bytes)
            write_json_output(
                {
                    "url": args.url,
                    "dest": str(dest_file),
                    "bytes_received": len(raw_bytes),
                    "bytes_written": len(saved_bytes),
                    "truncated_by_limit": truncated,
                    "limit": args.limit,
                    "content_type": headers.get("content-type", "application/octet-stream"),
                },
                args.output,
            )
        elif args.command == "sanitize-audit":
            report, exit_code = sanitize_audit(args.target, args.deny_terms, args.limit)
            write_json_output(report, args.output)
            if exit_code != 0:
                print(
                    f"Sanitization audit FAILED with {report['total_findings']} finding(s). Inspect {args.output}.",
                    file=sys.stderr,
                )
                sys.exit(exit_code)
        elif args.command == "audit-pr-markdown":
            report, exit_code = audit_pr_markdown(args.target, args.limit)
            write_json_output(report, args.output)
            if exit_code != 0:
                print(
                    f"PR markdown audit FAILED with {report['total_findings']} finding(s). Inspect {args.output}.",
                    file=sys.stderr,
                )
                sys.exit(exit_code)
        else:
            print(f"Unknown command: {args.command}", file=sys.stderr)
            sys.exit(1)
    except (RateLimitError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error [{args.command}]: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
