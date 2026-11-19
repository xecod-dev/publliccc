from odoo import http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal

# Rows the employee portal may ever show:
# - legacy rows (no period binding) that have actually been computed,
# - period rows whose period is pending approval / approved / closed
#   AND which are successfully computed.
# Uncomputed / computing / stale / error rows are never exposed, and
# error details are never rendered.
def _portal_domain(user):
    return [
        '&',
        ('employee_id.user_id', '=', user.id),
        ('calc_state', '=', 'computed'),
        '|',
        ('period_id', '=', False),
        ('period_id.state', 'in', ['in_progress', 'pending_approval', 'approved', 'closed']),
    ]


class PortalHomeAttendance(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)

        if "attendance_count" in counters:
            count = request.env['hr.payroll.attendance.summary'].sudo().search_count(
                _portal_domain(request.env.user))
            values["attendance_count"] = count if count > 0 else "0"

        return values


class PortalAttendance(http.Controller):

    @http.route('/my/attendance-summary', type='http', auth="user", website=True)
    def portal_attendance_report(self, **kwargs):

        user = request.env.user

        Attendance = request.env['hr.payroll.attendance.summary'].sudo()

        domain = _portal_domain(user)

        records = Attendance.search(domain, order="date_from desc")

        attendance_count = Attendance.search_count(domain)

        values = {
            'records': records,
            'attendance_count': attendance_count,
        }

        return request.render(
            'hr_payroll_attendance.portal_attendance_summary',
            values
        )

    @http.route(['/my/attendance-summary/<int:rec_id>'], type='http', auth="user", website=True)
    def portal_attendance_detail(self, rec_id, **kwargs):
        user = request.env.user
        Attendance = request.env['hr.payroll.attendance.summary'].sudo()
        rec = Attendance.browse(rec_id).exists()

        if not rec or rec.employee_id.user_id.id != user.id \
                or not Attendance.search_count([('id', '=', rec.id)] + _portal_domain(user)):
            return request.redirect('/my')

        preliminary = bool(rec.period_id) and rec.period_id.state == 'pending_approval'

        log = rec.calculation_log and rec.calculation_log.strip() or ''

        values = {
            'rec': rec,
            'log': log,
            'preliminary': preliminary,
        }

        return request.render('hr_payroll_attendance.portal_attendance_detail', values)
