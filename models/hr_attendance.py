from odoo import models, fields, api

_DIRTY_FIELDS = {'check_in', 'check_out', 'employee_id'}


def _mark_summaries_dirty(env, employee_ids):
    if not employee_ids:
        return
    try:
        env['hr.payroll.attendance.engine'].sudo().mark_dirty(list(employee_ids))
    except Exception:
        env['ir.logging'].sudo().create({
            'name': 'hr_payroll_attendance.dirty',
            'type': 'server',
            'dbname': env.cr.dbname,
            'level': 'ERROR',
            'message': 'Failed to mark attendance summaries dirty',
            'detail': 'employee_ids=%s' % list(employee_ids),
        })


class HrAttendance(models.Model):
    _inherit = "hr.attendance"

    attendance_status = fields.Selection([
        ('normal', 'Normal'),
        ('missing_check_out', 'Missing Check Out'),
        ('missing_check_in', 'Missing Check In'),
        ('same_time', 'Same Check In/Out (Forgot FP)'),
    ], string="Status", compute='_compute_attendance_status', store=True)
    calendar_id = fields.Many2one(
        'resource.calendar',
        string='Working Hours',
        compute='_compute_calendar_id',
        store=True,
        readonly=True,
        help='The working hours calendar that was active at check-in time. '
             'For multi-week calendars, this is the specific week\'s calendar.'
    )
    week_number = fields.Integer(
        string='Week Number',
        compute='_compute_calendar_id',
        store=True,
        readonly=True,
        help='For multi-week calendars, the week number (1, 2, 3, ...) that was active at check-in.'
    )

    @api.depends('check_in', 'check_out')
    def _compute_attendance_status(self):
        for rec in self:
            if not rec.check_out:
                rec.attendance_status = 'missing_check_out'
            elif rec.check_in and rec.check_out and rec.check_in == rec.check_out:
                rec.attendance_status = 'same_time'
            elif not rec.check_in:
                rec.attendance_status = 'missing_check_in'
            else:
                rec.attendance_status = 'normal'

    @api.depends('employee_id', 'check_in')
    def _compute_calendar_id(self):
        """Compute the calendar and week number based on check-in date."""
        for attendance in self:
            if attendance.employee_id and attendance.check_in:
                calendar = attendance.employee_id.resource_calendar_id
                if calendar and calendar.is_multi_week:
                    # Get the specific week's calendar for this check-in date
                    check_in_date = fields.Datetime.from_string(attendance.check_in).date()
                    specific_calendar = calendar._get_multi_week_calendar(check_in_date)
                    if specific_calendar:
                        attendance.calendar_id = specific_calendar
                        attendance.week_number = specific_calendar.week_number
                    else:
                        attendance.calendar_id = calendar
                        attendance.week_number = 0
                else:
                    attendance.calendar_id = calendar
                    attendance.week_number = 0
            else:
                attendance.calendar_id = False
                attendance.week_number = 0

    # ------------------------------------------------------------------
    # Dirty marking -> attendance summaries of open periods
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        _mark_summaries_dirty(self.env, records.mapped('employee_id.id'))
        return records

    def write(self, vals):
        employees = set()
        if _DIRTY_FIELDS & set(vals.keys()):
            employees.update(self.mapped('employee_id.id'))
            if 'employee_id' in vals:
                for rec in self:
                    if rec.employee_id.id != vals['employee_id']:
                        employees.add(rec.employee_id.id)
        res = super().write(vals)
        if employees:
            _mark_summaries_dirty(self.env, employees)
        return res

    def unlink(self):
        employees = set(self.mapped('employee_id.id'))
        res = super().unlink()
        if employees:
            _mark_summaries_dirty(self.env, employees)
        return res
