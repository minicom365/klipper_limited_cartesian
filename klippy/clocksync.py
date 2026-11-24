# -*- coding: utf-8 -*-
# Micro-controller clock synchronization
#
# Copyright (C) 2016-2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# [EN] Integrates pure-stdlib 1D DualLayerMAD v1.2.1 (Twin-Adjacent-Shell
# point-mass debiasing + 4-step truncated/mixture fixed-point) for ClockSync
# jitter variance, excluding NumPy/multidimensional estimators for <20us RT.
# [KO] ClockSync 예측 분산 추정에 순수 표준라이브러리 1D DualLayerMAD v1.2.1
# (점질량 편향 보정 및 4단계 절단·혼합 고정점 보정)을 결합하며, 20us 미만 실시간성
# 보장을 위해 외부 NumPy 및 다차원 추정기 의존성을 배제함.
import bisect, logging, math

RTT_AGE = .000010 / (60. * 60.)
DECAY = 1. / 30.
TRANSMIT_EXTRA = .001

class DualLayerMAD(object):
    """
    [EN] Real-time streaming Dual-Layer MAD noise estimator immune to
    sudden step-edges via point-symmetry warmup and two-tier filtering.

    [KO] 점대칭 반사 웜업과 2단계 이상치 필터링을 통해 급격한 스텝 에지나
    데이터 꺾임에 강건한 실시간 스트리밍 2중 MAD 노이즈 추정기.
    """
    __slots__ = ('window_size', 'history')

    def __init__(self, window_size=32):
        if window_size < 4:
            raise ValueError("window_size must be at least 4.")
        self.window_size = int(window_size)
        self.history = []

    def reset(self):
        """Reset the internal history buffer."""
        del self.history[:]

    def update(self, value):
        """
        [EN] Push a new scalar observation into the rolling window.
        [KO] 새로운 스칼라 관측값을 롤링 윈도우에 추가.
        """
        self.history.append(float(value))
        if len(self.history) > self.window_size:
            self.history.pop(0)

    def get_std(self):
        """
        [EN] Estimate robust standard deviation (sigma) of noise background.
        [KO] 노이즈 배경의 강건한 절단 정규분포 보정 표준편차(sigma) 추정.
        """
        if not self.history:
            return 0.0

        # Point-symmetry warmup at t=0
        # (fill negative time window across (0, v0))
        if len(self.history) < self.window_size:
            v0 = self.history[0]
            # Reflected virtual samples in negative time: v_neg = 2*v0 - v
            virtual = [2.0 * v0 - x for x in reversed(self.history[1:])]
            sample_set = (virtual + self.history)[-self.window_size:]
        else:
            sample_set = self.history

        return _dual_mad_std_from_list(sample_set)

    def get_variance(self):
        """
        [EN] Estimate robust noise variance (sigma^2).
        [KO] 노이즈 배경의 강건한 분산(sigma^2) 추정.
        """
        s = self.get_std()
        return s * s

class ClockSync:
    def __init__(self, reactor):
        self.reactor = reactor
        self.serial = None
        self.get_clock_timer = reactor.register_timer(self._get_clock_event)
        self.get_clock_cmd = self.cmd_queue = None
        self.mcu_freq = 1.
        # Tracking of background requests (updated in bg thread)
        self.queries_pending = 0
        # 32bit to 64bit clock conversions (updated in bg thread)
        self.last_clock = 0
        # Linear regression of mcu clock and system sent_time (bg thread)
        self.time_avg = self.time_variance = 0.
        self.clock_avg = self.clock_covariance = 0.
        self.prediction_variance = 0.
        self.last_prediction_time = 0.
        # Minimum round-trip-time tracking (bg thread)
        self.min_half_rtt = 999999999.9
        self.min_rtt_time = 0.
        # System clock to mcu clock estimation (updated in bg thread)
        self.clock_est = (0., 0., 0.)
        self.dlmad = DualLayerMAD(window_size=32)
    def connect(self, serial):
        self.serial = serial
        self.mcu_freq = serial.msgparser.get_constant_float('CLOCK_FREQ')
        # Load initial clock and frequency
        params = serial.send_with_response('get_uptime', 'uptime')
        self.last_clock = (params['high'] << 32) | params['clock']
        self.clock_avg = self.last_clock
        self.time_avg = params['#sent_time']
        self.clock_est = (self.time_avg, self.clock_avg, self.mcu_freq)
        self.prediction_variance = (.001 * self.mcu_freq)**2
        # Enable periodic get_clock timer
        for i in range(8):
            self.reactor.pause(self.reactor.monotonic() + 0.050)
            self.last_prediction_time = -9999.
            params = serial.send_with_response('get_clock', 'clock')
            self._handle_clock(params)
        self.get_clock_cmd = serial.get_msgparser().create_command('get_clock')
        self.cmd_queue = serial.alloc_command_queue()
        serial.register_response(self._handle_clock, 'clock')
        self.reactor.update_timer(self.get_clock_timer, self.reactor.NOW)
    def connect_file(self, serial, pace=False):
        self.serial = serial
        self.mcu_freq = serial.msgparser.get_constant_float('CLOCK_FREQ')
        self.clock_est = (0., 0., self.mcu_freq)
        freq = 1000000000000.
        if pace:
            freq = self.mcu_freq
        serial.set_clock_est(freq, self.reactor.monotonic(), 0)
    # MCU clock querying (_handle_clock is invoked from background thread)
    def _get_clock_event(self, eventtime):
        self.serial.raw_send(self.get_clock_cmd, 0, 0, self.cmd_queue)
        self.queries_pending += 1
        # Use an unusual time for the next event so clock messages
        # don't resonate with other periodic events.
        return eventtime + .9839
    def _update_regression(self, sent_time, clock, half_rtt=0.0):
        # Calculate expected clock using existing time/clock regression
        old_freq = self.clock_est[2] # self.clock_covariance/self.time_variance
        exp_clock = (sent_time - self.time_avg) * old_freq + self.clock_avg
        # Track prediction accuracy and filter out extreme outliers
        clock_diff2 = (clock - exp_clock)**2
        if (clock_diff2 > 25. * self.prediction_variance
            and clock_diff2 > (.000500 * self.mcu_freq)**2):
            if clock > exp_clock and sent_time < self.last_prediction_time+10.:
                logging.debug("Ignoring clock sample %.3f:"
                              " freq=%d diff=%d stddev=%.3f",
                              sent_time, old_freq, clock - exp_clock,
                              math.sqrt(self.prediction_variance))
                return False
            logging.info("Resetting prediction variance %.3f:"
                         " freq=%d diff=%d stddev=%.3f",
                         sent_time, old_freq, clock - exp_clock,
                         math.sqrt(self.prediction_variance))
            self.prediction_variance = (.001 * self.mcu_freq)**2
            self.dlmad.reset()
        else:
            self.last_prediction_time = sent_time
            # Inject DLMAD v1.2.1 for unbiased variance calculation
            self.dlmad.update(clock - exp_clock)
            dlmad_std = self.dlmad.get_std()
            if dlmad_std >= 1.0:
                self.prediction_variance = dlmad_std * dlmad_std
            else:
                self.prediction_variance = (
                    (1. - DECAY) * (
                        self.prediction_variance + clock_diff2 * DECAY
                    )
                )
        # Add clock and sent_time to linear regression
        diff_sent_time = sent_time - self.time_avg
        self.time_avg += DECAY * diff_sent_time
        self.time_variance = (1. - DECAY) * (
            self.time_variance + diff_sent_time**2 * DECAY)
        diff_clock = clock - self.clock_avg
        self.clock_avg += DECAY * diff_clock
        self.clock_covariance = (1. - DECAY) * (
            self.clock_covariance + diff_sent_time * diff_clock * DECAY)
        return True
    def _update_best_rtt(self, sent_time, receive_time):
        # Check if this is the best round-trip-time seen so far
        half_rtt = .5 * (receive_time - sent_time)
        aged_rtt = (sent_time - self.min_rtt_time) * RTT_AGE
        if half_rtt < self.min_half_rtt + aged_rtt:
            self.min_half_rtt = half_rtt
            self.min_rtt_time = sent_time
            logging.debug("new minimum rtt %.3f: hrtt=%.6f freq=%d",
                          sent_time, half_rtt, self.clock_est[2])
    def _handle_clock(self, params):
        self.queries_pending = 0
        # Extend clock to 64bit
        last_clock = self.last_clock
        clock_delta = (params['clock'] - last_clock) & 0xffffffff
        self.last_clock = clock = last_clock + clock_delta
        # Determine message timing
        sent_time = params['#sent_time']
        receive_time = params['#receive_time']
        if not sent_time:
            # sent_time unknown because of 'get_clock' retransmit
            return
        # Update sent_time to clock regression
        ret = self._update_regression(sent_time, clock)
        if not ret:
            # Message is an "outlier" and should be discarded
            return
        # Update serialqueue message release timing
        new_freq = self.clock_covariance / self.time_variance
        pred_stddev = math.sqrt(self.prediction_variance)
        self.serial.set_clock_est(new_freq, self.time_avg + TRANSMIT_EXTRA,
                                  int(self.clock_avg - 3. * pred_stddev))
        # Update time translation (mainly for multi-mcu synchronization)
        self._update_best_rtt(sent_time, receive_time)
        self.clock_est = (self.time_avg + self.min_half_rtt,
                          self.clock_avg, new_freq)
    # clock frequency conversions
    def print_time_to_clock(self, print_time):
        return int(print_time * self.mcu_freq)
    def clock_to_print_time(self, clock):
        return clock / self.mcu_freq
    # system time conversions
    def get_clock(self, eventtime):
        sample_time, clock, freq = self.clock_est
        return int(clock + (eventtime - sample_time) * freq)
    def estimate_clock_systime(self, reqclock):
        sample_time, clock, freq = self.clock_est
        return float(reqclock - clock)/freq + sample_time
    def estimated_print_time(self, eventtime):
        return self.clock_to_print_time(self.get_clock(eventtime))
    # misc commands
    def clock32_to_clock64(self, clock32):
        last_clock = self.last_clock
        clock_diff = (clock32 - last_clock) & 0xffffffff
        clock_diff -= (clock_diff & 0x80000000) << 1
        return last_clock + clock_diff
    def is_active(self):
        return self.queries_pending <= 4
    def dump_debug(self):
        sample_time, clock, freq = self.clock_est
        return ("clocksync state: mcu_freq=%d last_clock=%d"
                " clock_est=(%.3f %d %.3f) min_half_rtt=%.6f min_rtt_time=%.3f"
                " time_avg=%.3f(%.3f) clock_avg=%.3f(%.3f)"
                " pred_variance=%.3f" % (
                    self.mcu_freq, self.last_clock, sample_time, clock, freq,
                    self.min_half_rtt, self.min_rtt_time,
                    self.time_avg, self.time_variance,
                    self.clock_avg, self.clock_covariance,
                    self.prediction_variance))
    def stats(self, eventtime):
        sample_time, clock, freq = self.clock_est
        return "freq=%d" % (freq,)
    def calibrate_clock(self, print_time, eventtime):
        return (0., self.mcu_freq)

# Clock syncing code for secondary MCUs (whose clocks are sync'ed to a
# primary MCU)
class SecondarySync(ClockSync):
    def __init__(self, reactor, main_sync):
        ClockSync.__init__(self, reactor)
        self.main_sync = main_sync
        self.clock_adj = (0., 1.)
        self.last_sync_time = 0.
    def connect(self, serial):
        ClockSync.connect(self, serial)
        self.clock_adj = (0., self.mcu_freq)
        curtime = self.reactor.monotonic()
        main_print_time = self.main_sync.estimated_print_time(curtime)
        local_print_time = self.estimated_print_time(curtime)
        self.clock_adj = (main_print_time - local_print_time, self.mcu_freq)
        self.calibrate_clock(0., curtime)
    def connect_file(self, serial, pace=False):
        ClockSync.connect_file(self, serial, pace)
        self.clock_adj = (0., self.mcu_freq)
    # clock frequency conversions
    def print_time_to_clock(self, print_time):
        adjusted_offset, adjusted_freq = self.clock_adj
        return int((print_time - adjusted_offset) * adjusted_freq)
    def clock_to_print_time(self, clock):
        adjusted_offset, adjusted_freq = self.clock_adj
        return clock / adjusted_freq + adjusted_offset
    # misc commands
    def dump_debug(self):
        adjusted_offset, adjusted_freq = self.clock_adj
        return "%s clock_adj=(%.3f %.3f)" % (
            ClockSync.dump_debug(self), adjusted_offset, adjusted_freq)
    def stats(self, eventtime):
        adjusted_offset, adjusted_freq = self.clock_adj
        return "%s adj=%d" % (ClockSync.stats(self, eventtime), adjusted_freq)
    def calibrate_clock(self, print_time, eventtime):
        # Calculate: est_print_time = main_sync.estimatated_print_time()
        ser_time, ser_clock, ser_freq = self.main_sync.clock_est
        main_mcu_freq = self.main_sync.mcu_freq
        est_main_clock = (eventtime - ser_time) * ser_freq + ser_clock
        est_print_time = est_main_clock / main_mcu_freq
        # Determine sync1_print_time and sync2_print_time
        sync1_print_time = max(print_time, est_print_time)
        sync2_print_time = max(sync1_print_time + 4., self.last_sync_time,
                               print_time + 2.5 * (print_time - est_print_time))
        # Calc sync2_sys_time (inverse of main_sync.estimatated_print_time)
        sync2_main_clock = sync2_print_time * main_mcu_freq
        sync2_sys_time = ser_time + (sync2_main_clock - ser_clock) / ser_freq
        # Adjust freq so estimated print_time will match at sync2_print_time
        sync1_clock = self.print_time_to_clock(sync1_print_time)
        sync2_clock = self.get_clock(sync2_sys_time)
        adjusted_freq = ((sync2_clock - sync1_clock)
                         / (sync2_print_time - sync1_print_time))
        adjusted_offset = sync1_print_time - sync1_clock / adjusted_freq
        # Apply new values
        self.clock_adj = (adjusted_offset, adjusted_freq)
        self.last_sync_time = sync2_print_time
        return self.clock_adj


_SQRT2 = 1.4142135623730951

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / _SQRT2))

_INV_SQRT2PI = 0.3989422804014327

def _norm_pdf(x):
    return math.exp(-0.5 * x * x) * _INV_SQRT2PI

def _norm_ppf(p):
    z = 0.0
    for _ in range(6):
        err = _norm_cdf(z) - p
        pdf = _norm_pdf(z)
        z -= err / (pdf + 0.5 * z * err)
    return z

_Z75 = _norm_ppf(0.75)

_C_GAUSS = 1.0 / _Z75

def _debias_mad1_point_mass(mad1, p0_hat):
    if p0_hat <= 0.0:
        return mad1
    p_eff = p0_hat if p0_hat < 0.49 else 0.49
    z_zi = _norm_ppf(1.0 - 0.25 / (1.0 - p_eff))
    return mad1 * (_Z75 / z_zi)

def _excess_point_mass_list(sorted_devs, tol):
    n = len(sorted_devs)
    first_gt = bisect.bisect_right(sorted_devs, tol)
    if first_gt < 2 or first_gt >= n:
        return first_gt, 0.0
    nz_cnt = n - first_gt
    ref_off = min(nz_cnt - 1, min(3, nz_cnt // 150))
    d_ref = sorted_devs[first_gt + ref_off]
    n1 = bisect.bisect_right(sorted_devs, 1.5 * d_ref, first_gt, n) - first_gt
    if 2 * n1 >= first_gt:
        return first_gt, 0.0
    return first_gt, float(first_gt) / float(n)

def _median_sorted_range(s, offset, count):
    mid = offset + (count >> 1)
    if count & 1:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])

def _median_sorted(s):
    return _median_sorted_range(s, 0, len(s))

_C0_TAU25 = 2.5 * _Z75

_Q0_TAU25 = _norm_ppf(0.25 + 0.5 * _norm_cdf(_C0_TAU25))

def _truncated_mad_fixed_point(mad2, r1):
    sig = mad2 / _Q0_TAU25
    r1_floor = 1e-12 * max(r1, 1.0)
    for _ in range(4):
        c_eff = max(_C0_TAU25, r1 / max(sig, r1_floor))
        sig = mad2 / _norm_ppf(0.25 + 0.5 * _norm_cdf(c_eff))
    return sig

def _mixture_mad_fixed_point(mad2, r1, p0_in):
    if p0_in <= 0.0:
        return _truncated_mad_fixed_point(mad2, r1)
    w_in = (1.0 - 2.0 * p0_in) / (4.0 * (1.0 - p0_in))
    sig = mad2 / _norm_ppf(0.5 + w_in * (2.0 * _norm_cdf(_C0_TAU25) - 1.0))
    r1_floor = 1e-12 * max(r1, 1.0)
    for _ in range(4):
        c_eff = max(_C0_TAU25, r1 / max(sig, r1_floor))
        sig = mad2 / _norm_ppf(0.5 + w_in * (2.0 * _norm_cdf(c_eff) - 1.0))
    return sig

def _dual_mad_std_from_list(sample_set):
    n = len(sample_set)
    if n < 2:
        return 0.0

    sorted_history = sorted(sample_set)
    rng = sorted_history[-1] - sorted_history[0]
    if math.isnan(rng):
        return float("nan")
    if rng <= 0.0:
        return 0.0

    tol = 1e-12 * rng
    min_nz = 2 if n < 16 else max(3, n // 20)
    med = _median_sorted(sorted_history)
    abs_devs = [abs(x - med) for x in sample_set]
    sorted_abs_devs = sorted(abs_devs)
    mad1 = _median_sorted(sorted_abs_devs)
    first_gt, p0_hat = _excess_point_mass_list(sorted_abs_devs, tol)

    if mad1 <= tol or (first_gt * 2 >= n) or p0_hat >= 0.49:
        nz_count = n - first_gt
        if nz_count < min_nz:
            return 0.0
        eff_mad1 = _median_sorted_range(sorted_abs_devs, first_gt, nz_count)
        r1 = 2.5 * eff_mad1
        nzi_end = bisect.bisect_right(sorted_abs_devs, r1, first_gt, n)
        nzi_count = nzi_end - first_gt
        if nzi_count < 2:
            return eff_mad1 * _C_GAUSS
        nzi_mad = _median_sorted_range(sorted_abs_devs, first_gt, nzi_count)
        return _truncated_mad_fixed_point(nzi_mad, r1)

    # Level 1: Zero-inflation-debiased step-edge & outlier gate (50% breakdown)
    mad1_eff = _debias_mad1_point_mass(mad1, p0_hat)
    r1 = 2.5 * mad1_eff
    candidates = [
        x for x, dev in zip(sample_set, abs_devs) if dev <= r1
    ]

    c_len = len(candidates)
    if c_len < 2:
        return mad1_eff * _C_GAUSS

    # Level 2: Pure noise deviation estimation on filtered inliers
    sorted_candidates = sorted(candidates)
    pure_med = _median_sorted(sorted_candidates)

    pure_abs_devs = sorted(abs(x - pure_med) for x in candidates)
    pure_mad = _median_sorted(pure_abs_devs)
    first_gt2, p0_in = _excess_point_mass_list(pure_abs_devs, tol)

    if pure_mad <= tol or (first_gt2 * 2 >= c_len) or p0_in >= 0.35:
        nz_pure_count = c_len - first_gt2
        if nz_pure_count < min_nz:
            return 0.0
        pure_mad_nz = _median_sorted_range(
            pure_abs_devs, first_gt2, nz_pure_count
        )
        return _truncated_mad_fixed_point(pure_mad_nz, r1)

    return _mixture_mad_fixed_point(pure_mad, r1, p0_in)
