import hashlib
from datetime import datetime, timedelta, time

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import UserError


def _merge_intervals(intervals):
    """Merge overlapping/adjacent (start, end) float-hour intervals."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _overlap_total(intervals, window_start, window_end):
    """Total overlap of merged intervals with the [window_start, window_end] window."""
    if window_end <= window_start:
        return 0.0
    total = 0.0
    for s, e in intervals:
        total += max(0.0, min(e, window_end) - max(s, window_start))
    return total


class HrPayrollAttendanceEngine(models.Model):
    """Read-only computation service.

    No database table is created for this model. It owns:
    - the deterministic period-aware summary engine,
    - the single dirty-marking / placeholder-upsert service,
    - the scheduled-actions entry points (crons).
    """
    _name = 'hr.payroll.attendance.engine'
    _description = 'Attendance Summary Engine (service)'
    _auto = False

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    def _get_tz(self, employee, calendar):
        tzname = employee.tz or (calendar.tz if calendar else False) or 'UTC'
        try:
            return pytz.timezone(tzname)
        except Exception:
            return pytz.timezone('UTC')

    def _punch_local(self, punch_naive_utc, tz):
        """naive UTC punch -> aware local datetime."""
        return pytz.utc.localize(punch_naive_utc).astimezone(tz)

    def _local_float(self, local_dt):
        return local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0

    @staticmethod
    def _format_leave_hours(hours):
        if not hours:
            return "0س"
        if hours % 1 == 0:
            return f"{int(hours)}س"
        return f"{hours:.1f}س"

    def _scheduled_intervals(self, calendar, local_date, attendances_for_day):
        """Scheduled (start, end) float-hour intervals for a local date.

        Returns (intervals, is_fixed, week_calendar).
        Fixed-hours calendars produce no intervals (deficit-based offsets).
        """
        if not calendar:
            return [], False, False
        if calendar.use_fixed_work_hours and calendar.fixed_work_hours > 0:
            return [], True, False
        week_calendar = calendar
        if calendar.is_multi_week and attendances_for_day:
            first_att = attendances_for_day[0]
            if first_att.calendar_id:
                week_calendar = first_att.calendar_id
        elif calendar.is_multi_week:
            week_calendar = calendar._get_multi_week_calendar(local_date) or calendar
        lines = week_calendar.attendance_ids.filtered(
            lambda l: l.dayofweek == str(local_date.weekday())
            and (not l.date_from or l.date_from <= local_date)
            and (not l.date_to or l.date_to >= local_date)
        )
        intervals = [(l.hour_from, l.hour_to) for l in lines if l.hour_to > l.hour_from]
        return _merge_intervals(intervals), False, week_calendar

    def _expected_hours(self, calendar, temp_shift, local_date, attendances_for_day,
                        dt_start, dt_end):
        """Legacy-ordered expected-hours resolution with the scheduled intervals."""
        if temp_shift:
            return temp_shift.expected_end - temp_shift.expected_start, False
        if calendar and calendar.use_fixed_work_hours and calendar.fixed_work_hours > 0:
            return calendar.fixed_work_hours, True
        intervals, is_fixed, _wc = self._scheduled_intervals(
            calendar, local_date, attendances_for_day)
        if not is_fixed and calendar:
            if calendar.is_multi_week and attendances_for_day:
                first_att = attendances_for_day[0]
                week_calendar = (
                    first_att.calendar_id if first_att.calendar_id else calendar)
                expected = week_calendar.get_work_hours_count(
                    dt_start, dt_end, compute_leaves=False)
            else:
                expected = calendar.get_work_hours_count(
                    dt_start, dt_end, compute_leaves=False)
            return expected, False
        return 0.0, False

    def _day_scheduled_hours(self, calendar, intervals, is_fixed, fallback_hours):
        """Actual scheduled duration of the day, for pending-hour conversion."""
        if intervals:
            return sum(e - s for s, e in intervals)
        if is_fixed and calendar:
            return calendar.fixed_work_hours
        if calendar and calendar.hours_per_day:
            return calendar.hours_per_day
        return fallback_hours

    def _leave_hour_windows(self, leaves, tz):
        """Classify validated hour-unit leaves into permission / early-excuse windows.

        Returns (perm_windows_by_date, early_windows_by_date, exception_labels).
        Leaves on unreviewed leave types are excluded and reported as
        classification exceptions.
        """
        perm = {}
        early = {}
        exceptions = []
        for leave in leaves:
            ltype = leave.holiday_status_id
            unit = leave.leave_type_request_unit or (
                'half_day' if leave.request_unit_half
                else 'hour' if leave.request_unit_hours else False)
            if not (unit == 'hour' or leave.request_unit_hours):
                continue
            if not ltype or not ltype.classification_reviewed:
                label = ltype.name if ltype else _('نوع غير معروف')
                if label not in exceptions:
                    exceptions.append(label)
                continue
            local_start = pytz.utc.localize(leave.date_from).astimezone(tz)
            local_end = pytz.utc.localize(leave.date_to).astimezone(tz)
            target = early if ltype.is_early_leave_excuse else perm
            day = local_start.date()
            while day <= local_end.date():
                day0 = tz.localize(datetime.combine(day, time.min))
                day1 = tz.localize(datetime.combine(day + timedelta(days=1), time.min))
                start = max(local_start, day0)
                end = min(local_end, day1)
                if start < end:
                    sf = (start - day0).total_seconds() / 3600.0
                    ef = (end - day0).total_seconds() / 3600.0
                    target.setdefault(day, []).append((sf, ef))
                day += timedelta(days=1)
        return (
            {d: _merge_intervals(v) for d, v in perm.items()},
            {d: _merge_intervals(v) for d, v in early.items()},
            exceptions,
        )

    # ------------------------------------------------------------------
    # Core engine
    # ------------------------------------------------------------------

    def _compute_summary_vals(self, summary, run=None):
        """Deterministic day-loop computation for one summary row.

        Returns a vals dict for summary.write(). Never writes anything itself.
        """
        employee = summary.employee_id
        date_from = summary.date_from
        date_to = summary.date_to
        warnings = []
        classification_exceptions = []
        if not (employee and date_from and date_to):
            raise UserError(_('يرجى تحديد الموظف ونطاق التواريخ'))
        if date_from > date_to:
            raise UserError(_('خطأ: تاريخ البداية يجب أن يكون قبل تاريخ النهاية'))

        fallback_hours = float(self.env['ir.config_parameter'].sudo().get_param(
            'hr_payroll_attendance.provisional_day_hours', '8.0'))

        calendar = employee.resource_calendar_id
        tz = self._get_tz(employee, calendar)

        # ---- Batch load ----
        date_to_end = datetime.combine(date_to, time.max)
        attendances = self.env['hr.attendance'].search([
            ('employee_id', '=', employee.id),
            ('check_in', '>=', date_from),
            ('check_in', '<=', date_to_end),
        ])
        attendances_by_date = {}
        for att in attendances:
            local_dt = self._punch_local(att.check_in, tz)
            attendances_by_date.setdefault(local_dt.date(), []).append(att)
        # keep chronological order per day
        for day_att in attendances_by_date.values():
            day_att.sort(key=lambda a: a.check_in)

        all_leaves = self.env['hr.leave'].search([
            ('employee_id', '=', employee.id),
            ('state', '=', 'validate'),
            ('request_date_from', '<=', date_to),
            ('request_date_to', '>=', date_from),
        ])
        pending_leaves = self.env['hr.leave'].search([
            ('employee_id', '=', employee.id),
            ('state', 'in', ['draft', 'confirm', 'validate1']),
            ('request_date_from', '<=', date_to),
            ('request_date_to', '>=', date_from),
        ])
        leaves_intervals = [(l.request_date_from, l.request_date_to, l)
                            for l in all_leaves]
        pending_intervals = [(l.request_date_from, l.request_date_to, l)
                             for l in pending_leaves]

        temp_shifts = self.env['hr.temporary.shift'].search([
            ('employee_id', '=', employee.id),
            ('date', '>=', date_from),
            ('date', '<=', date_to),
        ])
        temp_by_date = {ts.date: ts for ts in temp_shifts}

        perm_windows_by_date, early_windows_by_date, classification_exceptions = \
            self._leave_hour_windows(all_leaves, tz)
        if classification_exceptions:
            warnings.append(
                _('أنواع إجازات ساعات غير مصنفة: %s — ترفع استثناء تصنيف ويمنع اعتماد الفترة')
                % '، '.join(classification_exceptions))

        policy_cache = {}

        def get_policy(policy_type, calendar_id, occurrence, minutes=None):
            if policy_type == 'late':
                key = (policy_type, calendar_id, occurrence, minutes)
            else:
                key = (policy_type, calendar_id, occurrence)
            if key not in policy_cache:
                domain = [
                    ('policy_type', '=', policy_type),
                    ('working_hours_id', '=', calendar_id),
                    ('occurrence_number', '=', occurrence),
                    ('company_id', '=', employee.company_id.id),
                ]
                if minutes is not None:
                    domain += [('minimum_minutes', '<=', minutes),
                               ('maximum_minutes', '>=', minutes)]
                policy_cache[key] = self.env['hr.attendance.policy.details'].search(
                    domain, limit=1)
            return policy_cache[key]

        absence_counter = 0.0
        penalty_counter = 0.0
        leave_counter = 0.0
        pending_leave_counter = 0.0
        late_occurrence_count = 0
        forgot_fp_occurrence_count = 0
        forgot_attendance_count = 0
        total_extra_minutes = 0.0
        total_lateness = 0.0
        net_lateness_minutes_list = []
        log_lines = [
            "=" * 100,
            f"\u200E{'التاريخ':<12} {'متوقع':<8} {'فعلي':<8} {'تأخير (د)':<10} {'الإجراء':<48}",
            "=" * 100,
        ]

        ltr_marker = "\u200E"
        current_date = date_from
        fixed_warning_logged = False

        while current_date <= date_to:
            dt_start = datetime.combine(current_date, time.min)
            dt_end = datetime.combine(current_date, time.max)
            attendances_for_day = attendances_by_date.get(current_date, [])
            has_attendances = bool(attendances_for_day)
            temp_shift = temp_by_date.get(current_date)

            expected_hours, is_fixed = self._expected_hours(
                calendar, temp_shift, current_date, attendances_for_day,
                dt_start, dt_end)
            scheduled_intervals, is_fixed_intervals, _wc = self._scheduled_intervals(
                calendar, current_date, attendances_for_day)
            if temp_shift:
                scheduled_intervals = [(temp_shift.expected_start, temp_shift.expected_end)]

            if is_fixed and not fixed_warning_logged and expected_hours > 0:
                fixed_warning_logged = True
                warnings.append(
                    _('ساعات العمل ثابتة: الجدول الافتراضي بلا أوقات محددة، '
                      'الإذونات تُخصم من العجز دون منطق التداخل'))
            if expected_hours > 0 and not scheduled_intervals and not is_fixed and not temp_shift:
                warnings.append(
                    _('لا توجد ساعات مجدولة لليوم %s رغم ساعات متوقعة') % current_date)

            if expected_hours <= 0:
                log_lines.append(
                    f"{ltr_marker}||{str(current_date)}||-||-||-||-||-||-||عطلة")
                current_date += timedelta(days=1)
                continue

            day_sched = self._day_scheduled_hours(
                calendar, scheduled_intervals, is_fixed, fallback_hours)
            if not scheduled_intervals and not is_fixed and not temp_shift:
                warnings.append(_(
                    'لا توجد ساعات عمل معرفة لليوم %s: تم استخدام %.1f ساعة افتراضية')
                    % (current_date, day_sched))

            # ---- Leaves ----
            leaves = [leave for s, e, leave in leaves_intervals
                      if s <= current_date <= e]
            pending_current = [leave for s, e, leave in pending_intervals
                               if s <= current_date <= e]

            is_excused = False
            has_day_mission = False
            excused_hours = 0.0
            excuse_reason = ""
            day_leave_counted = False
            day_time_off_label = "-"

            for leave in leaves:
                ltype = leave.holiday_status_id
                is_mission_type = bool(ltype and ltype.is_mission)
                if ltype and ltype.is_late_excuse:
                    is_excused = True
                    if not day_leave_counted:
                        if leave.leave_type_request_unit == 'day':
                            leave_counter += 1.0
                            day_leave_counted = True
                        elif leave.leave_type_request_unit == 'half_day' \
                                and leave.request_unit_half:
                            leave_counter += 0.5
                            day_leave_counted = True
                    excuse_reason = f"إجازة معفية: {ltype.name}"
                    day_time_off_label = f"{ltype.name}"
                    break
                if leave.leave_type_request_unit == 'day' or (
                        ltype and ltype.time_type == 'other'
                        and not leave.request_unit_half
                        and not leave.request_unit_hours):
                    if is_mission_type and has_attendances:
                        has_day_mission = True
                        if not day_leave_counted:
                            leave_counter += 1.0
                            day_leave_counted = True
                        if day_time_off_label == "-":
                            day_time_off_label = f"{ltype.name}"
                        else:
                            day_time_off_label += f" + {ltype.name}"
                    else:
                        is_excused = True
                        if not day_leave_counted:
                            leave_counter += 1.0
                            day_leave_counted = True
                        if is_mission_type:
                            excuse_reason = f"مأمورية كاملة: {ltype.name}"
                        else:
                            excuse_reason = f"إجازة كاملة: {ltype.name}"
                        day_time_off_label = f"{ltype.name}"
                        break
                elif leave.leave_type_request_unit == 'half_day' \
                        or leave.request_unit_half:
                    if leave.request_unit_half:
                        excused_hours += expected_hours * 0.5
                        if not day_leave_counted:
                            leave_counter += 0.5
                            day_leave_counted = True
                        excuse_reason = f"نصف يوم: {ltype.name}"
                        day_time_off_label = f"{ltype.name} (نصف يوم)"
                    else:
                        if is_mission_type and has_attendances:
                            has_day_mission = True
                            if not day_leave_counted:
                                leave_counter += 1.0
                                day_leave_counted = True
                            if day_time_off_label == "-":
                                day_time_off_label = f"{ltype.name}"
                            else:
                                day_time_off_label += f" + {ltype.name}"
                        else:
                            is_excused = True
                            if not day_leave_counted:
                                leave_counter += 1.0
                                day_leave_counted = True
                            if is_mission_type:
                                excuse_reason = f"مأمورية كاملة: {ltype.name}"
                            else:
                                excuse_reason = f"إجازة كاملة: {ltype.name}"
                            day_time_off_label = f"{ltype.name}"
                            break
                elif leave.leave_type_request_unit == 'hour' or leave.request_unit_hours:
                    hour_label = self._format_leave_hours(leave.number_of_hours_display)
                    leave_label = f"{ltype.name} ({hour_label})" if ltype else f"إذن ({hour_label})"
                    if day_time_off_label == "-":
                        day_time_off_label = leave_label
                    else:
                        day_time_off_label += f" + {leave_label}"

            for leave in pending_current:
                unit = leave.leave_type_request_unit or (
                    'half_day' if leave.request_unit_half
                    else 'hour' if leave.request_unit_hours else False)
                if unit == 'day':
                    pending_leave_counter += 1.0
                elif unit == 'half_day' or leave.request_unit_half:
                    pending_leave_counter += 0.5
                elif unit == 'hour' or leave.request_unit_hours:
                    if day_sched > 0:
                        pending_leave_counter += leave.number_of_hours_display / day_sched
                    else:
                        pending_leave_counter += leave.number_of_hours_display / fallback_hours

            if is_excused:
                excuse_detail = excuse_reason.split(':', 1)[1].strip() \
                    if ':' in excuse_reason else excuse_reason
                if 'مأمورية' in excuse_reason or 'مامورية' in excuse_reason:
                    log_lines.append(
                        f"{ltr_marker}||{str(current_date)}||"
                        f"{self._float_to_hours(expected_hours)}||-||-||-||مأمورية||{excuse_detail}||-")
                else:
                    log_lines.append(
                        f"{ltr_marker}||{str(current_date)}||-||-||-||-||time off||{excuse_detail}||-")
                current_date += timedelta(days=1)
                continue

            # ---- Attendance ----
            att_intervals = []
            valid_attendances_count = 0
            for att in attendances_for_day:
                if att.check_in and att.check_out and att.check_out > att.check_in:
                    local_in = self._punch_local(att.check_in, tz)
                    local_out = self._punch_local(att.check_out, tz)
                    sf = self._local_float(local_in)
                    ef = self._local_float(local_out)
                    if ef > sf and (ef - sf) >= (10.0 / 60.0):
                        att_intervals.append((sf, ef))
                        valid_attendances_count += 1
            merged_intervals = _merge_intervals(att_intervals)
            valid_worked_hours = sum(e - s for s, e in merged_intervals)

            first_att = attendances_for_day[0] if attendances_for_day else None
            policy_calendar_id = first_att.calendar_id.id \
                if first_att and first_att.calendar_id else (
                calendar.id if calendar else False)

            adjusted_expected_hours = expected_hours - excused_hours

            # Scenario A: no punches -> absence
            if not attendances_for_day:
                if adjusted_expected_hours > 0:
                    if is_fixed:
                        absence_occurrence = int(absence_counter) + 1
                        policy_detail = get_policy(
                            'fixed_hours_absence', policy_calendar_id,
                            absence_occurrence)
                        absence_counter += 1.0
                        if policy_detail:
                            penalty_counter += policy_detail.penalty_days
                            log_lines.append(
                                f"{ltr_marker}||{str(current_date)}||"
                                f"{self._float_to_hours(expected_hours)}||00:00||-||-||"
                                f"غياب: جزاء {policy_detail.penalty_days} يوم||"
                                f"{day_time_off_label}||{policy_detail.name or 'غياب'}")
                        else:
                            log_lines.append(
                                f"{ltr_marker}||{str(current_date)}||"
                                f"{self._float_to_hours(expected_hours)}||00:00||-||-||"
                                f"غياب (لا توجد سياسة)||{day_time_off_label}||-")
                    else:
                        absence_counter += 1.0
                        log_lines.append(
                            f"{ltr_marker}||{str(current_date)}||"
                            f"{self._float_to_hours(expected_hours)}||00:00||-||-||"
                            f"غياب كامل||{day_time_off_label}||-")
                current_date += timedelta(days=1)
                continue

            # Scenario B: no valid session -> forgot fingerprint
            if valid_attendances_count == 0:
                if has_day_mission:
                    log_lines.append(
                        f"{ltr_marker}||{str(current_date)}||"
                        f"{self._float_to_hours(expected_hours)}||-||-||-||"
                        f"مأمورية (بصمة غير مكتملة)||{day_time_off_label}||-")
                    current_date += timedelta(days=1)
                    continue
                forgot_fp_occurrence_count += 1
                forgot_attendance_count += 1
                policy_detail = get_policy(
                    'forgot_fp', policy_calendar_id, forgot_fp_occurrence_count)
                if policy_detail:
                    penalty_counter += policy_detail.penalty_days
                    log_lines.append(
                        f"{ltr_marker}||{str(current_date)}||"
                        f"{self._float_to_hours(expected_hours)}||-||-||-||"
                        f"نسيان بصمة: جزاء {policy_detail.penalty_days} يوم||"
                        f"{day_time_off_label}||{policy_detail.name or 'نسيان بصمة'}")
                else:
                    log_lines.append(
                        f"{ltr_marker}||{str(current_date)}||"
                        f"{self._float_to_hours(expected_hours)}||-||-||-||"
                        f"نسيان بصمة (لا توجد سياسة)||{day_time_off_label}||-")
                current_date += timedelta(days=1)
                continue

            # Scenario C: valid session
            actual_worked_hours = valid_worked_hours

            if has_day_mission:
                extra_minutes = 0
                if actual_worked_hours > expected_hours:
                    extra_minutes = int((actual_worked_hours - expected_hours) * 60)
                    total_extra_minutes += extra_minutes
                log_lines.append(
                    f"{ltr_marker}||{str(current_date)}||"
                    f"{self._float_to_hours(expected_hours)}||"
                    f"{self._float_to_hours(actual_worked_hours)}||0||{extra_minutes}||"
                    f"حاضر (مأمورية)||{day_time_off_label}||-")
                current_date += timedelta(days=1)
                continue

            raw_deficit = adjusted_expected_hours - actual_worked_hours
            deficit_minutes = max(0, int(raw_deficit * 60))

            # ---- Offset ledger (single shared deficit, no double counting) ----
            offsets_minutes = 0
            offset_detail = []
            perm_windows = perm_windows_by_date.get(current_date, [])
            early_windows = early_windows_by_date.get(current_date, [])
            hour_leave_windows = _merge_intervals(perm_windows + early_windows)
            if deficit_minutes >= 1 and hour_leave_windows:
                if is_fixed or not scheduled_intervals:
                    # No defined schedule: full permission/early hours offset the deficit
                    perm_total = sum(e - s for s, e in hour_leave_windows)
                    offsets_minutes = int(perm_total * 60)
                    offset_detail.append('إذن')
                else:
                    # Calculate overlap of approved hour leaves (permissions / missions) with scheduled shift
                    # Each hour of approved leave falling within shift covers deficit
                    total_leave_in_sched = sum(
                        _overlap_total(scheduled_intervals, s, e)
                        for s, e in hour_leave_windows)
                    offsets_minutes = int(round(total_leave_in_sched * 60))
                    if offsets_minutes > 0:
                        offset_detail.append('إذن %dد' % offsets_minutes)
                offsets_minutes = min(offsets_minutes, deficit_minutes)

            effective_deficit_minutes = max(
                deficit_minutes - offsets_minutes, 0)

            if deficit_minutes >= 1 and effective_deficit_minutes > 0:
                late_occurrence_count += 1
                net_lateness_minutes_list.append(f"{effective_deficit_minutes}")
                total_lateness += effective_deficit_minutes
                policy_detail = get_policy(
                    'late', policy_calendar_id, late_occurrence_count,
                    effective_deficit_minutes)
                if policy_detail:
                    penalty_counter += policy_detail.penalty_days
                    log_lines.append(
                        f"{ltr_marker}||{str(current_date)}||"
                        f"{self._float_to_hours(expected_hours)}||"
                        f"{self._float_to_hours(actual_worked_hours)}||"
                        f"{effective_deficit_minutes}||0||"
                        f"تأخير: جزاء {policy_detail.penalty_days} يوم||"
                        f"{day_time_off_label}||{policy_detail.name or 'تأخير'}")
                else:
                    log_lines.append(
                        f"{ltr_marker}||{str(current_date)}||"
                        f"{self._float_to_hours(expected_hours)}||"
                        f"{self._float_to_hours(actual_worked_hours)}||"
                        f"{effective_deficit_minutes}||0||"
                        f"تأخير (لا توجد سياسة)||{day_time_off_label}||-")
            elif deficit_minutes >= 1:
                extra_minutes = 0
                if actual_worked_hours > expected_hours:
                    extra_minutes = int((actual_worked_hours - expected_hours) * 60)
                    total_extra_minutes += extra_minutes
                suffix = f" ({'، '.join(offset_detail)})" if offset_detail else ""
                log_lines.append(
                    f"{ltr_marker}||{str(current_date)}||"
                    f"{self._float_to_hours(expected_hours)}||"
                    f"{self._float_to_hours(actual_worked_hours)}||0||{extra_minutes}||"
                    f"تأخير مغطى بالإذن{suffix}||{day_time_off_label}||-")
            else:
                extra_minutes = 0
                if actual_worked_hours > expected_hours:
                    extra_minutes = int((actual_worked_hours - expected_hours) * 60)
                    total_extra_minutes += extra_minutes
                    log_lines.append(
                        f"{ltr_marker}||{str(current_date)}||"
                        f"{self._float_to_hours(expected_hours)}||"
                        f"{self._float_to_hours(actual_worked_hours)}||0||"
                        f"{extra_minutes}||حاضر||{day_time_off_label}||-")
                else:
                    log_lines.append(
                        f"{ltr_marker}||{str(current_date)}||"
                        f"{self._float_to_hours(expected_hours)}||"
                        f"{self._float_to_hours(actual_worked_hours)}||0||0||"
                        f"حاضر||{day_time_off_label}||-")

            current_date += timedelta(days=1)

        log_lines.append("=" * 100)
        log_lines.append(
            f"====الملخص====|الغياب={absence_counter}|الإجازات={leave_counter}"
            f"|الإجازات المعلقة={pending_leave_counter}|الجزاءات={penalty_counter} يوم"
            f"|التأخير={total_lateness:.0f} دقيقة|الإضافي={total_extra_minutes:.0f} دقيقة")
        log_lines.append("=" * 100)

        calculation_log = "\n".join(log_lines)
        has_activity = bool(
            absence_counter or penalty_counter or leave_counter
            or pending_leave_counter or forgot_attendance_count
            or net_lateness_minutes_list or total_extra_minutes)

        quantity_hash = self._make_quantity_hash(
            absence_counter, penalty_counter, leave_counter,
            pending_leave_counter, total_lateness, total_extra_minutes,
            forgot_attendance_count)

        vals = {
            'total_absence_days': absence_counter,
            'total_penalty_days': penalty_counter,
            'total_leave_days': leave_counter,
            'pending_leave_days': pending_leave_counter,
            'forgot_attendance_count': forgot_attendance_count,
            'calculation_log': calculation_log,
            'net_lateness_minutes_list': ','.join(net_lateness_minutes_list),
            'total_lateness_hours': total_lateness / 60.0,
            'total_extra_hours': total_extra_minutes / 60.0,
            'last_computed_date': fields.Datetime.now(),
            'calc_state': 'computed',
            'dirty': False,
            'last_error': False,
            'error_count': 0,
            'last_checked': fields.Datetime.now(),
            'last_run_id': run.id if run else False,
            'has_activity': has_activity,
            'quantity_hash': quantity_hash,
            'data_quality_warning': '\n'.join(warnings) if warnings else False,
            'classification_exception':
                '، '.join(classification_exceptions) if classification_exceptions else False,
        }
        return vals

    def _make_quantity_hash(self, absence, penalty, leave, pending,
                            lateness_min, extra_min, forgot):
        raw = "%.4f|%.4f|%.4f|%.4f|%.0f|%.0f|%d" % (
            absence, penalty, leave, pending, lateness_min, extra_min, forgot)
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    @staticmethod
    def _float_to_hours(hours):
        if not hours:
            return "00:00"
        h = int(hours)
        m = int(round((hours - h) * 60))
        if m == 60:
            h += 1
            m = 0
        return f"{h:02d}:{m:02d}"

    # ------------------------------------------------------------------
    # History + apply
    # ------------------------------------------------------------------

    def _apply_vals(self, summary, vals, run=None):
        """Snapshot history on quantity change, then write the new values."""
        Summary = self.env['hr.payroll.attendance.summary']
        History = self.env['hr.payroll.attendance.summary.history']
        prev_hash = summary.quantity_hash
        if prev_hash and vals.get('quantity_hash') != prev_hash:
            History.sudo().create({
                'summary_id': summary.id,
                'run_id': run.id if run else None,
                'computed_at': fields.Datetime.now(),
                'computed_by': self.env.user.id,
                'quantity_hash': prev_hash,
                'total_absence_days': summary.total_absence_days,
                'total_penalty_days': summary.total_penalty_days,
                'total_leave_days': summary.total_leave_days,
                'pending_leave_days': summary.pending_leave_days,
                'total_lateness_hours': summary.total_lateness_hours,
                'total_extra_hours': summary.total_extra_hours,
                'forgot_attendance_count': summary.forgot_attendance_count,
                'net_lateness_minutes_list': summary.net_lateness_minutes_list,
                'calculation_log': summary.calculation_log,
            })
        summary.sudo().write(vals)
        return True

    # ------------------------------------------------------------------
    # Dirty marking / placeholder upsert (single service)
    # ------------------------------------------------------------------

    def _open_periods(self, company_ids=None):
        domain = [('state', 'in', ('draft', 'in_progress', 'pending_approval', 'approved'))]
        if company_ids:
            domain.append(('company_id', 'in', company_ids))
        return self.env['hr.payroll.attendance.period'].sudo().search(domain)

    def mark_dirty(self, employee_ids, periods=None):
        """Single dirty-marking entry point.

        - creates placeholder summaries ('uncomputed', dirty) for employees
          missing them in open periods,
        - flags existing rows dirty ('computed' -> 'stale').
        Only touches non-closed, non-cancelled periods.
        """
        employee_ids = [eid for eid in employee_ids if eid]
        if not employee_ids:
            return True
        Summary = self.env['hr.payroll.attendance.summary'].sudo()
        periods = periods or self._open_periods()
        for period in periods:
            employees = self.env['hr.employee'].sudo().search([
                ('id', 'in', employee_ids),
                ('company_id', '=', period.company_id.id),
            ])
            if not employees:
                continue
            self.env.cr.execute(
                "SELECT employee_id FROM hr_payroll_attendance_summary "
                "WHERE period_id = %s AND employee_id IN %s",
                [period.id, tuple(employees.ids)])
            existing = {row[0] for row in self.env.cr.fetchall()}
            missing = [emp for emp in employees if emp.id not in existing]
            if missing:
                Summary.create([{
                    'employee_id': emp.id,
                    'company_id': emp.company_id.id,
                    'period_id': period.id,
                    'date_from': period.date_from,
                    'date_to': period.date_to,
                    'calc_state': 'uncomputed',
                    'dirty': True,
                } for emp in missing])
            self.env.flush_all()
            self.env.cr.execute(
                "UPDATE hr_payroll_attendance_summary "
                "SET dirty = true, last_checked = NOW() AT TIME ZONE 'UTC', "
                "    calc_state = CASE WHEN calc_state = 'computed' THEN 'stale' "
                "                      ELSE calc_state END "
                "WHERE period_id = %s AND employee_id IN %s",
                [period.id, tuple(employees.ids)])
        self.env.invalidate_all()
        return True

    def ensure_period_summaries(self, period, employee_ids=None):
        """Create missing placeholder summaries for a period (idempotent)."""
        Summary = self.env['hr.payroll.attendance.summary'].sudo()
        if employee_ids:
            employees = self.env['hr.employee'].sudo().browse(employee_ids).exists()
        else:
            employees = self.env['hr.employee'].sudo().search([
                ('company_id', '=', period.company_id.id),
                ('active', '=', True),
            ])
        if not employees:
            return 0
        self.env.cr.execute(
            "SELECT employee_id FROM hr_payroll_attendance_summary "
            "WHERE period_id = %s AND employee_id IN %s",
            [period.id, tuple(employees.ids)])
        existing = {row[0] for row in self.env.cr.fetchall()}
        missing = [emp for emp in employees if emp.id not in existing]
        if missing:
            Summary.create([{
                'employee_id': emp.id,
                'company_id': emp.company_id.id,
                'period_id': period.id,
                'date_from': period.date_from,
                'date_to': period.date_to,
                'calc_state': 'uncomputed',
                'dirty': True,
            } for emp in missing])
        self.env.invalidate_all()
        return len(missing)

    # ------------------------------------------------------------------
    # Cron entry points
    # ------------------------------------------------------------------

    def _cron_process_dirty(self):
        """Every 5 min: resume draft / stale-running runs; start runs for
        dirty open periods. A 'running' run with a fresh heartbeat is being
        processed by a live worker and is left alone."""
        Run = self.env['hr.payroll.attendance.calc.run'].sudo()
        resume_cutoff = fields.Datetime.to_datetime(
            fields.Datetime.now()) - timedelta(minutes=3)
        resumable = Run.search(['|',
                                ('state', '=', 'draft'),
                                '&',
                                ('state', '=', 'running'),
                                ('heartbeat', '<', resume_cutoff)])
        for run in resumable:
            run.action_process()
        self.env.cr.execute(
            "SELECT DISTINCT s.period_id FROM hr_payroll_attendance_summary s "
            "JOIN hr_payroll_attendance_period p ON p.id = s.period_id "
            "WHERE s.dirty AND p.state IN ('draft', 'in_progress')")
        period_ids = [row[0] for row in self.env.cr.fetchall()]
        for period in self.env['hr.payroll.attendance.period'].sudo().browse(period_ids):
            if not Run.search_count([
                    ('period_id', '=', period.id),
                    ('state', 'in', ('draft', 'running'))]):
                run = Run.create({
                    'period_id': period.id,
                    'trigger': 'cron',
                })
                run.action_process()
        return True

    def _cron_daily_reconcile(self):
        """Daily 04:30 UTC: create missing placeholders + re-dirty broken rows."""
        for period in self._open_periods():
            if period.state not in ('draft', 'in_progress'):
                continue
            self.ensure_period_summaries(period)
            self.env.flush_all()
            self.env.cr.execute(
                "UPDATE hr_payroll_attendance_summary "
                "SET dirty = true, last_checked = NOW() AT TIME ZONE 'UTC' "
                "WHERE period_id = %s "
                "  AND calc_state IN ('uncomputed', 'stale', 'error')",
                [period.id])
        self.env.invalidate_all()
        return True

    def _cron_watchdog(self):
        """Every 10 min: recover stale runs and orphaned 'computing' rows."""
        Run = self.env['hr.payroll.attendance.calc.run'].sudo()
        stale_minutes = int(self.env['ir.config_parameter'].sudo().get_param(
            'hr_payroll_attendance.watchdog_stale_minutes', '20'))
        cutoff = fields.Datetime.to_datetime(fields.Datetime.now()) - timedelta(
            minutes=stale_minutes)
        stale_runs = Run.search([
            ('state', '=', 'running'),
            ('heartbeat', '<', cutoff),
        ])
        for run in stale_runs:
            run.write({
                'state': 'failed',
                'finished_at': fields.Datetime.now(),
                'notes': (run.notes or '') + '\n'
                         + _('استرداد: توقف التشغيل (heartbeat قديم) — تم إنشاء تشغيل تعويضي'),
            })
            if run.period_id.state in ('draft', 'in_progress'):
                recovery = Run.create({
                    'period_id': run.period_id.id,
                    'trigger': 'watchdog',
                })
                recovery.action_process()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE hr_payroll_attendance_summary "
            "SET calc_state = 'stale', dirty = true, "
            "    last_checked = NOW() AT TIME ZONE 'UTC' "
            "WHERE calc_state = 'computing' "
            "  AND write_date < %s "
            "  AND period_id IS NOT NULL",
            [fields.Datetime.now().replace(microsecond=0) - timedelta(
                minutes=stale_minutes)])
        self.env.invalidate_all()
        return True
