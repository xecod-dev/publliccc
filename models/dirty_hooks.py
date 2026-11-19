import logging

from odoo import models, api

_logger = logging.getLogger(__name__)

LEAVE_DIRTY_FIELDS = {
    'employee_id', 'holiday_status_id', 'state', 'date_from', 'date_to',
    'request_date_from', 'request_date_to', 'leave_type_request_unit',
    'request_unit_half', 'request_unit_hours', 'number_of_hours_display',
}
LEAVE_TYPE_DIRTY_FIELDS = {
    'is_mission', 'is_permission', 'is_late_excuse', 'is_early_leave_excuse',
    'classification_reviewed', 'request_unit', 'time_type',
}
CONTRACT_DIRTY_FIELDS = {
    'employee_id', 'resource_calendar_id', 'state', 'date_start', 'date_end',
}
CALENDAR_DIRTY_FIELDS = {
    'use_fixed_work_hours', 'fixed_work_hours', 'tz', 'is_multi_week',
    'attendance_ids', 'hours_per_day', 'flexible_hours', 'fulltime_required_hours',
}


def _safe_mark(env, employee_ids):
    """Never blocks the triggering user operation; failures are logged."""
    if not employee_ids:
        return
    try:
        env['hr.payroll.attendance.engine'].sudo().mark_dirty(list(employee_ids))
    except Exception:
        _logger.exception(
            'Failed to mark attendance summaries dirty for employees %s',
            employee_ids)


def _calendar_employee_ids(env, calendar):
    if not calendar:
        return []
    env.cr.execute(
        "SELECT id FROM hr_employee WHERE resource_calendar_id = %s",
        [calendar.id])
    return [row[0] for row in env.cr.fetchall()]


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        _safe_mark(self.env, records.mapped('employee_id.id'))
        return records

    def write(self, vals):
        employees = set()
        if LEAVE_DIRTY_FIELDS & set(vals.keys()):
            employees.update(self.mapped('employee_id.id'))
            if 'employee_id' in vals:
                for rec in self:
                    if rec.employee_id.id != vals['employee_id']:
                        employees.add(rec.employee_id.id)
        res = super().write(vals)
        if employees:
            _safe_mark(self.env, employees)
        return res

    def unlink(self):
        employees = set(self.mapped('employee_id.id'))
        res = super().unlink()
        if employees:
            _safe_mark(self.env, employees)
        return res


class HrLeaveType(models.Model):
    _inherit = 'hr.leave.type'

    def write(self, vals):
        res = super().write(vals)
        if LEAVE_TYPE_DIRTY_FIELDS & set(vals.keys()):
            for ltype in self:
                self.env.cr.execute(
                    """
                    SELECT DISTINCT l.employee_id
                    FROM hr_leave l
                    JOIN hr_employee e ON e.id = l.employee_id
                    JOIN hr_payroll_attendance_period p
                         ON p.company_id = e.company_id
                        AND p.state IN ('draft', 'in_progress',
                                        'pending_approval', 'approved')
                    WHERE l.holiday_status_id = %s
                      AND l.request_date_from <= p.date_to
                      AND l.request_date_to >= p.date_from
                    """, [ltype.id])
                _safe_mark(self.env, [row[0] for row in self.env.cr.fetchall()])
        return res


class HrContract(models.Model):
    _inherit = 'hr.contract'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        _safe_mark(self.env, records.mapped('employee_id.id'))
        return records

    def write(self, vals):
        employees = set()
        if CONTRACT_DIRTY_FIELDS & set(vals.keys()):
            employees.update(self.mapped('employee_id.id'))
            if 'employee_id' in vals:
                for rec in self:
                    if rec.employee_id.id != vals['employee_id']:
                        employees.add(rec.employee_id.id)
        res = super().write(vals)
        if employees:
            _safe_mark(self.env, employees)
        return res

    def unlink(self):
        employees = set(self.mapped('employee_id.id'))
        res = super().unlink()
        if employees:
            _safe_mark(self.env, employees)
        return res


class ResourceCalendar(models.Model):
    _inherit = 'resource.calendar'

    def _dirty_employees_of_calendar(self):
        return _calendar_employee_ids(self.env, self)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # newly created calendars have no employees bound yet
        return records

    def write(self, vals):
        employees = set()
        if CALENDAR_DIRTY_FIELDS & set(vals.keys()):
            for calendar in self:
                employees.update(_calendar_employee_ids(self.env, calendar))
        res = super().write(vals)
        if employees:
            _safe_mark(self.env, employees)
        return res

    def unlink(self):
        employees = set()
        for calendar in self:
            employees.update(_calendar_employee_ids(self.env, calendar))
        res = super().unlink()
        if employees:
            _safe_mark(self.env, employees)
        return res


class ResourceCalendarAttendance(models.Model):
    _inherit = 'resource.calendar.attendance'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        calendars = set(records.mapped('calendar_id.id'))
        if calendars:
            for calendar in self.env['resource.calendar'].browse(list(calendars)):
                _safe_mark(self.env, _calendar_employee_ids(self.env, calendar))
        return records

    def write(self, vals):
        calendars = set(self.mapped('calendar_id.id'))
        res = super().write(vals)
        if calendars:
            for calendar in self.env['resource.calendar'].browse(list(calendars)):
                _safe_mark(self.env, _calendar_employee_ids(self.env, calendar))
        return res

    def unlink(self):
        calendars = set(self.mapped('calendar_id.id'))
        res = super().unlink()
        if calendars:
            for calendar in self.env['resource.calendar'].browse(list(calendars)):
                _safe_mark(self.env, _calendar_employee_ids(self.env, calendar))
        return res


class ResourceCalendarLeaves(models.Model):
    _inherit = 'resource.calendar.leaves'

    def _affected_employee_ids(self, records):
        env = self.env
        employee_ids = set()
        for rec in records:
            if rec.resource_id:
                emp = env['hr.employee'].search(
                    [('resource_id', '=', rec.resource_id.id)], limit=1)
                if emp:
                    employee_ids.add(emp.id)
            elif rec.calendar_id:
                env.cr.execute(
                    "SELECT id FROM hr_employee WHERE resource_calendar_id = %s",
                    [rec.calendar_id.id])
                employee_ids.update(row[0] for row in env.cr.fetchall())
            else:
                env.cr.execute(
                    "SELECT id FROM hr_employee WHERE active = TRUE "
                    "AND company_id IN %s",
                    [tuple(env.companies.ids) or (0,)])
                employee_ids.update(row[0] for row in env.cr.fetchall())
        return list(employee_ids)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        _safe_mark(self.env, self._affected_employee_ids(records))
        return records

    def write(self, vals):
        affected = self._affected_employee_ids(self)
        res = super().write(vals)
        _safe_mark(self.env, affected)
        return res

    def unlink(self):
        affected = self._affected_employee_ids(self)
        res = super().unlink()
        if affected:
            _safe_mark(self.env, affected)
        return res
