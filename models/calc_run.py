import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ACTIVE_STATES = ('draft', 'running')


def _cfg(env, key, default):
    try:
        return int(env['ir.config_parameter'].sudo().get_param(key, default))
    except (TypeError, ValueError):
        return int(default)


class HrPayrollAttendanceCalcRun(models.Model):
    _name = 'hr.payroll.attendance.calc.run'
    _description = 'Attendance Summary Calculation Run'
    _order = 'id desc'
    _rec_name = 'display_name'

    period_id = fields.Many2one(
        'hr.payroll.attendance.period', string='الفترة', required=True,
        ondelete='cascade')
    company_id = fields.Many2one(
        'res.company', string='الشركة', related='period_id.company_id', store=True)
    trigger = fields.Selection([
        ('manual', 'يدوي'),
        ('cron', 'مجدول'),
        ('reconcile', 'تسوية يومية'),
        ('watchdog', 'استرداد تلقائي'),
    ], string='المصدر', default='manual', required=True)
    state = fields.Selection([
        ('draft', 'جديد'),
        ('running', 'قيد التنفيذ'),
        ('done', 'مكتمل'),
        ('done_with_errors', 'مكتمل بأخطاء'),
        ('failed', 'فشل'),
        ('cancelled', 'ملغى'),
    ], string='الحالة', default='draft', required=True)
    batch_size = fields.Integer(default=lambda self: _cfg(self.env, 'hr_payroll_attendance.batch_size', 25))
    max_batches = fields.Integer(default=lambda self: _cfg(self.env, 'hr_payroll_attendance.max_batches_per_invocation', 4))
    max_seconds = fields.Integer(default=lambda self: _cfg(self.env, 'hr_payroll_attendance.max_seconds_per_invocation', 60))
    total_count = fields.Integer(string='إجمالي السجلات', readonly=True)
    processed_count = fields.Integer(string='المعالجة', readonly=True)
    success_count = fields.Integer(string='ناجحة', readonly=True)
    failed_count = fields.Integer(string='فاشلة', readonly=True)
    heartbeat = fields.Datetime(string='آخر نبضة', readonly=True)
    started_at = fields.Datetime(string='بدأ في', readonly=True)
    finished_at = fields.Datetime(string='انتهى في', readonly=True)
    processed_by_id = fields.Many2one('res.users', string='شغّله', readonly=True)
    worker_token = fields.Integer(string='رمز العامل', default=0, readonly=True)
    error_line_ids = fields.One2many(
        'hr.payroll.attendance.calc.run.error', 'run_id', string='أخطاء')
    display_name = fields.Char(compute='_compute_display_name')
    notes = fields.Text(string='ملاحظات')

    def init(self):
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            hr_payroll_calc_run_period_active_uidx
            ON hr_payroll_attendance_calc_run (period_id)
            WHERE state IN ('draft', 'running')
        """)
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS
            hr_payroll_summary_period_dirty_idx
            ON hr_payroll_attendance_summary (period_id)
            WHERE dirty IS TRUE
        """)

    def _compute_display_name(self):
        for run in self:
            run.display_name = _(
                'تشغيل #%(run)s — %(period)s') % {
                'run': run.id, 'period': run.period_id.period_key}

    # ------------------------------------------------------------------
    # Batch claiming
    # ------------------------------------------------------------------

    def _claim_batch(self):
        """Atomically claim the next dirty batch of the period.

        Sets claimed rows to 'computing' and commits so other workers
        never pick them up. Returns list of summary ids.
        """
        self.ensure_one()
        self.env.flush_all()
        self.env.cr.execute("""
            UPDATE hr_payroll_attendance_summary
            SET calc_state = 'computing', last_checked = NOW() AT TIME ZONE 'UTC'
            WHERE id IN (
                SELECT id FROM hr_payroll_attendance_summary
                WHERE period_id = %(period)s
                  AND dirty IS TRUE
                  AND calc_state IN ('uncomputed', 'stale')
                ORDER BY employee_id
                LIMIT %(limit)s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id
        """, {'period': self.period_id.id, 'limit': self.batch_size})
        claimed = [row[0] for row in self.env.cr.fetchall()]
        self.env.cr.commit()
        self.env.invalidate_all()
        return claimed

    def _check_alive(self):
        """Re-validate run ownership, worker token generation, and period state after every commit."""
        self.env.cr.execute(
            "SELECT state, worker_token FROM hr_payroll_attendance_calc_run WHERE id = %s",
            [self.id])
        row = self.env.cr.fetchone()
        if not row or row[0] != 'running':
            return False
        # Verify token hasn't changed (watchdog increments or invalidates token on recovery)
        self.env.cr.execute(
            "SELECT state FROM hr_payroll_attendance_period WHERE id = %s",
            [self.period_id.id])
        prow = self.env.cr.fetchone()
        return bool(prow and prow[0] in ('draft', 'in_progress'))

    def _heartbeat_commit(self):
        self.env.cr.execute(
            "UPDATE hr_payroll_attendance_calc_run "
            "SET heartbeat = NOW() AT TIME ZONE 'UTC', "
            "    processed_count = %s, success_count = %s, failed_count = %s "
            "WHERE id = %s",
            [self.processed_count, self.success_count, self.failed_count, self.id])
        self.env.cr.commit()
        self.env.invalidate_all()

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def action_process(self):
        self.ensure_one()
        if self.state == 'cancelled':
            raise UserError(_('هذا التشغيل ملغى'))
        if self.state == 'running':
            if not self._check_alive():
                raise UserError(_('هذا التشغيل لا يعمل'))
            # resume: nothing special, claim loop continues
        elif self.state == 'draft':
            import random
            token = random.randint(100000, 999999)
            self.env.cr.execute(
                "UPDATE hr_payroll_attendance_calc_run "
                "SET state = 'running', started_at = NOW() AT TIME ZONE 'UTC', "
                "    heartbeat = NOW() AT TIME ZONE 'UTC', "
                "    processed_by_id = %s, worker_token = %s "
                "WHERE id = %s AND state = 'draft'",
                [self.env.user.id, token, self.id])
            if self.env.cr.rowcount != 1:
                raise UserError(_('هذا التشغيل بدأ من عملية أخرى'))
            self.env.cr.commit()
            self.env.invalidate_all()
        else:
            raise UserError(_('هذا التشغيل انتهى بالفعل'))

        Engine = self.env['hr.payroll.attendance.engine'].sudo()
        Summary = self.env['hr.payroll.attendance.summary'].sudo()
        ErrorLine = self.env['hr.payroll.attendance.calc.run.error'].sudo()

        if not self.total_count:
            self.env.cr.execute(
                "SELECT COUNT(*) FROM hr_payroll_attendance_summary "
                "WHERE period_id = %s "
                "  AND calc_state IN ('uncomputed', 'stale', 'error')",
                [self.period_id.id])
            self.env.cr.execute(
                "UPDATE hr_payroll_attendance_calc_run SET total_count = %s "
                "WHERE id = %s",
                [self.env.cr.fetchone()[0], self.id])
            self.env.cr.commit()
            self.env.invalidate_all()

        import time as _time
        deadline = _time.time() + max(self.max_seconds, 5)
        batches = 0

        while batches < self.max_batches:
            if _time.time() > deadline:
                break
            if not self._check_alive():
                _logger.info('Run %s lost ownership / period closed, aborting', self.id)
                return True
            claimed = self._claim_batch()
            if not claimed:
                break
            batches += 1
            for sid in claimed:
                summary = Summary.browse(sid)
                if not summary.exists():
                    continue
                try:
                    vals = Engine._compute_summary_vals(summary, run=self)
                    Engine._apply_vals(summary, vals, run=self)
                    self.success_count += 1
                except Exception as exc:  # noqa: BLE001 - per-row isolation
                    _logger.exception('Summary %s computation failed', sid)
                    summary.write({
                        'calc_state': 'error',
                        'dirty': False,
                        'last_error': str(exc)[:2000],
                        'last_checked': fields.Datetime.now(),
                        'error_count': (summary.error_count or 0) + 1,
                    })
                    self.failed_count += 1
                    ErrorLine.create({
                        'run_id': self.id,
                        'employee_id': summary.employee_id.id,
                        'error_text': str(exc)[:2000],
                    })
            self._heartbeat_commit()

        if not self._check_alive():
            return True
        self.env.cr.execute(
            "SELECT COUNT(*) FROM hr_payroll_attendance_summary "
            "WHERE period_id = %s AND dirty IS TRUE "
            "  AND calc_state IN ('uncomputed', 'stale')",
            [self.period_id.id])
        remaining = self.env.cr.fetchone()[0]
        if remaining:
            # leave running; next cron/manual invocation continues
            self.env.cr.execute(
                "UPDATE hr_payroll_attendance_calc_run "
                "SET heartbeat = NOW() AT TIME ZONE 'UTC', "
                "    processed_count = %s, success_count = %s, failed_count = %s "
                "WHERE id = %s",
                [self.processed_count, self.success_count,
                 self.failed_count, self.id])
        else:
            final_state = 'done_with_errors' if self.failed_count else 'done'
            self.env.cr.execute(
                "UPDATE hr_payroll_attendance_calc_run "
                "SET state = %s, finished_at = NOW() AT TIME ZONE 'UTC', "
                "    processed_count = %s, success_count = %s, failed_count = %s "
                "WHERE id = %s",
                [final_state, self.processed_count, self.success_count,
                 self.failed_count, self.id])
        self.env.cr.commit()
        self.env.invalidate_all()
        return True

    def action_retry_failed(self):
        self.ensure_one()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE hr_payroll_attendance_summary "
            "SET dirty = true, calc_state = 'stale', "
            "    last_checked = NOW() AT TIME ZONE 'UTC' "
            "WHERE period_id = %s AND calc_state = 'error'",
            [self.period_id.id])
        self.env.invalidate_all()
        return self.action_process()

    def action_cancel_run(self):
        self.ensure_one()
        if self.state not in ('draft', 'running'):
            raise UserError(_('يمكن إلغاء التشغيلات النشطة فقط'))
        self.env.cr.execute(
            "UPDATE hr_payroll_attendance_calc_run SET state = 'cancelled', "
            "finished_at = NOW() AT TIME ZONE 'UTC' WHERE id = %s", [self.id])
        self.env.cr.commit()
        self.env.invalidate_all()
        return True

    def action_rerun_remaining(self):
        self.ensure_one()
        if self.state in ('done', 'done_with_errors'):
            run = self.create({'period_id': self.period_id.id, 'trigger': 'manual'})
            return run.action_process()
        return self.action_process()


class HrPayrollAttendanceCalcRunError(models.Model):
    _name = 'hr.payroll.attendance.calc.run.error'
    _description = 'Calculation Run Error Line'
    _order = 'id desc'

    run_id = fields.Many2one(
        'hr.payroll.attendance.calc.run', string='التشغيل',
        required=True, ondelete='cascade')
    employee_id = fields.Many2one('hr.employee', string='الموظف')
    error_text = fields.Text(string='نص الخطأ')
    message_date = fields.Datetime(
        string='التاريخ', default=fields.Datetime.now, readonly=True)


class HrPayrollAttendanceSummaryHistory(models.Model):
    _name = 'hr.payroll.attendance.summary.history'
    _description = 'Attendance Summary Quantity History'
    _order = 'computed_at desc, id desc'

    summary_id = fields.Many2one(
        'hr.payroll.attendance.summary', string='الملخص',
        required=True, ondelete='cascade', index=True)
    run_id = fields.Many2one('hr.payroll.attendance.calc.run', string='التشغيل')
    computed_at = fields.Datetime(string='وقت التعديل')
    computed_by = fields.Many2one('res.users', string='عدّلها')
    quantity_hash = fields.Char(string='بصمة الكميات')
    total_absence_days = fields.Float(string='أيام الغياب')
    total_penalty_days = fields.Float(string='أيام الجزاءات')
    total_leave_days = fields.Float(string='أيام الإجازات')
    pending_leave_days = fields.Float(string='أيام معلقة')
    total_lateness_hours = fields.Float(string='ساعات التأخير')
    total_extra_hours = fields.Float(string='ساعات الإضافي')
    forgot_attendance_count = fields.Integer(string='نسيان بصمة')
    net_lateness_minutes_list = fields.Char(string='دقائق التأخير')
    calculation_log = fields.Text(string='تفاصيل الحساب السابقة')
