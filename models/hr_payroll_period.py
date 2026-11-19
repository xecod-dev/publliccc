import logging
from datetime import date, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)

OPEN_STATES = ('draft', 'in_progress', 'pending_approval', 'approved')


def _default_period_dates(today=None):
    """Period covering the 26th of the previous month -> 25th of the current one.

    Returns (date_from, date_to, period_key).
    """
    today = today or date.today()
    if today.day >= 26:
        date_from = date(today.year, today.month, 26)
        if today.month == 12:
            nxt = date(today.year + 1, 1, 25)
            key = f"{today.year + 1}-01"
        else:
            nxt = date(today.year, today.month + 1, 25)
            key = f"{today.year}-{today.month + 1:02d}"
        date_to = nxt
    else:
        if today.month == 1:
            date_from = date(today.year - 1, 12, 26)
        else:
            date_from = date(today.year, today.month - 1, 26)
        date_to = date(today.year, today.month, 25)
        key = f"{date_to.year}-{date_to.month:02d}"
    return date_from, date_to, key


class HrPayrollAttendancePeriod(models.Model):
    _name = 'hr.payroll.attendance.period'
    _description = 'Attendance Payroll Period'
    _order = 'date_from desc, id desc'
    _rec_name = 'period_key'

    company_id = fields.Many2one(
        'res.company', string='الشركة', required=True,
        default=lambda self: self.env.company)
    period_key = fields.Char(
        string='مفتاح الفترة', required=True,
        help='مثال: 2026-09 لفترة 26/08 -> 25/09')
    name = fields.Char(
        string='الاسم', compute='_compute_name', store=True)
    date_from = fields.Date(string='من تاريخ', required=True)
    date_to = fields.Date(string='إلى تاريخ', required=True)
    state = fields.Selection([
        ('draft', 'مسودة'),
        ('in_progress', 'قيد التنفيذ'),
        ('pending_approval', 'بانتظار الاعتماد'),
        ('approved', 'معتمدة'),
        ('closed', 'مغلقة'),
        ('cancelled', 'ملغاة'),
    ], string='الحالة', default='draft', required=True)
    dirty_count = fields.Integer(
        string='سجلات بحاجة لإعادة حساب',
        compute='_compute_counts')
    uncomputed_count = fields.Integer(
        string='غير محسوبة',
        compute='_compute_counts')
    error_count = fields.Integer(
        string='سجلات بأخطاء',
        compute='_compute_counts')
    summary_count = fields.Integer(
        string='إجمالي السجلات',
        compute='_compute_counts')
    exception_count = fields.Integer(
        string='استثناءات تصنيف',
        compute='_compute_counts')
    approved_by_id = fields.Many2one(
        'res.users', string='اعتمدتها', readonly=True)
    approved_date = fields.Datetime(string='تاريخ الاعتماد', readonly=True)
    closed_by_id = fields.Many2one('res.users', string='أغلقها', readonly=True)
    closed_date = fields.Datetime(string='تاريخ الإغلاق', readonly=True)
    reopen_reason = fields.Text(
        string='سبب الإعادة للفتح', readonly=True, copy=False)
    run_ids = fields.One2many(
        'hr.payroll.attendance.calc.run', 'period_id', string='تشغيلات الحساب')

    _sql_constraints = [
        ('period_company_key_unique',
         'unique(company_id, period_key)',
         'مفتاح الفترة يجب أن يكون فريداً لكل شركة'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company_id = vals.get('company_id') or self.env.company.id
            date_from = vals.get('date_from')
            date_to = vals.get('date_to')
            state = vals.get('state', 'draft')
            if state != 'cancelled' and date_from and date_to:
                self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext('hr_payroll_period_company_%s'))", [company_id])
                overlapping = self.search([
                    ('company_id', '=', company_id),
                    ('state', '!=', 'cancelled'),
                    ('date_from', '<=', date_to),
                    ('date_to', '>=', date_from),
                ], limit=1)
                if overlapping:
                    raise ValidationError(_(
                        'تتعارض هذه الفترة مع الفترة القائمة %s (%s → %s) لنفس الشركة')
                        % (overlapping.period_key, overlapping.date_from, overlapping.date_to))
        return super().create(vals_list)

    def init(self):
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS
            hr_payroll_period_company_state_idx
            ON hr_payroll_attendance_period (company_id, state)
        """)
        # Add advisory lock or exclusion index support if supported, plus advisory locking on create/write
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS
            hr_payroll_period_overlap_idx
            ON hr_payroll_attendance_period (company_id, date_from, date_to)
            WHERE state != 'cancelled'
        """)

    @api.depends('period_key', 'company_id')
    def _compute_name(self):
        for period in self:
            period.name = f"{period.company_id.name or ''} — {period.period_key}"

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for period in self:
            if period.date_from and period.date_to and period.date_from > period.date_to:
                raise ValidationError(_('تاريخ البداية يجب أن يكون قبل تاريخ النهاية'))
            if period.date_from and period.date_to:
                span = (period.date_to - period.date_from).days + 1
                if span > 45:
                    raise ValidationError(_('مدة الفترة لا يمكن أن تتجاوز 45 يوماً'))

    @api.constrains('company_id', 'date_from', 'date_to', 'state')
    def _check_no_overlap(self):
        for period in self:
            if period.state == 'cancelled' or not (period.date_from and period.date_to):
                continue
            overlapping = self.search([
                ('id', '!=', period.id),
                ('company_id', '=', period.company_id.id),
                ('state', '!=', 'cancelled'),
                ('date_from', '<=', period.date_to),
                ('date_to', '>=', period.date_from),
            ], limit=1)
            if overlapping:
                raise ValidationError(_(
                    'تتعارض هذه الفترة مع الفترة %s (%s → %s) لنفس الشركة')
                    % (overlapping.period_key, overlapping.date_from, overlapping.date_to))

    def _compute_counts(self):
        Summary = self.env['hr.payroll.attendance.summary']
        for period in self:
            base = [('period_id', '=', period.id)]
            period.summary_count = Summary.search_count(base)
            period.dirty_count = Summary.search_count(base + [('dirty', '=', True)])
            period.uncomputed_count = Summary.search_count(
                base + [('calc_state', 'in', ('uncomputed', 'computing'))])
            period.error_count = Summary.search_count(
                base + [('calc_state', '=', 'error')])
            period.exception_count = Summary.search_count(
                base + [('classification_exception', '!=', False)])

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------

    def _assert_state(self, expected):
        for period in self:
            if period.state not in expected:
                raise UserError(_(
                    'حالة الفترة %s لا تسمح بهذا الإجراء (الحالة الحالية: %s)')
                    % (period.period_key, dict(self._fields['state']._description_selection(
                        self.env)).get(period.state, period.state)))

    def action_open_calculation(self):
        """draft -> in_progress; creates missing placeholders."""
        self._assert_state(['draft'])
        Engine = self.env['hr.payroll.attendance.engine']
        for period in self:
            Engine.ensure_period_summaries(period)
            period.state = 'in_progress'
        return True

    def action_generate_missing(self):
        self._assert_state(['draft', 'in_progress'])
        Engine = self.env['hr.payroll.attendance.engine']
        created = 0
        for period in self:
            created += Engine.ensure_period_summaries(period)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('تم إنشاء %s سجل جديد') % created,
                'type': 'info',
            },
        }

    def action_recalculate_dirty(self):
        self._assert_state(['draft', 'in_progress'])
        Run = self.env['hr.payroll.attendance.calc.run']
        runs = Run.create([
            {'period_id': period.id, 'trigger': 'manual'}
            for period in self
        ])
        for run in runs:
            run.action_process()
        return self._open_runs_action(runs)

    def action_recalculate_all(self):
        """Explicit HR-triggered full recalculation of the period."""
        self._assert_state(['draft', 'in_progress'])
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE hr_payroll_attendance_summary "
            "SET dirty = true, "
            "    calc_state = CASE WHEN calc_state = 'computed' THEN 'stale' "
            "                      ELSE calc_state END "
            "WHERE period_id IN %s",
            [tuple(self.ids)])
        self.env.invalidate_all()
        return self.action_recalculate_dirty()

    def action_request_approval(self):
        self._assert_state(['in_progress'])
        Summary = self.env['hr.payroll.attendance.summary']
        for period in self:
            base = [('period_id', '=', period.id)]
            if Summary.search_count(base + [('dirty', '=', True)]):
                raise UserError(_(
                    'لا يمكن طلب اعتماد الفترة %s: توجد سجلات بحاجة لإعادة حساب')
                    % period.period_key)
            if Summary.search_count(base + [('calc_state', 'in', ('uncomputed', 'computing'))]):
                raise UserError(_(
                    'لا يمكن طلب اعتماد الفترة %s: توجد سجلات لم تحسب بعد')
                    % period.period_key)
            if Summary.search_count(base + [('calc_state', '=', 'error')]):
                raise UserError(_(
                    'لا يمكن طلب اعتماد الفترة %s: توجد سجلات بأخطاء حساب') % period.period_key)
            exceptions = Summary.search(
                base + [('classification_exception', '!=', False)], limit=5)
            if exceptions:
                names = '، '.join(
                    s.employee_id.name or str(s.employee_id.id) for s in exceptions)
                raise UserError(_(
                    'لا يمكن طلب اعتماد الفترة %s: أنواع إجازات غير مصنفة للموظفين: %s')
                    % (period.period_key, names))
            period.state = 'pending_approval'
        return True

    def action_approve(self):
        if not self.env.user.has_group('hr.group_hr_manager'):
            raise UserError(_('اعتماد الفترات متاح لمدير الموارد البشرية فقط'))
        self._assert_state(['pending_approval'])
        for period in self:
            period.write({
                'state': 'approved',
                'approved_by_id': self.env.user.id,
                'approved_date': fields.Datetime.now(),
            })
        return True

    def action_close(self):
        if not self.env.user.has_group('hr.group_hr_manager'):
            raise UserError(_('إغلاق الفترات متاح لمدير الموارد البشرية فقط'))
        self._assert_state(['approved'])
        for period in self:
            period.write({
                'state': 'closed',
                'closed_by_id': self.env.user.id,
                'closed_date': fields.Datetime.now(),
            })
        return True

    def action_reopen(self):
        if not self.env.user.has_group('hr.group_hr_manager'):
            raise UserError(_('إعادة فتح الفترات متاح لمدير الموارد البشرية فقط'))
        self._assert_state(['approved', 'closed', 'cancelled'])
        return {
            'name': _('إعادة فتح الفترة'),
            'type': 'ir.actions.act_window',
            'res_model': 'hr.payroll.attendance.period.reopen.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_period_ids': self.ids},
        }

    def _do_reopen(self, reason):
        for period in self:
            period.write({'state': 'in_progress', 'reopen_reason': reason})
            self.env.flush_all()
            self.env.cr.execute(
                "UPDATE hr_payroll_attendance_summary "
                "SET dirty = true, calc_state = 'stale', "
                "    last_checked = NOW() AT TIME ZONE 'UTC' "
                "WHERE period_id = %s", [period.id])
        self.env.invalidate_all()
        self.env['hr.payroll.attendance.engine'].sudo()._cron_process_dirty()
        return True

    def action_cancel(self):
        self._assert_state(['draft', 'in_progress', 'pending_approval'])
        self.write({'state': 'cancelled'})
        return True

    def _open_runs_action(self, runs):
        return {
            'type': 'ir.actions.act_window',
            'name': _('تشغيلات الحساب'),
            'res_model': 'hr.payroll.attendance.calc.run',
            'view_mode': 'tree,form',
            'domain': [('id', 'in', runs.ids)],
        }

    def action_view_summaries(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('ملخصات الفترة %s') % self.period_key,
            'res_model': 'hr.payroll.attendance.summary',
            'view_mode': 'tree,form',
            'domain': [('period_id', '=', self.id)],
            'context': {'default_period_id': self.id},
        }

    # ------------------------------------------------------------------
    # Date edits (pre-approval only) + auto-generation cron
    # ------------------------------------------------------------------

    def write(self, vals):
        editable_states = ('draft', 'in_progress')
        res = super().write(vals)
        if 'date_from' in vals or 'date_to' in vals:
            for period in self:
                if period.state not in editable_states:
                    continue
                self.env.flush_all()
                self.env.cr.execute(
                    "UPDATE hr_payroll_attendance_summary "
                    "SET date_from = %s, date_to = %s, dirty = true, "
                    "    calc_state = CASE WHEN calc_state = 'computed' THEN 'stale' "
                    "                      ELSE calc_state END "
                    "WHERE period_id = %s",
                    [period.date_from, period.date_to, period.id])
            self.env.invalidate_all()
        return res

    @api.model
    def _cron_auto_generate(self):
        for company in self.env['res.company'].sudo().search([]):
            date_from, date_to, key = _default_period_dates()
            exists = self.sudo().search([
                ('company_id', '=', company.id),
                ('period_key', '=', key),
                ('state', '!=', 'cancelled'),
            ], limit=1)
            if not exists:
                self.sudo().create({
                    'company_id': company.id,
                    'period_key': key,
                    'date_from': date_from,
                    'date_to': date_to,
                })
                _logger.info(
                    'Auto-created attendance period %s for company %s',
                    key, company.name)
        return True
