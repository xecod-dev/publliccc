from odoo import models, fields, api

_DIRTY_FIELDS = {'employee_id', 'date', 'expected_start', 'expected_end'}


class HrTemporaryShift(models.Model):
    _name = "hr.temporary.shift"
    _description = "Temporary Employee Shift Change"
    _rec_name = "employee_id"

    employee_id = fields.Many2one("hr.employee", string="Employee", required=True)
    date = fields.Date(string="Shift Date", required=True)
    expected_start = fields.Float(
        string="New Start Time", required=True, help="Format: 8.5 for 08:30"
    )
    expected_end = fields.Float(
        string="New End Time", required=True, help="Format: 17.5 for 17:30"
    )
    notes = fields.Text(string="Reason")

    # ------------------------------------------------------------------
    # Dirty marking -> attendance summaries of open periods
    # ------------------------------------------------------------------

    def _notify(self, records):
        employee_ids = list(records.mapped('employee_id.id'))
        if not employee_ids:
            return
        try:
            self.env['hr.payroll.attendance.engine'].sudo().mark_dirty(employee_ids)
        except Exception:
            self.env['ir.logging'].sudo().create({
                'name': 'hr_payroll_attendance.dirty',
                'type': 'server',
                'dbname': self.env.cr.dbname,
                'level': 'ERROR',
                'message': 'Failed to mark summaries dirty (temporary shift)',
                'detail': 'employee_ids=%s' % employee_ids,
            })

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self._notify(records)
        return records

    def write(self, vals):
        employees = set()
        if _DIRTY_FIELDS & set(vals.keys()):
            employees.update(self.mapped('employee_id.id'))
        res = super().write(vals)
        if employees:
            self._notify(self)
        return res

    def unlink(self):
        employee_ids = list(set(self.mapped('employee_id.id')))
        res = super().unlink()
        if employee_ids:
            try:
                self.env['hr.payroll.attendance.engine'].sudo().mark_dirty(employee_ids)
            except Exception:
                pass
        return res
