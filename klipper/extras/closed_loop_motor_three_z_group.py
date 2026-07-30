# Generic strategy for three closed-loop motors on an independent Z axis.

from collections import deque
import logging
import math

try:
    from . import closed_loop_motor_core as closed_loop_core
except ImportError:
    import closed_loop_motor_core as closed_loop_core


GROUP_HISTORY_SECONDS = 10.0
AUTOTUNE_SAMPLE_INTERVAL = 0.02
DEFAULT_SAMPLE_INTERVAL = 0.025
DEFAULT_SAMPLE_SKEW = 0.04
DEFAULT_SETTLE_TIME = 0.50
DEFAULT_MIN_IMPROVEMENT = 0.02
DEFAULT_AUTOTUNE_REPEATS = 3
DEFAULT_VALIDATION_REPEATS = 5


class GroupCandidateRejected(Exception):
    """A PID candidate exceeded a recoverable group safety limit."""


def _finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _percentile_abs(values, percentile):
    ordered = sorted(abs(float(value)) for value in values)
    if not ordered:
        raise ValueError('percentile requires at least one value')
    index = int(math.ceil(percentile * len(ordered))) - 1
    return ordered[max(0, min(len(ordered) - 1, index))]


def _rms(values):
    return math.sqrt(
        sum(float(value) * float(value) for value in values) /
        float(len(values)))


def _median(values):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError('median requires at least one value')
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


class ClosedLoopThreeZGroup:
    group_type = 'three_z'

    def __init__(self, config):
        self.expected_member_count = 3
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        section = config.get_name().split(None, 1)
        self.name = section[1] if len(section) > 1 else 'default'

        raw_members = config.get('members', '')
        self.member_names = [
            value for value in raw_members.replace(',', ' ').split() if value]
        if len(self.member_names) != 3 or len(set(self.member_names)) != 3:
            raise config.error(
                'closed_loop_motor_group: members must name exactly three '
                'unique closed_loop_motor instances')
        self.max_error_deg = config.getfloat(
            'max_error_deg', 5.0, above=0.0)
        self.max_spread_deg = config.getfloat(
            'max_spread_deg', 2.0, above=0.0)
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
            'autotune_distance', 10.0, above=0.0)
        self.autotune_speed = config.getfloat(
            'autotune_speed', 10.0, above=0.0)
        self.autotune_accel = config.getfloat(
            'autotune_accel', 200.0, above=0.0)
        self.autotune_iterations = config.getint(
            'autotune_iterations', 27, minval=1, maxval=1000)
        self.autotune_repeats = config.getint(
            'autotune_repeats', DEFAULT_AUTOTUNE_REPEATS,
            minval=1, maxval=100)
        self.autotune_validation_repeats = config.getint(
            'autotune_validation_repeats', DEFAULT_VALIDATION_REPEATS,
            minval=1, maxval=100)
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
            desc='Report synchronized state for a closed-loop motor group')
        self.gcode.register_mux_command(
            'CLOSED_LOOP_GROUP_AUTOTUNE', 'NAME', self.name,
            self.cmd_AUTOTUNE,
            desc='Coordinate position-loop PID tuning for a motor group')
        self.gcode.register_mux_command(
            'CLOSED_LOOP_GROUP_AUTOTUNE_CANCEL', 'NAME', self.name,
            self.cmd_AUTOTUNE_CANCEL,
            desc='Request cancellation of active group PID tuning')

    def _empty_status(self):
        return {
            'schema_version': closed_loop_core.SCHEMA_VERSION,
            'object_type': closed_loop_core.GROUP_OBJECT_TYPE,
            'name': self.name,
            'group_type': self.group_type,
            'connection_state': 'unknown',
            'capabilities': [
                'position_error', 'group_monitor', 'autotune_group'],
            'online': False,
            'members': [],
            'history': [],
            'max_abs_error_deg': None,
            'max_abs_error_mm': None,
            'spread_deg': None,
            'spread_mm': None,
            'max_error_deg': self.max_error_deg,
            'max_spread_deg': self.max_spread_deg,
            'warning': False,
            'warning_reasons': [],
            'warning_since': None,
            'autotune_active': False,
        }

    def _member_object_name(self, name):
        if name.lower().startswith('closed_loop_motor '):
            return name
        return closed_loop_core.motor_object_name(name)

    def _handle_connect(self):
        self.members = []
        for name in self.member_names:
            object_name = self._member_object_name(name)
            try:
                member = self.printer.lookup_object(object_name)
            except Exception:
                raise self.printer.config_error(
                    "closed_loop_motor_group %s: member '%s' was not found" %
                    (self.name, object_name))
            try:
                closed_loop_core.validate_group_member(
                    member, self.group_type)
            except ValueError as exc:
                raise self.printer.config_error(
                    "closed_loop_motor_group %s: member '%s' %s" %
                    (self.name, object_name, exc))
            self.members.append(member)
        identities = [member.closed_loop_identity()
                      for member in self.members]
        if len(set(identities)) != 1:
            raise self.printer.config_error(
                'closed_loop_motor_group %s: members must use the same '
                'vendor, model, and transport' % self.name)
        addresses = [member.transport_address()
                     for member in self.members]
        if len(set(addresses)) != len(addresses):
            raise self.printer.config_error(
                'closed_loop_motor_group %s: member transport addresses '
                'must be unique' % self.name)
        if self.timer is None:
            self.timer = self.reactor.register_timer(self._sample_timer)
        self.reactor.update_timer(self.timer, self.reactor.NOW)

    def _handle_disconnect(self):
        if self.timer is not None:
            self.reactor.update_timer(self.timer, self.reactor.NEVER)
        self.members = []
        self.history.clear()
        self.last = self._empty_status()

    def _sample_timer(self, eventtime):
        if not self.monitor_suspended:
            try:
                self._collect_sample(eventtime)
            except Exception:
                logging.exception(
                    'closed_loop_motor_group %s: sample aggregation failed',
                    self.name)
        return eventtime + self.sample_interval

    def _member_snapshot(self, member, status):
        vendor, model, transport = member.closed_loop_identity()
        _, interface, address = member.transport_address()
        endpoint = dict(status.get('endpoint') or {})
        position_pid = dict(status.get('position_pid') or {})
        topology = endpoint.get('topology')
        if topology is None:
            topology = ('multidrop' if transport in ('can', 'rs485')
                        else 'point_to_point')
        return {
            'name': member.closed_loop_name(),
            'object': member.closed_loop_object_name(),
            'identity': {'vendor': vendor, 'model': model},
            'endpoint': {
                'transport': endpoint.get('transport', transport),
                'interface': endpoint.get('interface', interface),
                'address': endpoint.get('address', address),
                'topology': topology,
            },
            'connection_state': status.get(
                'connection_state',
                'online' if status.get('online') is True else 'offline'),
            'online': (status.get('connection_state') == 'online' or
                       status.get('online') is True),
            'error_deg': status.get('error_deg'),
            'error_mm': status.get('error_mm'),
            'position_pid': {
                'revision': int(position_pid.get('revision') or 0),
                'kp': position_pid.get('kp'),
                'ki': position_pid.get('ki'),
                'kd': position_pid.get('kd'),
                'error': str(position_pid.get('error') or ''),
            },
            'stalled': status.get('stalled'),
            'stall_protect': status.get('stall_protect'),
        }

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
        if (len(errors) == getattr(self, 'expected_member_count', 3) and
                all(_finite(value) for value in errors)):
            max_abs = max(abs(float(value)) for value in errors)
            spread = max(float(value) for value in errors) - min(
                float(value) for value in errors)
            if max_abs > self.max_error_deg:
                reasons.append({
                    'code': 'absolute_error', 'value': max_abs,
                    'limit': self.max_error_deg})
            if spread > self.max_spread_deg:
                reasons.append({
                    'code': 'spread', 'value': spread,
                    'limit': self.max_spread_deg})
        return reasons

    def _reason_signature(self, reasons):
        return tuple(sorted(
            (reason.get('code', ''), reason.get('member', ''))
            for reason in reasons))

    def _update_warning(self, reasons, eventtime):
        signature = self._reason_signature(reasons)
        if reasons:
            self.warning_clear_since = None
            if signature != self.warning_signature:
                self.warning_signature = signature
                self.warning_pending_since = eventtime
            if self.warning_pending_since is None:
                self.warning_pending_since = eventtime
            if (not self.warning_active and
                    eventtime - self.warning_pending_since >=
                    self.warning_hold_time):
                self.warning_active = True
                self.warning_since = self.warning_pending_since
            if self.warning_active:
                changed = signature != self.active_warning_signature
                self.warning_reasons = [dict(reason) for reason in reasons]
                if (changed or eventtime - self.last_warning_log_time >=
                        self.warning_log_interval):
                    logging.warning(
                        'closed_loop_motor_group %s warning: %s', self.name,
                        self.warning_reasons)
                    self.last_warning_log_time = eventtime
                self.active_warning_signature = signature
            return

        self.warning_pending_since = None
        self.warning_signature = None
        if not self.warning_active:
            self.warning_reasons = []
            return
        if self.warning_clear_since is None:
            self.warning_clear_since = eventtime
        if eventtime - self.warning_clear_since >= self.warning_clear_time:
            self.warning_active = False
            self.warning_reasons = []
            self.warning_since = None
            self.warning_clear_since = None
            self.active_warning_signature = None

    def _latest_aligned_samples(self):
        samples = []
        for member in self.members:
            sample = closed_loop_core.normalize_position_error_sample(
                member.latest_position_error_sample())
            if sample is None:
                return None
            samples.append(sample)
        times = [float(sample['time']) for sample in samples]
        if max(times) - min(times) > self.sample_skew_tolerance:
            return None
        if any(self.last_sample_times.get(
                member.closed_loop_name()) == sample['time']
               for member, sample in zip(self.members, samples)):
            return None
        return samples

    def _collect_sample(self, eventtime):
        if len(self.members) != getattr(self, 'expected_member_count', 3):
            return
        statuses = [member.closed_loop_status(eventtime)
                    for member in self.members]
        snapshots = [
            self._member_snapshot(member, status)
            for member, status in zip(self.members, statuses)]
        reasons = self._current_reasons(snapshots)
        self._update_warning(reasons, eventtime)
        self.last['members'] = snapshots
        self.last['online'] = all(
            snapshot['online'] for snapshot in snapshots)

        errors_deg = [snapshot['error_deg'] for snapshot in snapshots]
        errors_mm = [snapshot['error_mm'] for snapshot in snapshots]
        if all(_finite(value) for value in errors_deg):
            self.last['max_abs_error_deg'] = max(
                abs(float(value)) for value in errors_deg)
            self.last['spread_deg'] = (
                max(float(value) for value in errors_deg) -
                min(float(value) for value in errors_deg))
        else:
            self.last['max_abs_error_deg'] = None
            self.last['spread_deg'] = None
        if all(_finite(value) for value in errors_mm):
            self.last['max_abs_error_mm'] = max(
                abs(float(value)) for value in errors_mm)
            self.last['spread_mm'] = (
                max(float(value) for value in errors_mm) -
                min(float(value) for value in errors_mm))
        else:
            self.last['max_abs_error_mm'] = None
            self.last['spread_mm'] = None

        aligned = self._latest_aligned_samples()
        if aligned is not None and self.last['online']:
            aligned_deg = [float(value['error_deg']) for value in aligned]
            aligned_mm = [value.get('error_mm') for value in aligned]
            has_mm = all(_finite(value) for value in aligned_mm)
            sample = {
                'time': max(float(value['time']) for value in aligned),
                'members': [{
                    'name': snapshot['name'],
                    'address': snapshot['endpoint']['address'],
                    'error_deg': value.get('error_deg'),
                    'error_mm': value.get('error_mm'),
                } for snapshot, value in zip(snapshots, aligned)],
                'max_abs_error_deg': max(abs(value)
                                         for value in aligned_deg),
                'max_abs_error_mm': (
                    max(abs(float(value)) for value in aligned_mm)
                    if has_mm else None),
                'spread_deg': max(aligned_deg) - min(aligned_deg),
                'spread_mm': (
                    max(float(value) for value in aligned_mm) -
                    min(float(value) for value in aligned_mm)
                    if has_mm else None),
            }
            self.history.append(sample)
            for member, value in zip(self.members, aligned):
                self.last_sample_times[
                    member.closed_loop_name()] = value['time']
        self._prune_history(eventtime)
        self.last['warning'] = self.warning_active
        self.last['warning_reasons'] = [
            dict(reason) for reason in self.warning_reasons]
        self.last['warning_since'] = self.warning_since
        self.last['autotune_active'] = self.autotune_active
        self.last['connection_state'] = (
            'online' if self.last['online'] else 'offline')

    def _prune_history(self, eventtime):
        cutoff = eventtime - GROUP_HISTORY_SECONDS
        while self.history and self.history[0]['time'] < cutoff:
            self.history.popleft()
        self.last['history'] = [dict(sample) for sample in self.history]

    def get_status(self, eventtime):
        if self.members and not self.monitor_suspended:
            self._collect_sample(eventtime)
        status = dict(self.last)
        status['members'] = [dict(value) for value in self.last['members']]
        status['history'] = [dict(value) for value in self.history]
        status['warning_reasons'] = [
            dict(value) for value in self.warning_reasons]
        status['warning'] = self.warning_active
        status['warning_since'] = self.warning_since
        status['autotune_active'] = self.autotune_active
        return status

    def cmd_STATUS(self, gcmd):
        status = self.get_status(self.reactor.monotonic())
        lines = [
            "Closed-loop motor group '%s' online=%s warning=%s" % (
                self.name, status['online'], status['warning']),
            'max_error=%s deg spread=%s deg limits=%s/%s deg' % (
                self._fmt(status.get('max_abs_error_deg')),
                self._fmt(status.get('spread_deg')),
                self.max_error_deg, self.max_spread_deg),
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

    def _check_autotune_preconditions(self, gcmd, distance):
        if len(self.members) != 3:
            raise gcmd.error('Closed-loop motor group is not connected')
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats is not None:
            state = str(print_stats.get_status(
                self.reactor.monotonic()).get('state', '')).lower()
            if state not in ('standby', 'complete', 'cancelled', 'error'):
                raise gcmd.error(
                    '3Z autotune requires an idle printer (state=%s)' % state)
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            raise gcmd.error('3Z autotune requires the Klipper toolhead')
        toolhead.wait_moves()
        toolhead_status = toolhead.get_status(self.reactor.monotonic())
        if 'z' not in str(toolhead_status.get('homed_axes', '')).lower():
            raise gcmd.error('Z must be homed before 3Z autotune')
        z_tilt = self.printer.lookup_object('z_tilt', None)
        if z_tilt is None or not z_tilt.get_status(
                self.reactor.monotonic()).get('applied', False):
            raise gcmd.error(
                'Z_TILT_ADJUST must be applied before 3Z autotune')
        origin = list(toolhead.get_position())
        maximum = toolhead_status.get('axis_maximum')
        max_z = getattr(maximum, 'z', None)
        if max_z is None:
            try:
                max_z = maximum[2]
            except Exception:
                max_z = None
        if max_z is not None and origin[2] + distance > float(max_z):
            raise gcmd.error(
                '3Z autotune path exceeds configured Z workspace')

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
        return toolhead, origin, originals

    def _prepare_member_capture(self):
        saved = []
        self.monitor_suspended = True
        try:
            for member in self.members:
                saved.append(member.prepare_group_capture(
                    AUTOTUNE_SAMPLE_INTERVAL, GROUP_HISTORY_SECONDS))
        except Exception:
            try:
                self._restore_member_capture(saved)
            except Exception:
                logging.exception(
                    'closed_loop_motor_group %s: capture preparation '
                    'rollback failed', self.name)
            raise
        return saved

    def _restore_member_capture(self, saved):
        first_error = None
        try:
            for member, state in zip(self.members, saved):
                try:
                    member.restore_group_capture(state)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    logging.exception(
                        "closed_loop_motor_group %s: failed to restore "
                        "member '%s' after capture", self.name,
                        member.closed_loop_name())
        finally:
            self.monitor_suspended = False
        if first_error is not None:
            raise first_error

    def _set_motion_limits(self, toolhead, speed, accel):
        status = toolhead.get_status(self.reactor.monotonic())
        saved = (
            float(status['max_velocity']), float(status['max_accel']),
            float(status['square_corner_velocity']),
            float(status['minimum_cruise_ratio']))
        if not hasattr(toolhead, 'set_max_velocities'):
            raise RuntimeError(
                'Klipper toolhead does not provide set_max_velocities()')
        toolhead.set_max_velocities(
            min(saved[0], speed), min(saved[1], accel),
            saved[2], saved[3])
        return saved

    def _align_capture_samples(self, captures):
        if not captures or any(not isinstance(values, (list, tuple))
                               for values in captures):
            return []
        captures = [[
            normalized
            for value in values
            for normalized in (
                closed_loop_core.normalize_position_error_sample(value),)
            if normalized is not None
        ] for values in captures]
        if any(not values for values in captures):
            return []
        aligned = []
        indices = [0 for _ in captures]
        for reference in captures[0]:
            row = [reference]
            valid = True
            for member_index in range(1, len(captures)):
                values = captures[member_index]
                while (indices[member_index] + 1 < len(values) and
                       abs(values[indices[member_index] + 1]['time'] -
                           reference['time']) <=
                       abs(values[indices[member_index]]['time'] -
                           reference['time'])):
                    indices[member_index] += 1
                candidate = values[indices[member_index]]
                if abs(candidate['time'] - reference['time']) > max(
                        self.sample_skew_tolerance,
                        AUTOTUNE_SAMPLE_INTERVAL * 1.5):
                    valid = False
                    break
                row.append(candidate)
            if valid:
                aligned.append({
                    'time': max(float(value['time']) for value in row),
                    'phase': reference.get('phase', 'unknown'),
                    'errors': [float(value['error_deg']) for value in row],
                })
        return aligned

    def _series_score(self, motion, settle):
        if len(motion) < 3 or not settle:
            return None
        motion_rms = _rms(motion)
        motion_p95 = _percentile_abs(motion, 0.95)
        motion_peak = max(abs(float(value)) for value in motion)
        settle_rms = _rms(settle)
        return {
            'score': (0.50 * motion_rms + 0.30 * motion_p95 +
                      0.15 * motion_peak + 0.05 * settle_rms),
            'rms': motion_rms,
            'p95': motion_p95,
            'peak': motion_peak,
            'settle_rms': settle_rms,
        }

    def _score_group_samples(self, samples):
        if not samples:
            return None
        member_metrics = []
        for index in range(3):
            motion = [sample['errors'][index] for sample in samples
                      if str(sample['phase']).startswith('motion:')]
            settle = [sample['errors'][index] for sample in samples
                      if sample['phase'] == 'settle']
            metrics = self._series_score(motion, settle)
            if metrics is None:
                return None
            member_metrics.append(metrics)
        spread_motion = [
            max(sample['errors']) - min(sample['errors'])
            for sample in samples
            if str(sample['phase']).startswith('motion:')]
        spread_settle = [
            max(sample['errors']) - min(sample['errors'])
            for sample in samples if sample['phase'] == 'settle']
        spread_metrics = self._series_score(
            spread_motion, spread_settle)
        if spread_metrics is None:
            return None
        member_score = sum(
            value['score'] for value in member_metrics) / 3.0
        return {
            'score': 0.70 * member_score + 0.30 * spread_metrics['score'],
            'member_score': member_score,
            'spread_score': spread_metrics['score'],
            'member_metrics': member_metrics,
            'spread_metrics': spread_metrics,
            'samples': len(samples),
        }

    def _check_group_samples(self, samples):
        if not samples:
            raise GroupCandidateRejected(
                'too few synchronized position-error samples')
        max_abs = max(
            abs(value) for sample in samples for value in sample['errors'])
        max_spread = max(
            max(sample['errors']) - min(sample['errors'])
            for sample in samples)
        if max_abs > self.max_error_deg:
            raise GroupCandidateRejected(
                'position error %.6f deg exceeded %.6f deg' %
                (max_abs, self.max_error_deg))
        if max_spread > self.max_spread_deg:
            raise GroupCandidateRejected(
                '3Z spread %.6f deg exceeded %.6f deg' %
                (max_spread, self.max_spread_deg))

    def _run_motion(self, toolhead, origin, distance, speed, accel,
                    settle_time):
        if self.autotune_abort:
            raise RuntimeError('3Z autotune cancellation requested')
        saved_limits = None
        returned = False
        for member in self.members:
            member.begin_position_capture(self.max_error_deg)
            member.set_position_capture_phase('motion:z')
        try:
            saved_limits = self._set_motion_limits(toolhead, speed, accel)
            target = list(origin)
            target[2] += distance
            toolhead.manual_move(target, speed)
            toolhead.wait_moves()
            toolhead.manual_move(origin, speed)
            toolhead.wait_moves()
            returned = True
            for member in self.members:
                member.set_position_capture_phase('settle')
            self.reactor.pause(
                self.reactor.monotonic() + settle_time)
        finally:
            if not returned:
                try:
                    toolhead.manual_move(origin, speed)
                    toolhead.wait_moves()
                except Exception:
                    logging.exception(
                        'closed_loop_motor_group %s: failed to return Z '
                        'after tuning',
                        self.name)
            captures = []
            for member in self.members:
                captures.append(member.end_position_capture())
            if saved_limits is not None:
                toolhead.set_max_velocities(*saved_limits)
        if self.autotune_abort:
            raise RuntimeError('3Z autotune cancellation requested')
        for member in self.members:
            member_name = member.closed_loop_name()
            violation = member.consume_position_capture_violation()
            if violation:
                raise GroupCandidateRejected(
                    "%s: %s" % (member_name, violation))
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
            'score': _median([value['score'] for value in metrics]),
            'member_score': _median([
                value['member_score'] for value in metrics]),
            'spread_score': _median([
                value['spread_score'] for value in metrics]),
            'samples': sum(value['samples'] for value in metrics),
        }

    def _run_repeated_motions(self, gcmd, phase, repeats, toolhead,
                              origin, distance, speed, accel, settle_time):
        results = []
        for repeat in range(repeats):
            metrics = self._run_motion(
                toolhead, origin, distance, speed, accel, settle_time)
            results.append(metrics)
            gcmd.respond_info(
                '3Z %s repeat %d/%d: %s' % (
                    phase, repeat + 1, repeats,
                    self._format_metrics(metrics)))
        return self._aggregate_repeat_metrics(results)

    def _write_pid_set(self, values, store=0, verify=True):
        for member, pid in zip(self.members, values):
            if not member.write_position_pid(
                    pid, store=store, verify=verify):
                return False, member
        return True, None

    def _restore_pid_set(self, originals, store):
        failures = []
        for member, pid in zip(self.members, originals):
            member_name = member.closed_loop_name()
            try:
                if not member.write_position_pid(
                        pid, store=store, verify=False):
                    failures.append(member_name)
            except Exception:
                failures.append(member_name)
                logging.exception(
                    "closed_loop_motor_group %s: failed to restore PID "
                    "for '%s'", self.name, member_name)
        return failures

    def _candidate(self, current, member_index, parameter, direction, step):
        member = self.members[member_index]
        candidate = [list(value) for value in current]
        old_value = candidate[member_index][parameter]
        value = old_value + direction * step
        pid_min, pid_max = member.position_pid_bounds()
        if value < pid_min or value > pid_max:
            direction *= -1
            value = old_value + direction * step
        value = max(pid_min, min(pid_max, int(value)))
        if value == old_value:
            return None, direction
        candidate[member_index][parameter] = value
        return [tuple(value) for value in candidate], direction

    def _search_target(self, iteration):
        slot = iteration % 9
        return slot // 3, slot % 3

    def _format_metrics(self, metrics):
        return ('score=%.6f member=%.6f spread=%.6f samples=%d' % (
            metrics['score'], metrics['member_score'],
            metrics['spread_score'], metrics['samples']))

    def _format_pid_set(self, values):
        return '; '.join(
            '%s=%s/%s/%s' % (
                member.closed_loop_name(), pid[0], pid[1], pid[2])
            for member, pid in zip(self.members, values))

    def cmd_AUTOTUNE(self, gcmd):
        if self.autotune_active:
            raise gcmd.error(
                'CLOSED_LOOP_GROUP_AUTOTUNE is already running')
        if gcmd.get_int('CONFIRM', 0, minval=0, maxval=1) != 1:
            raise gcmd.error(
                '3Z autotune moves Z and writes three drivers; pass CONFIRM=1')
        distance = gcmd.get_float(
            'DISTANCE', self.autotune_distance, minval=0.1, maxval=100.0)
        speed = gcmd.get_float(
            'SPEED', self.autotune_speed, minval=0.1, maxval=100.0)
        accel = gcmd.get_float(
            'ACCEL', self.autotune_accel, minval=1.0, maxval=10000.0)
        iterations = gcmd.get_int(
            'ITERATIONS', self.autotune_iterations, minval=1, maxval=1000)
        repeats = gcmd.get_int(
            'REPEATS', self.autotune_repeats, minval=1, maxval=100)
        validation_repeats = gcmd.get_int(
            'VALIDATION_REPEATS', self.autotune_validation_repeats,
            minval=1, maxval=100)
        settle_time = gcmd.get_float(
            'SETTLE', self.autotune_settle_time,
            minval=0.05, maxval=30.0)
        min_improvement = gcmd.get_float(
            'MIN_IMPROVEMENT', self.autotune_min_improvement,
            minval=0.0, maxval=0.50)

        toolhead = None
        origin = None
        originals = None
        current = None
        saved_runtime = None
        persist_attempted = False
        self.autotune_active = True
        self.autotune_abort = False
        try:
            toolhead, origin, originals = self._check_autotune_preconditions(
                gcmd, distance)
            current = [tuple(value) for value in originals]
            saved_runtime = self._prepare_member_capture()
            baseline = self._run_repeated_motions(
                gcmd, 'baseline', repeats, toolhead, origin, distance,
                speed, accel, settle_time)
            best = baseline
            gcmd.respond_info(
                'Closed-loop 3Z baseline: %s; %s' % (
                    self._format_pid_set(current),
                    self._format_metrics(best)))

            directions = [[1 for _ in range(3)] for _ in range(3)]
            failures = [[0 for _ in range(3)] for _ in range(3)]
            steps = [list(member.position_pid_steps())
                     for member in self.members]
            parameter_names = ('Kp', 'Ki', 'Kd')
            for iteration in range(iterations):
                if self.autotune_abort:
                    raise gcmd.error('3Z autotune cancellation requested')
                member_index, parameter = self._search_target(iteration)
                candidate, directions[member_index][parameter] = (
                    self._candidate(
                        current, member_index, parameter,
                        directions[member_index][parameter],
                        steps[member_index][parameter]))
                if candidate is None:
                    failures[member_index][parameter] += 1
                    steps[member_index][parameter] = max(
                        1, steps[member_index][parameter] // 2)
                    continue
                member = self.members[member_index]
                member_name = member.closed_loop_name()
                if not member.write_position_pid(
                        candidate[member_index], store=0, verify=True):
                    raise gcmd.error(
                        "temporary PID write failed for '%s': %s" %
                        (member_name, member.closed_loop_last_error()))
                metrics = None
                rejected = None
                try:
                    metrics = self._run_repeated_motions(
                        gcmd, 'iteration %d %s %s' % (
                            iteration + 1, member_name,
                            parameter_names[parameter]),
                        repeats, toolhead, origin, distance, speed, accel,
                        settle_time)
                except GroupCandidateRejected as exc:
                    rejected = str(exc)
                threshold = best['score'] * (1.0 - min_improvement)
                accepted = metrics is not None and metrics['score'] < threshold
                if accepted:
                    current = candidate
                    best = metrics
                    failures[member_index][parameter] = 0
                else:
                    if not member.write_position_pid(
                            current[member_index], store=0, verify=True):
                        raise gcmd.error(
                            "failed to restore selected PID for '%s': %s" %
                            (member_name,
                             member.closed_loop_last_error()))
                    directions[member_index][parameter] *= -1
                    failures[member_index][parameter] += 1
                    if failures[member_index][parameter] >= 2:
                        steps[member_index][parameter] = max(
                            1, steps[member_index][parameter] // 2)
                        failures[member_index][parameter] = 0
                detail = rejected or self._format_metrics(metrics)
                gcmd.respond_info(
                    '3Z iteration %d %s %s: %s (%s)' % (
                        iteration + 1, member_name,
                        parameter_names[parameter],
                        'accepted' if accepted else 'rejected', detail))

            ok, failed_member = self._write_pid_set(
                originals, store=0, verify=True)
            if not ok:
                raise gcmd.error(
                    "failed to prepare original validation for '%s'" %
                    failed_member.closed_loop_name())
            original_validation = self._run_repeated_motions(
                gcmd, 'original validation', validation_repeats,
                toolhead, origin, distance * 0.70, speed * 0.70,
                accel * 0.70, settle_time)
            ok, failed_member = self._write_pid_set(
                current, store=0, verify=True)
            if not ok:
                raise gcmd.error(
                    "failed to prepare tuned validation for '%s'" %
                    failed_member.closed_loop_name())
            tuned_validation = self._run_repeated_motions(
                gcmd, 'tuned validation', validation_repeats,
                toolhead, origin, distance * 0.70, speed * 0.70,
                accel * 0.70, settle_time)
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
                    '3Z autotune found no validated improvement; original '
                    'PIDs restored. original %s tuned %s' % (
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
                message = 'final 3Z PID readback mismatch'
                if failures:
                    message += '; original restore failed for ' + ', '.join(
                        failures)
                raise gcmd.error(message)
            gcmd.respond_info(
                'Closed-loop 3Z autotune complete: %s; training %s; '
                'validation %s; stored in all three drivers' % (
                    self._format_pid_set(current),
                    self._format_metrics(best),
                    self._format_metrics(tuned_validation)))
        except GroupCandidateRejected as exc:
            if originals is not None:
                self._restore_pid_set(
                    originals, store=1 if persist_attempted else 0)
            raise gcmd.error('3Z autotune safety rejection: %s' % exc)
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
                'closed_loop_motor_group %s: unexpected autotune failure',
                self.name)
            raise gcmd.error('3Z autotune failed safely: %s' % exc)
        finally:
            if saved_runtime is not None:
                self._restore_member_capture(saved_runtime)
            self.autotune_abort = False
            self.autotune_active = False

    def cmd_AUTOTUNE_CANCEL(self, gcmd):
        if not self.autotune_active:
            gcmd.respond_info('Closed-loop 3Z autotune is not running')
            return
        self.autotune_abort = True
        for member in self.members:
            member.request_autotune_cancel()
        gcmd.respond_info(
            'Closed-loop 3Z autotune cancellation requested; the current '
            'move will finish before all original PIDs are restored')

    def _fmt(self, value):
        if not _finite(value):
            return 'None'
        return '%.6f' % float(value)
