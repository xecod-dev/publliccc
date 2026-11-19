from odoo import models, fields, api, _  # type: ignore


class HrPayrollAttendanceSummaryWizard(models.TransientModel):
    _name = 'hr.payroll.attendance.summary.wizard'
    _description = 'Generate Attendance Summary'

    company_id = fields.Many2one(
        'res.company', string='الشركة', required=True,
        default=lambda self: self.env.company)
    period_id = fields.Many2one(
        'hr.payroll.attendance.period', string='الفترة', required=True,
        domain="[('company_id', '=', company_id),"
              " ('state', 'in', ['draft', 'in_progress'])]")
    department_ids = fields.Many2many('hr.department', string='الإدارات')
    employee_ids = fields.Many2many('hr.employee', string='الموظفين')

    @api.onchange('company_id')
    def _onchange_company_id(self):
        self.period_id = False

    def _target_employee_ids(self):
        domain = [('company_id', '=', self.company_id.id), ('active', '=', True)]
        if self.employee_ids:
            domain.append(('id', 'in', self.employee_ids.ids))
        elif self.department_ids:
            domain.append(('department_id', 'in', self.department_ids.ids))
        return self.env['hr.employee'].search(domain).ids

    def action_generate(self):
        """Ensure placeholder summaries for the period, then start a
        manual calculation run (bounded; remaining work continues via the
        5-minute processor)."""
        self.ensure_one()
        period = self.period_id
        if not period:
            raise ValueError(_('يجب اختيار الفترة'))
        employee_ids = self._target_employee_ids()
        Engine = self.env['hr.payroll.attendance.engine']
        Engine.ensure_period_summaries(period, employee_ids=employee_ids)
        if employee_ids:
            Engine.mark_dirty(employee_ids, periods=period)
        run = self.env['hr.payroll.attendance.calc.run'].create({
            'period_id': period.id,
            'trigger': 'manual',
        })
        run.action_process()
        return {
            'type': 'ir.actions.act_window',
            'name': _('تشغيل الحساب'),
            'res_model': 'hr.payroll.attendance.calc.run',
            'res_id': run.id,
            'view_mode': 'form',
            'target': 'current',
        }


class HrPayrollAttendancePeriodReopenWizard(models.TransientModel):
    _name = 'hr.payroll.attendance.period.reopen.wizard'
    _description = 'Reopen Attendance Period'

    period_ids = fields.Many2many(
        'hr.payroll.attendance.period', string='الفترات',
        relation='hr_attendance_period_reopen_rel')
    reopen_reason = fields.Text(string='سبب إعادة الفتح', required=True)

    def action_confirm(self):
        self.ensure_one()
        if not self.env.user.has_group('hr.group_hr_manager'):
            raise ValueError(_('إعادة فتح الفترات متاح لمدير الموارد البشرية فقط'))
        periods = self.env['hr.payroll.attendance.period'].browse(
            self.period_ids.ids)
        periods._do_reopen(self.reopen_reason)
        return {'type': 'ir.actions.act_window_close'}
