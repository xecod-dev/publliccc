from odoo import models, fields, api, _  # type: ignore
from datetime import timedelta, datetime, time
from odoo.exceptions import ValidationError, UserError


class HrPayrollAttendanceSummary(models.Model):
    _name = 'hr.payroll.attendance.summary'
    _description = 'محرك مؤثرات الرواتب والجزاءات'

    employee_id = fields.Many2one('hr.employee', string='الموظف', required=True)
    department_id = fields.Many2one(
        'hr.department',
        string='القسم',
        related='employee_id.department_id',
        store=True,
        readonly=True
    )
    work_location = fields.Many2one(
        'hr.work.location',
        string='مقر العمل',
        related='employee_id.work_location_id',
        store=True,
        readonly=True
    )
    date_from = fields.Date(string='من تاريخ', required=True)
    date_to = fields.Date(string='إلى تاريخ', required=True)
    company_id = fields.Many2one('res.company', string='الشركة', default=lambda self: self.env.company)

    # Period binding (NULL = legacy pre-period row, computed by the legacy engine)
    period_id = fields.Many2one(
        'hr.payroll.attendance.period', string='الفترة', index=True,
        ondelete='restrict')
    calc_state = fields.Selection([
        ('uncomputed', 'لم يحسب'),
        ('computing', 'جارٍ الحساب'),
        ('computed', 'محسوب'),
        ('stale', 'قديم'),
        ('error', 'خطأ'),
    ], string='حالة الحساب', default='uncomputed', required=True, index=True)
    dirty = fields.Boolean(string='بحاجة لإعادة حساب', default=False, index=True)
    last_error = fields.Text(string='آخر خطأ', readonly=True, copy=False)
    error_count = fields.Integer(string='عدد الأخطاء', default=0)
    last_checked = fields.Datetime(string='آخر فحص', readonly=True)
    last_run_id = fields.Many2one(
        'hr.payroll.attendance.calc.run', string='آخر تشغيل', readonly=True)
    has_activity = fields.Boolean(
        string='له مؤثرات', compute='_compute_has_activity', store=True)
    quantity_hash = fields.Char(string='بصمة الكميات', index=True)
    data_quality_warning = fields.Text(
        string='تحذيرات جودة البيانات', readonly=True, copy=False)
    classification_exception = fields.Text(
        string='استثناء تصنيف', readonly=True, copy=False,
        help='أنواع إجازات ساعات غير مصنفة — يمنع اعتماد الفترة حتى تصنيفها')
    history_ids = fields.One2many(
        'hr.payroll.attendance.summary.history', 'summary_id', string='سجل التغييرات')

    # Manual calculation fields (not auto-computed)
    total_absence_days = fields.Float(
        string='إجمالي أيام الغياب الفعلي',
        default=0.0
    )
    total_penalty_days = fields.Float(
        string='إجمالي أيام جزاءات التأخير',
        default=0.0
    )
    total_leave_days = fields.Float(
        string='إجمالي أيام الإجازة',
        default=0.0
    )
    pending_leave_days = fields.Float(
        string='أيام الإجازة قيد الانتظار',
        default=0.0,
        help='Number of leave days with pending approval status (Waiting Approval/Waiting Second Approval)'
    )

    # Detailed calculation log
    calculation_log = fields.Text(
        string='تفاصيل الحساب'
    )

    # HTML table view of calculation log
    calculation_log_html = fields.Html(
        string='تفاصيل الحساب',
        compute='_compute_calculation_log_html',
        sanitize=False
    )

    # Track last computation
    last_computed_date = fields.Datetime(string='آخر حساب', readonly=True)

    is_computed = fields.Boolean(
        string='تم الحساب',
        compute='_compute_is_computed',
        store=False,
        help='Indicates if the record has been computed with current dates'
    )

    # Store all net lateness minutes as comma-separated string
    net_lateness_minutes_list = fields.Char(
        string='دقائق التأخير'
    )

    # Total lateness and extra hours (computed during recalculation, stored as decimal hours for float_time widget)
    total_lateness_hours = fields.Float(
        string='الإجمالي التأخير',
        default=0.0,
        help='Stored as decimal hours (e.g., 1.5 = 01:30)'
    )
    total_extra_hours = fields.Float(
        string='الإجمالي الإضافي',
        default=0.0,
        help='Stored as decimal hours (e.g., 1.5 = 01:30)'
    )

    # Count of forgotten attendances (missing check-out or same time check-in/out)
    forgot_attendance_count = fields.Integer(
        string='عدد مرات نسيان البصمة',
        default=0,
        help='Count of days with missing check-out or same check-in/out time'
    )

    @api.depends('total_absence_days', 'total_penalty_days', 'total_leave_days',
                 'pending_leave_days', 'forgot_attendance_count',
                 'total_extra_hours', 'net_lateness_minutes_list')
    def _compute_has_activity(self):
        for rec in self:
            rec.has_activity = bool(
                rec.total_absence_days or rec.total_penalty_days
                or rec.total_leave_days or rec.pending_leave_days
                or rec.forgot_attendance_count or rec.total_extra_hours
                or rec.net_lateness_minutes_list)

    @api.constrains('period_id')
    def _check_period_uniqueness(self):
        for rec in self:
            if rec.period_id:
                duplicate = self.search([
                    ('id', '!=', rec.id),
                    ('employee_id', '=', rec.employee_id.id),
                    ('period_id', '=', rec.period_id.id),
                ], limit=1)
                if duplicate:
                    raise ValidationError(_(
                        'يوجد مسبقاً ملخص لنفس الموظف في الفترة %s')
                        % rec.period_id.period_key)

    @staticmethod
    def _float_to_hours(hours):
        """Convert float hours to HH:MM format"""
        if not hours:
            return "00:00"
        h = int(hours)
        m = int(round((hours - h) * 60))
        if m == 60:
            h += 1
            m = 0
        return f"{h:02d}:{m:02d}"

    @staticmethod
    def _format_leave_hours(hours):
        """Format leave hours nicely without losing decimals:
        - 0.5 -> '0.5س'
        - 1.0 -> '1س'
        - 1.5 -> '1.5س'
        - 2.0 -> '2س'
        """
        if not hours:
            return "0س"
        if hours % 1 == 0:
            return f"{int(hours)}س"
        return f"{hours:g}س"

    @staticmethod
    def _is_mission(leave):
        """Legacy name heuristic — used ONLY for legacy (period-less) rows."""
        name = (leave.holiday_status_id.name or '').strip()
        return 'مأمور' in name or 'مامور' in name or 'mission' in name.lower()

    @api.depends('last_computed_date', 'date_from', 'date_to')
    def _compute_is_computed(self):
        """Check if current dates match last computation"""
        for rec in self:
            if not rec.last_computed_date or not rec.date_from or not rec.date_to:
                rec.is_computed = False
                continue
            rec.is_computed = (
                rec.date_from <= rec.last_computed_date.date() <= rec.date_to
            )

    @api.depends('calculation_log')
    def _compute_calculation_log_html(self):
        """Parse || delimited log lines and convert to HTML table"""
        for rec in self:
            if not rec.calculation_log:
                rec.calculation_log_html = ''
                continue

            log_text = rec.calculation_log.strip()
            if not log_text:
                rec.calculation_log_html = ''
                continue

            lines = log_text.split('\n')
            html_parts = []
            html_parts.append('<table class="table table-bordered table-striped" style="direction: rtl; text-align: right;">')
            html_parts.append('<thead><tr class="table-primary">')
            html_parts.append('<th>التاريخ</th><th>الساعات المتوقعة</th><th>الساعات الفعلية</th><th>دقائق التأخير</th><th class="extra-col" >دقائق الإضافي</th><th>السياسة</th><th>time off type</th><th>الحالة/الجزاء</th></tr></thead>')
            html_parts.append('<tbody>')

            for line in lines:
                if line.startswith('='):
                    html_parts.append(f'<tr><td colspan="8" class="table-secondary" style="text-align: center;"><strong>{line}</strong></td></tr>')
                elif line.startswith('====الملخص===='):
                    html_parts.append(f'<tr><td colspan="8" class="table-info" style="text-align: center;"><strong>{line}</strong></td></tr>')
                elif '||' in line:
                    parts = line.split('||')
                    if parts[0] == '\u200E' or parts[0] == '':
                        parts = parts[1:]
                    while len(parts) < 8:
                        parts.append('')
                    html_parts.append('<tr>')
                    html_parts.append(f'<td>{parts[0]}</td>')  # Date
                    html_parts.append(f'<td>{parts[1]}</td>')  # Expected
                    html_parts.append(f'<td>{parts[2]}</td>')  # Actual
                    html_parts.append(f'<td>{parts[3]}</td>')  # Lateness
                    html_parts.append(f'<td class="extra-col" >{parts[4]}</td>')  # Extra
                    html_parts.append(f'<td>{parts[5]}</td>')  # Policy
                    html_parts.append(f'<td>{parts[6]}</td>')  # time off type
                    html_parts.append(f'<td>{parts[7]}</td>')  # Status
                    html_parts.append('</tr>')

            html_parts.append('</tbody></table>')
            rec.calculation_log_html = ''.join(html_parts)

    def _compute_payroll_inputs(self):
        """LEGACY engine — used only for rows without a period binding
        (period_id = NULL). The 2,176 legacy rows are computed by exactly
        this code path, preserving pre-upgrade behavior including the
        legacy mission-name heuristic."""
        for rec in self:
            # Validate date range
            if rec.date_from and rec.date_to and rec.date_from > rec.date_to:
                rec.calculation_log = _('خطأ: تاريخ البداية يجب أن يكون قبل تاريخ النهاية')
                rec.total_absence_days = 0.0
                rec.total_penalty_days = 0.0
                rec.total_leave_days = 0.0
                rec.pending_leave_days = 0.0
                rec.forgot_attendance_count = 0
                rec.net_lateness_minutes_list = ''
                rec.total_lateness_hours = 0.0
                rec.total_extra_hours = 0.0
                continue

            if not (rec.employee_id and rec.date_from and rec.date_to):
                rec.total_absence_days = 0.0
                rec.total_penalty_days = 0.0
                rec.calculation_log = _('يرجى تحديد الموظف ونطاق التواريخ')
                rec.net_lateness_minutes_list = ''
                rec.pending_leave_days = 0.0
                continue

            absence_counter = 0.0
            penalty_counter = 0.0
            leave_counter = 0.0
            pending_leave_counter = 0.0
            late_occurrence_count = 0
            forgot_fp_occurrence_count = 0
            forgot_attendance_count = 0
            log_lines = []
            net_lateness_minutes_list = []
            total_extra_minutes = 0.0
            total_lateness = 0.0

            log_lines.append("=" * 100)
            log_lines.append(f"\u200E{'التاريخ':<12} {'متوقع':<8} {'فعلي':<8} {'تأخير (د)':<10} {'الإجراء':<48}")
            log_lines.append("=" * 100)

            calendar = rec.employee_id.resource_calendar_id
            employee = rec.employee_id

            # OPTIMIZATION: Batch load all data upfront
            # Load all attendances for the date range
            # IMPORTANT: Convert date_to to end of day (23:59:59) to include all attendances on that day
            date_to_end = datetime.combine(rec.date_to, time.max)  # 2026-03-31 23:59:59.999999
            all_attendances = rec.env['hr.attendance'].search([
                ('employee_id', '=', employee.id),
                ('check_in', '>=', rec.date_from),
                ('check_in', '<=', date_to_end)
            ])

            # Group attendances by date for fast lookup - store ALL attendances per date
            attendances_by_date = {}
            for att in all_attendances:
                att_date = att.check_in.date()
                attendances_by_date.setdefault(att_date, []).append(att)

            # Load all approved leaves for the date range
            all_leaves = rec.env['hr.leave'].search([
                ('employee_id', '=', employee.id),
                ('state', '=', 'validate'),
                ('request_date_from', '<=', rec.date_to),
                ('request_date_to', '>=', rec.date_from)
            ])

            # Load pending leaves (waiting approval) separately
            pending_leaves = rec.env['hr.leave'].search([
                ('employee_id', '=', employee.id),
                ('state', 'in', ['draft', 'confirm', 'validate1']),
                ('request_date_from', '<=', rec.date_to),
                ('request_date_to', '>=', rec.date_from)
            ])

            # Store leaves as intervals (memory efficient for long leaves)
            leaves_intervals = []
            for leave in all_leaves:
                leaves_intervals.append((leave.request_date_from, leave.request_date_to, leave))

            # Store pending leaves as intervals separately
            pending_leaves_intervals = []
            for leave in pending_leaves:
                pending_leaves_intervals.append((leave.request_date_from, leave.request_date_to, leave))

            # Load all temporary shifts for the date range
            # IMPORTANT: Convert date_to to end of day to include all temp shifts on the last day
            date_to_end = datetime.combine(rec.date_to, time.max)
            all_temp_shifts = rec.env['hr.temporary.shift'].search([
                ('employee_id', '=', employee.id),
                ('date', '>=', rec.date_from),
                ('date', '<=', date_to_end.date())  # Convert back to date for comparison
            ])

            # Index temp shifts by date
            temp_shifts_by_date = {ts.date: ts for ts in all_temp_shifts}

            # Cache for policy lookups
            policy_cache = {}

            def get_policy(policy_type, calendar_id, occurrence, minutes=None):
                """Cached policy lookup"""
                if policy_type == 'late':
                    key = (policy_type, calendar_id, occurrence, minutes)
                else:
                    key = (policy_type, calendar_id, occurrence)

                if key not in policy_cache:
                    domain = [
                        ('policy_type', '=', policy_type),
                        ('working_hours_id', '=', calendar_id),
                        ('occurrence_number', '=', occurrence),
                        ('company_id', '=', employee.company_id.id)
                    ]
                    if minutes is not None:
                        domain += [
                            ('minimum_minutes', '<=', minutes),
                            ('maximum_minutes', '>=', minutes)
                        ]
                    policy_cache[key] = rec.env['hr.attendance.policy.details'].search(domain, limit=1)
                return policy_cache[key]

            current_date = rec.date_from

            while current_date <= rec.date_to:
                day_name = current_date.strftime('%A')  # Get day name
                dt_start = datetime.combine(current_date, time.min)
                dt_end = datetime.combine(current_date, time.max)

                # LTR marker for proper table alignment
                ltr_marker = "\u200E"

                # 1. Determine expected hours for the day
                # Check for temporary shift first
                temp_shift = temp_shifts_by_date.get(current_date)

                if temp_shift:
                    expected_hours = temp_shift.expected_end - temp_shift.expected_start
                elif calendar.use_fixed_work_hours and calendar.fixed_work_hours > 0:
                    # Only use fixed work hours if explicitly enabled and set
                    expected_hours = calendar.fixed_work_hours
                elif calendar.is_multi_week:
                    # Multi-week calendar: Always use week-specific calendar
                    attendances_for_day = attendances_by_date.get(current_date)

                    if attendances_for_day and len(attendances_for_day) > 0 and attendances_for_day[0].calendar_id:
                        week_calendar = attendances_for_day[0].calendar_id
                        expected_hours = week_calendar.get_work_hours_count(
                            dt_start, dt_end, compute_leaves=False
                        )
                    else:
                        expected_hours = calendar.get_work_hours_count(
                            dt_start, dt_end, compute_leaves=False
                        )
                else:
                    # Use standard calendar working hours (attendance lines)
                    expected_hours = calendar.get_work_hours_count(
                        dt_start, dt_end, compute_leaves=False
                    )

                if expected_hours <= 0:
                    log_lines.append(f"{ltr_marker}||{str(current_date)}||{'-'}||{'-'}||{'-'}||{'-'}||{'-'}||{'-'}||{'عطلة'}")
                    current_date += timedelta(days=1)
                    continue

                # 2. Check Leaves & Excuses FIRST - use interval-based lookup
                # Find approved leaves for current date from intervals
                leaves = [leave for start, end, leave in leaves_intervals if start <= current_date <= end]

                # Find pending leaves for current date from intervals
                pending_leaves_current = [leave for start, end, leave in pending_leaves_intervals if start <= current_date <= end]

                # Check preloaded attendances for this day
                attendances_for_day = attendances_by_date.get(current_date, [])
                has_attendances = bool(attendances_for_day)

                is_excused = False
                has_day_mission = False
                permission_hours = 0.0
                excuse_reason = ""
                excused_hours = 0.0
                day_leave_counted = False  # Track if we already counted a full/half day leave
                day_time_off_label = "-"   # Label for time off column in summary row

                # Track permission leaves with their time windows for smart overlap
                permission_leaves = []

                for leave in leaves:
                    is_mission_type = self._is_mission(leave)

                    if leave.holiday_status_id and leave.holiday_status_id.is_late_excuse:
                        is_excused = True
                        # Count late excuse as leave days
                        if not day_leave_counted:
                            if leave.leave_type_request_unit == 'day':
                                leave_counter += 1.0
                                day_leave_counted = True
                            elif leave.leave_type_request_unit == 'half_day' and leave.request_unit_half:
                                leave_counter += 0.5
                                day_leave_counted = True
                        excuse_reason = f"إجازة معفية: {leave.holiday_status_id.name}"
                        day_time_off_label = f"{leave.holiday_status_id.name}"
                        break
                    if leave.leave_type_request_unit == 'day' or (leave.holiday_status_id and leave.holiday_status_id.time_type == 'other' and not leave.request_unit_half and not leave.request_unit_hours):
                        if is_mission_type and has_attendances:
                            # Day mission WITH attendance punches:
                            # Do NOT skip attendance; determine and display actual worked hours
                            has_day_mission = True
                            if not day_leave_counted:
                                leave_counter += 1.0
                                day_leave_counted = True
                            if day_time_off_label == "-":
                                day_time_off_label = f"{leave.holiday_status_id.name}"
                            else:
                                day_time_off_label += f" + {leave.holiday_status_id.name}"
                            # Don't break - allow processing hour permissions on same day
                        else:
                            # Regular full-day leave OR day mission with NO attendance punches
                            is_excused = True
                            if not day_leave_counted:
                                leave_counter += 1.0
                                day_leave_counted = True
                            if is_mission_type:
                                excuse_reason = f"مأمورية كاملة: {leave.holiday_status_id.name}"
                            else:
                                excuse_reason = f"إجازة كاملة: {leave.holiday_status_id.name}"
                            day_time_off_label = f"{leave.holiday_status_id.name}"
                            break
                    elif leave.leave_type_request_unit == 'half_day' or leave.request_unit_half:
                        if leave.request_unit_half:
                            # Half day: excuse 50% of expected hours
                            excused_hours += expected_hours * 0.5
                            if not day_leave_counted:
                                leave_counter += 0.5
                                day_leave_counted = True
                            excuse_reason = f"نصف يوم: {leave.holiday_status_id.name}"
                            day_time_off_label = f"{leave.holiday_status_id.name} (نصف يوم)"
                            # Don't break - allow processing hour leaves on same day
                        else:
                            # Half day type but request_unit_half not set - treat as full day
                            if is_mission_type and has_attendances:
                                has_day_mission = True
                                if not day_leave_counted:
                                    leave_counter += 1.0
                                    day_leave_counted = True
                                if day_time_off_label == "-":
                                    day_time_off_label = f"{leave.holiday_status_id.name}"
                                else:
                                    day_time_off_label += f" + {leave.holiday_status_id.name}"
                            else:
                                is_excused = True
                                if not day_leave_counted:
                                    leave_counter += 1.0
                                    day_leave_counted = True
                                if is_mission_type:
                                    excuse_reason = f"مأمورية كاملة: {leave.holiday_status_id.name}"
                                else:
                                    excuse_reason = f"إجازة كاملة: {leave.holiday_status_id.name}"
                                day_time_off_label = f"{leave.holiday_status_id.name}"
                                break
                    elif leave.leave_type_request_unit == 'hour' or leave.request_unit_hours:
                        permission_hours += leave.number_of_hours_display
                        # Track permission with time window for smart overlap
                        if leave.date_from and leave.date_to:
                            permission_leaves.append({
                                'date_from': leave.date_from,
                                'date_to': leave.date_to,
                                'hours': leave.number_of_hours_display
                            })
                        hour_label = self._format_leave_hours(leave.number_of_hours_display)
                        excuse_reason = f"إذن ساعات: {hour_label}"
                        if day_time_off_label == "-":
                            day_time_off_label = f"{leave.holiday_status_id.name} ({hour_label})"
                        else:
                            day_time_off_label += f" + {leave.holiday_status_id.name} ({hour_label})"
                        # Don't break - hour permissions can combine with other leaves

                # Count pending leaves separately (they don't excuse absence but are tracked)
                for leave in pending_leaves_current:
                    if leave.leave_type_request_unit == 'day':
                        pending_leave_counter += 1.0
                    elif leave.leave_type_request_unit == 'half_day' or leave.request_unit_half:
                        pending_leave_counter += 0.5
                    elif leave.leave_type_request_unit == 'hour' or leave.request_unit_hours:
                        # Convert hours to days for pending count
                        pending_leave_counter += leave.number_of_hours_display / 8.0  # Assuming 8 hours = 1 day

                if is_excused:
                    excuse_detail = excuse_reason.split(':', 1)[1].strip() if ':' in excuse_reason else excuse_reason
                    if 'مأمورية' in excuse_reason or 'مامورية' in excuse_reason:
                        log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{'-'}||{'-'}||{'-'}||{'مأمورية'}||{f'{excuse_detail}'}||{'-'}")
                    else:
                        log_lines.append(f"{ltr_marker}||{str(current_date)}||{'-'}||{'-'}||{'-'}||{'-'}||{f'time off'}||{f'{excuse_detail}'}||{'-'}")
                    current_date += timedelta(days=1)
                    continue

                # 3. Check Attendance - use pre-loaded data
                attendances = attendances_by_date.get(current_date, [])

                # Calculate actual worked hours from valid attendance records with interval merging
                intervals = []
                valid_attendances_count = 0
                for att in attendances:
                    if att.check_in and att.check_out and att.check_out > att.check_in:
                        sf = att.check_in.hour + att.check_in.minute / 60.0 + att.check_in.second / 3600.0
                        ef = att.check_out.hour + att.check_out.minute / 60.0 + att.check_out.second / 3600.0
                        if ef > sf and (ef - sf) >= (10.0 / 60.0):
                            intervals.append((sf, ef))
                            valid_attendances_count += 1
                # Simple interval merge for legacy fallback
                def _merge_int(ints):
                    if not ints: return []
                    ints = sorted(ints)
                    merged = [list(ints[0])]
                    for s, e in ints[1:]:
                        if s <= merged[-1][1]:
                            merged[-1][1] = max(merged[-1][1], e)
                        else:
                            merged.append([s, e])
                    return [(s, e) for s, e in merged]
                merged_intervals = _merge_int(intervals)
                valid_worked_hours = sum(e - s for s, e in merged_intervals)

                first_att = attendances[0] if attendances else None
                policy_calendar_id = first_att.calendar_id.id if first_att and first_att.calendar_id else calendar.id

                # 4. Calculate Result - ENSURE ALL DAYS ARE LOGGED
                # Adjust expected hours for half-day leaves
                adjusted_expected_hours = expected_hours - excused_hours

                # Scenario A: No punches at all -> ABSENCE
                if not attendances:
                    if adjusted_expected_hours > 0:
                        if calendar.use_fixed_work_hours and calendar.fixed_work_hours > 0:
                            absence_occurrence = int(absence_counter) + 1
                            policy_detail = get_policy('fixed_hours_absence', policy_calendar_id, absence_occurrence)

                            if policy_detail:
                                absence_counter += 1.0
                                penalty_counter += policy_detail.penalty_days
                                log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{'00:00'}||{'-'}||{'-'}||{f'غياب: جزاء {policy_detail.penalty_days} يوم'}||{day_time_off_label}||{policy_detail.name or 'غياب'}")
                            else:
                                absence_counter += 1.0
                                log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{'00:00'}||{'-'}||{'-'}||{'غياب (لا توجد سياسة)'}||{day_time_off_label}||{'-'}")
                        else:
                            absence_counter += 1.0
                            log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{'00:00'}||{'-'}||{'-'}||{'غياب كامل'}||{day_time_off_label}||{'-'}")
                    current_date += timedelta(days=1)
                    continue

                # Scenario B: Punches exist, but NO valid attendance session (e.g. single punch / duplicate < 10min / missing checkout) -> FORGOT FINGERPRINT
                if valid_attendances_count == 0:
                    if has_day_mission:
                        # Day mission excuses the punch anomaly
                        log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{'-'}||{'-'}||{'-'}||{'مأمورية (بصمة غير مكتملة)'}||{day_time_off_label}||{'-'}")
                        current_date += timedelta(days=1)
                        continue
                    forgot_fp_occurrence_count += 1
                    forgot_attendance_count += 1
                    policy_detail = get_policy('forgot_fp', policy_calendar_id, forgot_fp_occurrence_count)

                    if policy_detail:
                        penalty_counter += policy_detail.penalty_days
                        log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{'-'}||{'-'}||{'-'}||{f'نسيان بصمة: جزاء {policy_detail.penalty_days} يوم'}||{day_time_off_label}||{policy_detail.name or 'نسيان بصمة'}")
                    else:
                        log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{'-'}||{'-'}||{'-'}||{'نسيان بصمة (لا توجد سياسة)'}||{day_time_off_label}||{'-'}")
                    current_date += timedelta(days=1)
                    continue

                # Scenario C: Valid attendance exists -> PRESENCE, LATENESS, or EXTRA HOURS
                actual_worked_hours = valid_worked_hours

                if has_day_mission:
                    # Full day mission with attendance punches:
                    # Mission excuses any deficit/lateness - employee was on official mission for the rest of the shift
                    extra_minutes = 0
                    if actual_worked_hours > expected_hours:
                        extra_hours = actual_worked_hours - expected_hours
                        extra_minutes = int(extra_hours * 60)
                        total_extra_minutes += extra_minutes
                    log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{rec._float_to_hours(actual_worked_hours)}||{'0'}||{extra_minutes}||{'حاضر (مأمورية)'}||{day_time_off_label}||{'-'}")
                    current_date += timedelta(days=1)
                    continue

                raw_deficit = adjusted_expected_hours - actual_worked_hours

                # Floor deficit to 1 minute (60 seconds) precision
                deficit_minutes = int(raw_deficit * 60)  # floor to whole minutes

                if deficit_minutes >= 1:
                    # LATENESS CASE - only if deficit is 1+ minutes
                    # SMART OVERLAP: Permission hours only offset lateness if employee
                    # arrived before or during the permission end time
                    offsettable_permission_hours = 0.0
                    if attendances and permission_leaves:
                        for perm in permission_leaves:
                            offsettable_permission_hours += perm['hours']

                    # Apply permission offset (capped at deficit_minutes to avoid negative lateness)
                    effective_deficit_minutes = max(deficit_minutes - int(offsettable_permission_hours * 60), 0)

                    if effective_deficit_minutes > 0:
                        late_occurrence_count += 1
                        net_lateness_minutes_list.append(f"{effective_deficit_minutes}")
                        total_lateness += effective_deficit_minutes

                        policy_detail = get_policy('late', policy_calendar_id, late_occurrence_count, effective_deficit_minutes)

                        if policy_detail:
                            penalty_counter += policy_detail.penalty_days
                            log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{rec._float_to_hours(actual_worked_hours)}||{effective_deficit_minutes}||{'0'}||{f'تأخير: جزاء {policy_detail.penalty_days} يوم'}||{day_time_off_label}||{policy_detail.name or 'تأخير'}")
                        else:
                            log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{rec._float_to_hours(actual_worked_hours)}||{effective_deficit_minutes}||{'0'}||{'تأخير (لا توجد سياسة)'}||{day_time_off_label}||{'-'}")
                    else:
                        # Fully covered by permission - log as present
                        extra_minutes = 0
                        if actual_worked_hours > expected_hours:
                            extra_hours = actual_worked_hours - expected_hours
                            extra_minutes = int(extra_hours * 60)
                            total_extra_minutes += extra_minutes
                        log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{rec._float_to_hours(actual_worked_hours)}||{'0'}||{extra_minutes}||{'تأخير مغطى بالإذن'}||{day_time_off_label}||{'-'}")
                else:
                    # PRESENCE or EXTRA HOURS CASE
                    # Extra hours are only granted for physical work exceeding the FULL standard expected shift
                    if actual_worked_hours > expected_hours:
                        extra_hours = actual_worked_hours - expected_hours
                        extra_minutes = int(extra_hours * 60)
                        total_extra_minutes += extra_minutes
                        log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{rec._float_to_hours(actual_worked_hours)}||{'0'}||{extra_minutes}||{'حاضر'}||{day_time_off_label}||{'-'}")
                    else:
                        log_lines.append(f"{ltr_marker}||{str(current_date)}||{rec._float_to_hours(expected_hours)}||{rec._float_to_hours(actual_worked_hours)}||{'0'}||{'0'}||{'حاضر'}||{day_time_off_label}||{'-'}")

                current_date += timedelta(days=1)

            # Footer summary - use local variables (not rec.* which are stale)
            log_lines.append("=" * 100)
            log_lines.append(f"====الملخص====|الغياب={absence_counter}|الإجازات={leave_counter}|الإجازات المعلقة={pending_leave_counter}|الجزاءات={penalty_counter} يوم|التأخير={total_lateness:.0f} دقيقة|الإضافي={total_extra_minutes:.0f} دقيقة")
            log_lines.append("=" * 100)

            # Finalize
            rec.total_absence_days = absence_counter
            rec.total_penalty_days = penalty_counter
            rec.total_leave_days = leave_counter
            rec.pending_leave_days = pending_leave_counter
            rec.forgot_attendance_count = forgot_attendance_count
            rec.calculation_log = "\n".join(log_lines)
            rec.net_lateness_minutes_list = ','.join(net_lateness_minutes_list)
            # Convert minutes to hours (decimal) for float_time widget
            rec.total_lateness_hours = total_lateness / 60.0
            rec.total_extra_hours = total_extra_minutes / 60.0
            rec.last_computed_date = fields.Datetime.now()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def action_recalculate(self):
        """Single recalculation entry point.

        Rows bound to a period use the deterministic period engine;
        legacy rows (period_id = NULL) keep the legacy engine unchanged.
        """
        Engine = self.env['hr.payroll.attendance.engine']
        for rec in self:
            if rec.period_id:
                if rec.period_id.state in ('approved', 'closed'):
                    raise UserError(_(
                        'لا يمكن إعادة حساب سجل في فترة %s لأنها %s — أعد فتح الفترة أولاً')
                        % (rec.period_id.period_key,
                           _('معتمدة') if rec.period_id.state == 'approved' else _('مغلقة')))
                vals = Engine._compute_summary_vals(rec)
                Engine._apply_vals(rec, vals)
            else:
                rec._compute_payroll_inputs()
        self.invalidate_recordset(['is_computed'])
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }
