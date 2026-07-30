# Generic strategy for two closed-loop motors on a standard CoreXY drive.

from collections import deque
import logging
import math

try:
    from . import closed_loop_motor_three_z_group as group_base
except ImportError:
    import closed_loop_motor_three_z_group as group_base


AUTOTUNE_SAMPLE_INTERVAL = group_base.AUTOTUNE_SAMPLE_INTERVAL
GROUP_HISTORY_SECONDS = group_base.GROUP_HISTORY_SECONDS
DEFAULT_SAMPLE_INTERVAL = group_base.DEFAULT_SAMPLE_INTERVAL
DEFAULT_SAMPLE_SKEW = group_base.DEFAULT_SAMPLE_SKEW
DEFAULT_SETTLE_TIME = group_base.DEFAULT_SETTLE_TIME
DEFAULT_MIN_IMPROVEMENT = group_base.DEFAULT_MIN_IMPROVEMENT
GroupCandidateRejected = group_base.GroupCandidateRejected


class ClosedLoopCoreXYGroup(group_base.ClosedLoopThreeZGroup):
    group_type = 'corexy'

    def __init__(self, config):
        self.expected_member_count = 2
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        section = config.get_name().split(None, 1)
        self.name = section[1] if len(section) > 1 else 'default'

        raw_members = config.get('members', '')
        self.member_names = [
            value for value in raw_members.replace(',', ' ').split() if value]
        if len(self.member_names) != 2 or len(set(self.member_names)) != 2:
            raise config.error(
                'closed_loop_motor_group: members must name exactly two '
                'unique closed_loop_motor instances for CoreXY')
        self.max_error_deg = config.getfloat(
            'max_error_deg', 6.5, above=0.0)
        # Spread is reported for diagnostics, but it is not a CoreXY safety
        # limit because the two belt motors do not have identical trajectories.
        self.max_spread_deg = None
        self.warning_hold_time = config.getfloat(
            'warning_hold_time', 0.50, minval=0.0)
        self.warning_clear_time = config.getfloat(
            'warning_clear_time', 0.50, minval=0.0)
        self.warning_log_interval = config.getfloat(
            'warning_log_interval', 30.0, above=0.0)
        self.sample_interval = config.getfloat(
            'sample_interval', DEFAULT_SAMPLE_INTERVAL, above=0.0)
        self.sample_skew_tolerance = config.getfloat(
            'sample_skew_tolerance', DEFAULT_SAMPLE_SKEW, above=0.0)
        self.autotune_distance = config.getfloat(
            'autotune_distance', 50.0, above=0.0)
        self.autotune_speed = config.getfloat(
            'autotune_speed', 200.0, above=0.0)
        self.autotune_accel = config.getfloat(
            'autotune_accel', 4000.0, above=0.0)
        self.autotune_iterations = config.getint(
            'autotune_iterations', 12, minval=1, maxval=1000)
        self.autotune_repeats = config.getint(
            'autotune_repeats', 2, minval=1, maxval=5)
        self.autotune_validation_repeats = config.getint(
            'autotune_validation_repeats', 3, minval=1, maxval=5)
        self.autotune_settle_time = config.getfloat(
            'autotune_settle_time', DEFAULT_SETTLE_TIME,
            minval=0.05, maxval=30.0)
        self.autotune_min_improvement = config.getfloat(
            'autotune_min_improvement', DEFAULT_MIN_IMPROVEMENT,
            minval=0.0, maxval=0.50)

        self.members = []
        self.timer = None
        self.history = deque(maxlen=max(
            2, int(math.ceil(GROUP_HISTORY_SECONDS /
                            self.sample_interval)) + 2))
        self.last_sample_times = {}
        self.warning_active = False
        self.warning_reasons = []
        self.warning_since = None
        self.warning_pending_since = None
        self.warning_clear_since = None
        self.warning_signature = None
        self.active_warning_signature = None
        self.last_warning_log_time = 0.0
        self.autotune_active = False
        self.autotune_abort = False
        self.monitor_suspended = False
        self.last = self._empty_status()

        self.printer.register_event_handler(
            'klippy:connect', self._handle_connect)
        self.printer.register_event_handler(
            'klippy:disconnect', self._handle_disconnect)
        self.gcode.register_mux_command(
            'CLOSED_LOOP_GROUP_STATUS', 'NAME', self.name, self.cmd_STATUS,
            desc='Report synchronized state for a CoreXY motor group')
        self.gcode.register_mux_command(
            'CLOSED_LOOP_GROUP_AUTOTUNE', 'NAME', self.name,
            self.cmd_AUTOTUNE,
            desc='Coordinate position-loop PID tuning for two CoreXY motors')
        self.gcode.register_mux_command(
            'CLOSED_LOOP_GROUP_AUTOTUNE_CANCEL', 'NAME', self.name,
            self.cmd_AUTOTUNE_CANCEL,
            desc='Request cancellation of active CoreXY PID tuning')

    def _empty_status(self):
        status = super()._empty_status()
        return status

    def _handle_connect(self):
        try:
            super()._handle_connect()
        except Exception:
            logging.exception(
                'closed_loop_motor_group %s: failed to resolve CoreXY '
                'members', self.name)
            raise

    def _sample_timer(self, eventtime):
        if not self.monitor_suspended:
            try:
                self._collect_sample(eventtime)
            except Exception:
                logging.exception(
                    'closed_loop_motor_group %s: CoreXY sample aggregation '
                    'failed',
                    self.name)
        return eventtime + self.sample_interval

    def _current_reasons(self, snapshots):
        reasons = []
        for snapshot in snapshots:
            if not snapshot['online']:
                reasons.append({
                    'code': 'offline', 'member': snapshot['name']})
            if snapshot.get('stalled'):
                reasons.append({
                    'code': 'stalled', 'member': snapshot['name']})
            if snapshot.get('stall_protect'):
                reasons.append({
                    'code': 'stall_protect', 'member': snapshot['name']})
        errors = [snapshot['error_deg'] for snapshot in snapshots]
        if (len(errors) == self.expected_member_count and
                all(group_base._finite(value) for value in errors)):
            max_abs = max(abs(float(value)) for value in errors)
            if max_abs > self.max_error_deg:
                reasons.append({
                    'code': 'absolute_error', 'value': max_abs,
                    'limit': self.max_error_deg})
        return reasons

    def cmd_STATUS(self, gcmd):
        status = self.get_status(self.reactor.monotonic())
        lines = [
            "Closed-loop CoreXY group '%s' online=%s warning=%s" % (
                self.name, status['online'], status['warning']),
            'max_error=%s deg limit=%s deg' % (
                self._fmt(status.get('max_abs_error_deg')),
                self.max_error_deg),
        ]
        for member in status['members']:
            position_pid = member.get('position_pid') or {}
            endpoint = member.get('endpoint') or {}
            lines.append(
                '%s address=%s online=%s error=%s deg PID=%s/%s/%s' % (
                    member['name'], endpoint.get('address'), member['online'],
                    self._fmt(member.get('error_deg')),
                    position_pid.get('kp'), position_pid.get('ki'),
                    position_pid.get('kd')))
        if status['warning_reasons']:
            lines.append('warnings=%s' % status['warning_reasons'])
        gcmd.respond_info('\n'.join(lines))

    def _coord(self, value, axis, index):
        if value is None:
            return None
        if hasattr(value, axis):
            return float(getattr(value, axis))
        try:
            return float(value[index])
        except Exception:
            return None

    def _centered_test_origin(self, toolhead_status, current, distance):
        minimum = toolhead_status.get('axis_minimum')
        maximum = toolhead_status.get('axis_maximum')
        min_x = self._coord(minimum, 'x', 0)
        min_y = self._coord(minimum, 'y', 1)
        max_x = self._coord(maximum, 'x', 0)
        max_y = self._coord(maximum, 'y', 1)
        if None in (min_x, min_y, max_x, max_y):
            raise RuntimeError(
                'CoreXY autotune requires configured X/Y workspace limits')
        if max_x - min_x < distance or max_y - min_y < distance:
            raise RuntimeError(
                'CoreXY autotune square does not fit configured X/Y workspace')
        origin = list(current)
        origin[0] = (min_x + max_x - distance) * 0.5
        origin[1] = (min_y + max_y - distance) * 0.5
        return origin

    def _check_autotune_preconditions(self, gcmd, distance):
        if len(self.members) != self.expected_member_count:
            raise gcmd.error('Closed-loop CoreXY group is not connected')
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats is not None:
            state = str(print_stats.get_status(
                self.reactor.monotonic()).get('state', '')).lower()
            if state not in ('standby', 'complete', 'cancelled', 'error'):
                raise gcmd.error(
                    'CoreXY autotune requires an idle printer (state=%s)' %
                    state)
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            raise gcmd.error('CoreXY autotune requires the Klipper toolhead')
        toolhead.wait_moves()
        toolhead_status = toolhead.get_status(self.reactor.monotonic())
        homed = str(toolhead_status.get('homed_axes', '')).lower()
        if 'x' not in homed or 'y' not in homed:
            raise gcmd.error('X and Y must both be homed for CoreXY autotune')
        start = list(toolhead.get_position())
        try:
            origin = self._centered_test_origin(
                toolhead_status, start, distance)
        except RuntimeError as exc:
            raise gcmd.error(str(exc))

        originals = []
        for member in self.members:
            member_name = member.closed_loop_name()
            if member.closed_loop_autotune_active():
                raise gcmd.error(
                    "closed_loop_motor '%s' is already tuning" %
                    member_name)
            try:
                member.validate_group_autotune_configuration()
            except ValueError as exc:
                raise gcmd.error(
                    "closed_loop_motor '%s': %s" %
                    (member_name, str(exc)))
            if not member.refresh_position_error_status():
                raise gcmd.error(
                    "closed_loop_motor '%s' communication preflight "
                    "failed: %s" %
                    (member_name, member.closed_loop_last_error()))
            status = member.closed_loop_status(
                self.reactor.monotonic())
            if not status.get('online'):
                raise gcmd.error(
                    "closed_loop_motor '%s' is offline" % member_name)
            if status.get('stalled') or status.get('stall_protect'):
                raise gcmd.error(
                    "closed_loop_motor '%s' reports a stall" %
                    member_name)
            pid = member.read_position_pid()
            if pid is None or not member.write_position_pid(
                    pid, store=0, verify=True):
                raise gcmd.error(
                    "closed_loop_motor '%s' PID read/write preflight "
                    "failed: %s" %
                    (member_name, member.closed_loop_last_error()))
            originals.append(tuple(pid))
        return toolhead, start, origin, originals

    def _move_to(self, toolhead, target, speed, accel):
        saved_limits = self._set_motion_limits(toolhead, speed, accel)
        try:
            toolhead.manual_move(target, speed)
            toolhead.wait_moves()
        finally:
            toolhead.set_max_velocities(*saved_limits)

    def _check_group_samples(self, samples):
        if not samples:
            raise GroupCandidateRejected(
                'too few synchronized position-error samples')
        max_abs = max(
            abs(value) for sample in samples for value in sample['errors'])
        if max_abs > self.max_error_deg:
            raise GroupCandidateRejected(
                'position error %.6f deg exceeded %.6f deg' %
                (max_abs, self.max_error_deg))

    def _score_group_samples(self, samples):
        if not samples:
            return None
        member_metrics = []
        for index in range(self.expected_member_count):
            motion = [sample['errors'][index] for sample in samples
                      if str(sample['phase']).startswith('motion:')]
            settle = [sample['errors'][index] for sample in samples
                      if sample['phase'] == 'settle']
            metrics = self._series_score(motion, settle)
            if metrics is None:
                return None
            member_metrics.append(metrics)
        member_scores = [value['score'] for value in member_metrics]
        mean_score = sum(member_scores) / float(len(member_scores))
        worst_score = max(member_scores)
        return {
            'score': 0.70 * mean_score + 0.30 * worst_score,
            'mean_score': mean_score,
            'worst_score': worst_score,
            'member_metrics': member_metrics,
            'samples': len(samples),
        }

    def _run_profile(self, toolhead, origin, distance, speed, accel,
                     settle_time, validation, profile):
        if self.autotune_abort:
            raise RuntimeError('CoreXY autotune cancellation requested')
        saved_limits = None
        returned = False
        captures = []
        for member in self.members:
            member.begin_position_capture(self.max_error_deg)
            member.set_position_capture_phase('motion:' + profile)
        try:
            saved_limits = self._set_motion_limits(toolhead, speed, accel)
            route = self.members[0].corexy_test_route(
                origin, distance, validation=validation, profile=profile)
            for _, target in route:
                toolhead.manual_move(target, speed)
            toolhead.wait_moves()
            returned = True
            for member in self.members:
                member.set_position_capture_phase('settle')
            self.reactor.pause(self.reactor.monotonic() + settle_time)
        finally:
            if not returned:
                try:
                    toolhead.manual_move(origin, speed)
                    toolhead.wait_moves()
                except Exception:
                    logging.exception(
                        'closed_loop_motor_group %s: failed to return after '
                        'CoreXY profile',
                        self.name)
            for member in self.members:
                captures.append(member.end_position_capture())
            if saved_limits is not None:
                toolhead.set_max_velocities(*saved_limits)
        if self.autotune_abort:
            raise RuntimeError('CoreXY autotune cancellation requested')
        violations = []
        for member in self.members:
            member_name = member.closed_loop_name()
            violation = member.consume_position_capture_violation()
            if violation:
                violations.append('%s: %s' % (member_name, violation))
        if violations:
            raise GroupCandidateRejected('; '.join(violations))
        for member in self.members:
            member_name = member.closed_loop_name()
            status = member.closed_loop_status(
                self.reactor.monotonic())
            if not status.get('online'):
                raise RuntimeError(
                    "closed_loop_motor '%s' went offline during tuning" %
                    member_name)
            motor_state = member.refresh_motor_state()
            if motor_state is None:
                raise RuntimeError(
                    "failed to read motor state for '%s'" % member_name)
            if (motor_state.get('stalled') or
                    motor_state.get('stall_protect')):
                raise RuntimeError(
                    "closed_loop_motor '%s' reported a stall" %
                    member_name)
        samples = self._align_capture_samples(captures)
        self._check_group_samples(samples)
        metrics = self._score_group_samples(samples)
        if metrics is None:
            raise GroupCandidateRejected(
                'too few synchronized motion and settle samples')
        return metrics

    def _aggregate_repeat_metrics(self, metrics):
        if not metrics:
            raise ValueError('repeat metrics must not be empty')
        return {
            'score': group_base._median([
                value['score'] for value in metrics]),
            'mean_score': group_base._median([
                value['mean_score'] for value in metrics]),
            'worst_score': group_base._median([
                value['worst_score'] for value in metrics]),
            'samples': sum(value['samples'] for value in metrics),
        }

    def _evaluate_profiles(self, gcmd, phase, toolhead, origin, distance,
                           speed, accel, settle_time, repeats, factors,
                           validation=False):
        scenarios = []
        profiles = ('long', 'corner', 'curve')
        for factor in factors:
            for profile in profiles:
                results = []
                for repeat in range(repeats):
                    results.append(self._run_profile(
                        toolhead, origin, distance,
                        max(0.001, speed * factor),
                        max(0.001, accel * factor), settle_time,
                        validation, profile))
                metrics = self._aggregate_repeat_metrics(results)
                scenarios.append(metrics)
                gcmd.respond_info(
                    'CoreXY %s %d%% %s repeats=%d: %s' % (
                        phase, int(round(factor * 100)), profile, repeats,
                        self._format_metrics(metrics)))
        return {
            'score': sum(value['score'] for value in scenarios) /
                     float(len(scenarios)),
            'mean_score': sum(value['mean_score'] for value in scenarios) /
                          float(len(scenarios)),
            'worst_score': sum(value['worst_score'] for value in scenarios) /
                           float(len(scenarios)),
            'samples': sum(value['samples'] for value in scenarios),
        }

    def _pid_bounds(self, gcmd, originals, steps):
        all_bounds = []
        names = ('KP', 'KI', 'KD')
        for member_index, (member, original) in enumerate(zip(
                self.members, originals)):
            pid_min, pid_max = member.position_pid_bounds()
            defaults = [
                (max(pid_min, int(original[0] * 0.5)),
                 min(pid_max,
                     max(1, int(original[0] * 2.0)))),
                (pid_min,
                 min(pid_max,
                     max(int(original[1] * 10),
                         original[1] + 10 * steps[member_index][1]))),
                (max(pid_min, int(original[2] * 0.5)),
                 min(pid_max,
                     max(1, int(original[2] * 2.0)))),
            ]
            bounds = []
            prefix = 'M%d_' % (member_index + 1)
            for parameter, name in enumerate(names):
                lower = gcmd.get_int(
                    prefix + name + '_MIN', defaults[parameter][0],
                    minval=0, maxval=0xFFFFFFFF)
                upper = gcmd.get_int(
                    prefix + name + '_MAX', defaults[parameter][1],
                    minval=0, maxval=0xFFFFFFFF)
                if (lower > upper or
                        not lower <= original[parameter] <= upper):
                    raise gcmd.error(
                        '%s%s_MIN/%s%s_MAX must contain the original PID '
                        'value' % (prefix, name, prefix, name))
                bounds.append((lower, upper))
            all_bounds.append(bounds)
        return all_bounds

    def _candidate(self, current, member_index, parameter, delta, bounds):
        candidate = [list(value) for value in current]
        lower, upper = bounds[member_index][parameter]
        value = max(
            lower, min(upper,
                       candidate[member_index][parameter] + delta))
        if value == candidate[member_index][parameter]:
            return None
        candidate[member_index][parameter] = value
        return [tuple(value) for value in candidate]

    def _search_target(self, iteration):
        slot = iteration % 6
        return slot // 3, slot % 3

    def _format_metrics(self, metrics):
        return ('score=%.6f mean=%.6f worst=%.6f samples=%d' % (
            metrics['score'], metrics['mean_score'],
            metrics['worst_score'], metrics['samples']))

    def cmd_AUTOTUNE(self, gcmd):
        if self.autotune_active:
            raise gcmd.error(
                'CLOSED_LOOP_GROUP_AUTOTUNE is already running')
        if gcmd.get_int('CONFIRM', 0, minval=0, maxval=1) != 1:
            raise gcmd.error(
                'CoreXY autotune moves X and Y and writes two drivers; '
                'pass CONFIRM=1')
        distance = gcmd.get_float(
            'DISTANCE', self.autotune_distance, minval=0.1, maxval=100.0)
        speed = gcmd.get_float(
            'SPEED', self.autotune_speed, minval=0.1, maxval=1000.0)
        accel = gcmd.get_float(
            'ACCEL', self.autotune_accel, minval=1.0, maxval=100000.0)
        iterations = gcmd.get_int(
            'ITERATIONS', self.autotune_iterations, minval=1, maxval=1000)
        repeats = gcmd.get_int(
            'REPEATS', self.autotune_repeats, minval=1, maxval=5)
        validation_repeats = gcmd.get_int(
            'VALIDATION_REPEATS', self.autotune_validation_repeats,
            minval=1, maxval=5)
        max_error_deg = gcmd.get_float(
            'MAX_ERROR_DEG', self.max_error_deg,
            minval=0.001, maxval=360.0)
        settle_time = gcmd.get_float(
            'SETTLE', self.autotune_settle_time,
            minval=0.05, maxval=30.0)
        min_improvement = gcmd.get_float(
            'MIN_IMPROVEMENT', self.autotune_min_improvement,
            minval=0.0, maxval=0.50)

        toolhead = None
        start = None
        origin = None
        originals = None
        current = None
        saved_runtime = None
        moved_to_origin = False
        persist_attempted = False
        configured_max_error_deg = self.max_error_deg
        self.max_error_deg = max_error_deg
        self.autotune_active = True
        self.autotune_abort = False
        try:
            toolhead, start, origin, originals = (
                self._check_autotune_preconditions(gcmd, distance))
            current = [tuple(value) for value in originals]
            saved_runtime = self._prepare_member_capture()
            positioning_speed = min(100.0, max(1.0, speed * 0.5))
            positioning_accel = min(1000.0, max(1.0, accel * 0.5))
            moved_to_origin = True
            self._move_to(
                toolhead, origin, positioning_speed, positioning_accel)

            baseline = self._evaluate_profiles(
                gcmd, 'baseline', toolhead, origin, distance, speed, accel,
                settle_time, repeats, (0.50, 1.00))
            best = baseline
            gcmd.respond_info(
                'Closed-loop CoreXY baseline: %s; %s' % (
                    self._format_pid_set(current),
                    self._format_metrics(best)))

            steps = [list(member.position_pid_steps())
                     for member in self.members]
            bounds = self._pid_bounds(gcmd, originals, steps)
            parameter_names = ('Kp', 'Ki', 'Kd')
            cycle_improved = False
            no_improvement_cycles = 0
            for iteration in range(iterations):
                if self.autotune_abort:
                    raise gcmd.error(
                        'CoreXY autotune cancellation requested')
                member_index, parameter = self._search_target(iteration)
                member = self.members[member_index]
                member_name = member.closed_loop_name()
                evaluated = []
                for direction in (1, -1):
                    candidate = self._candidate(
                        current, member_index, parameter,
                        direction * steps[member_index][parameter], bounds)
                    if candidate is None:
                        continue
                    if not member.write_position_pid(
                            candidate[member_index], store=0, verify=True):
                        raise gcmd.error(
                            "temporary PID write failed for '%s': %s" %
                            (member_name,
                             member.closed_loop_last_error()))
                    try:
                        metrics = self._evaluate_profiles(
                            gcmd, 'iteration %d %s %s' % (
                                iteration + 1, member_name,
                                parameter_names[parameter]),
                            toolhead, origin, distance, speed, accel,
                            settle_time, repeats, (0.50, 1.00))
                    except GroupCandidateRejected as exc:
                        gcmd.respond_info(
                            'CoreXY iteration %d %s %s candidate rejected: '
                            '%s' % (iteration + 1, member_name,
                                   parameter_names[parameter], exc))
                        continue
                    evaluated.append((metrics['score'], candidate, metrics))

                accepted = False
                if evaluated:
                    _, candidate, metrics = min(
                        evaluated, key=lambda value: value[0])
                    threshold = best['score'] * (1.0 - min_improvement)
                    if metrics['score'] < threshold:
                        current = candidate
                        best = metrics
                        accepted = True
                        cycle_improved = True
                ok, failed_member = self._write_pid_set(
                    current, store=0, verify=True)
                if not ok:
                    raise gcmd.error(
                        "failed to restore selected PID for '%s'" %
                        failed_member.closed_loop_name())
                gcmd.respond_info(
                    'CoreXY iteration %d %s %s: %s; selected %s' % (
                        iteration + 1, member_name,
                        parameter_names[parameter],
                        'accepted' if accepted else 'rejected',
                        self._format_pid_set(current)))

                if (iteration + 1) % 6 == 0:
                    steps = [[max(1, value // 2) for value in member_steps]
                             for member_steps in steps]
                    if cycle_improved:
                        no_improvement_cycles = 0
                    else:
                        no_improvement_cycles += 1
                    cycle_improved = False
                    if no_improvement_cycles >= 2:
                        gcmd.respond_info(
                            'CoreXY autotune converged: two full PID cycles '
                            'without significant improvement')
                        break

            ok, failed_member = self._write_pid_set(
                originals, store=0, verify=True)
            if not ok:
                raise gcmd.error(
                    "failed to prepare original validation for '%s'" %
                    failed_member.closed_loop_name())
            original_validation = self._evaluate_profiles(
                gcmd, 'original validation', toolhead, origin, distance,
                speed, accel, settle_time, validation_repeats, (0.70,),
                validation=True)
            ok, failed_member = self._write_pid_set(
                current, store=0, verify=True)
            if not ok:
                raise gcmd.error(
                    "failed to prepare tuned validation for '%s'" %
                    failed_member.closed_loop_name())
            tuned_validation = self._evaluate_profiles(
                gcmd, 'tuned validation', toolhead, origin, distance,
                speed, accel, settle_time, validation_repeats, (0.70,),
                validation=True)
            validation_limit = original_validation['score'] * (
                1.0 - min_improvement)
            if (current == originals or
                    tuned_validation['score'] >= validation_limit):
                failures = self._restore_pid_set(originals, store=0)
                if failures:
                    raise gcmd.error(
                        'no validated improvement and original PID restore '
                        'failed for %s' % ', '.join(failures))
                gcmd.respond_info(
                    'CoreXY autotune found no validated improvement; '
                    'original PIDs restored. original %s tuned %s' % (
                        self._format_metrics(original_validation),
                        self._format_metrics(tuned_validation)))
                return

            persist_attempted = True
            ok, failed_member = self._write_pid_set(
                current, store=1, verify=True)
            if not ok:
                failures = self._restore_pid_set(originals, store=1)
                message = "failed to persist PID for '%s'" % (
                    failed_member.closed_loop_name())
                if failures:
                    message += '; original restore failed for ' + ', '.join(
                        failures)
                raise gcmd.error(message)
            readback = [member.read_position_pid()
                        for member in self.members]
            if readback != current:
                failures = self._restore_pid_set(originals, store=1)
                message = 'final CoreXY PID readback mismatch'
                if failures:
                    message += '; original restore failed for ' + ', '.join(
                        failures)
                raise gcmd.error(message)
            gcmd.respond_info(
                'Closed-loop CoreXY autotune complete: %s; training %s; '
                'validation %s; stored in both drivers' % (
                    self._format_pid_set(current),
                    self._format_metrics(best),
                    self._format_metrics(tuned_validation)))
        except GroupCandidateRejected as exc:
            if originals is not None:
                self._restore_pid_set(
                    originals, store=1 if persist_attempted else 0)
            raise gcmd.error('CoreXY autotune safety rejection: %s' % exc)
        except self.printer.command_error:
            if originals is not None:
                self._restore_pid_set(
                    originals, store=1 if persist_attempted else 0)
            raise
        except Exception as exc:
            if originals is not None:
                self._restore_pid_set(
                    originals, store=1 if persist_attempted else 0)
            logging.exception(
                'closed_loop_motor_group %s: unexpected CoreXY autotune '
                'failure',
                self.name)
            raise gcmd.error('CoreXY autotune failed safely: %s' % exc)
        finally:
            if moved_to_origin and toolhead is not None and start is not None:
                try:
                    self._move_to(
                        toolhead, start,
                        min(100.0, max(1.0, speed * 0.5)),
                        min(1000.0, max(1.0, accel * 0.5)))
                except Exception:
                    logging.exception(
                        'closed_loop_motor_group %s: failed to return CoreXY '
                        'to start',
                        self.name)
            if saved_runtime is not None:
                self._restore_member_capture(saved_runtime)
            self.max_error_deg = configured_max_error_deg
            self.autotune_abort = False
            self.autotune_active = False

    def cmd_AUTOTUNE_CANCEL(self, gcmd):
        if not self.autotune_active:
            gcmd.respond_info('Closed-loop CoreXY autotune is not running')
            return
        self.autotune_abort = True
        for member in self.members:
            member.request_autotune_cancel()
        gcmd.respond_info(
            'CoreXY autotune cancellation requested; the active profile '
            'will finish before both original PIDs are restored')
