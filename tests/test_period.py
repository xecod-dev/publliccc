from odoo import fields
from odoo.exceptions import ValidationError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPeriod(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.ref('base.main_company')
        cls.manager = cls.env.ref('base.user_admin')
        cls.manager.groups_id |= cls.env.ref('hr.group_hr_manager')
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Test 8h Calendar',
            'tz': 'UTC',
            'attendance_ids': [(0, 0, {
                'name': 'Mon-Fri',
                'dayofweek': str(d),
                'hour_from': 8.0,
                'hour_to': 16.0,
            }) for d in range(5)],
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Test Employee',
            'company_id': cls.company.id,
            'resource_calendar_id': cls.calendar.id,
        })

    def _period(self, date_from='2026-10-26', date_to='2026-11-25',
                key='2026-11'):
        return self.env['hr.payroll.attendance.period'].create({
            'company_id': self.company.id,
            'period_key': key,
            'date_from': date_from,
            'date_to': date_to,
        })

    def test_default_period_dates(self):
        from odoo.addons.hr_payroll_attendance.models.hr_payroll_period import \
            _default_period_dates
        from datetime import date
        df, dt, key = _default_period_dates(date(2026, 11, 17))
        self.assertEqual((str(df), str(dt), key), ('2026-10-26', '2026-11-25', '2026-11'))
        df, dt, key = _default_period_dates(date(2026, 12, 30))
        self.assertEqual((str(df), str(dt), key), ('2026-12-26', '2027-01-25', '2027-01'))
        df, dt, key = _default_period_dates(date(2026, 1, 10))
        self.assertEqual((str(df), str(dt), key), ('2025-12-26', '2026-01-25', '2026-01'))

    def test_no_overlapping_periods(self):
        self._period()
        with self.assertRaises(ValidationError):
            self._period(date_from='2026-11-20', date_to='2026-12-10',
                         key='2026-12a')

    def test_no_more_than_45_days(self):
        with self.assertRaises(ValidationError):
            self._period(date_from='2026-01-01', date_to='2026-03-01',
                         key='2026-01x')

    def test_open_calculation_creates_placeholders(self):
        period = self._period()
        period.action_open_calculation()
        self.assertEqual(period.state, 'in_progress')
        summary = self.env['hr.payroll.attendance.summary'].search([
            ('period_id', '=', period.id),
            ('employee_id', '=', self.employee.id),
        ])
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary.calc_state, 'uncomputed')
        self.assertTrue(summary.dirty)

    def test_placeholder_uniqueness(self):
        period = self._period()
        period.action_open_calculation()
        # second run must be idempotent
        self.env['hr.payroll.attendance.engine'].ensure_period_summaries(period)
        count = self.env['hr.payroll.attendance.summary'].search_count([
            ('period_id', '=', period.id)])
        self.assertEqual(count, 1)

    def test_date_edit_cascades_and_dirties(self):
        period = self._period()
        period.action_open_calculation()
        summary = self.env['hr.payroll.attendance.summary'].search([
            ('period_id', '=', period.id),
            ('employee_id', '=', self.employee.id)])
        summary.write({'calc_state': 'computed', 'dirty': False})
        period.write({'date_from': '2026-10-27'})
        self.assertEqual(summary.date_from, fields.Date.to_date('2026-10-27'))
        self.assertEqual(summary.calc_state, 'stale')
        self.assertTrue(summary.dirty)

    def test_approval_blocked_while_dirty(self):
        period = self._period()
        period.action_open_calculation()
        with self.assertRaises(UserError):
            period.action_request_approval()

    def test_full_workflow_and_locks(self):
        period = self._period()
        period.action_open_calculation()
        self.env['hr.payroll.attendance.engine'].mark_dirty([self.employee.id])
        run = self.env['hr.payroll.attendance.calc.run'].create({
            'period_id': period.id, 'trigger': 'manual'})
        run.action_process()
        summary = self.env['hr.payroll.attendance.summary'].search([
            ('period_id', '=', period.id),
            ('employee_id', '=', self.employee.id)])
        self.assertEqual(summary.calc_state, 'computed')
        self.assertFalse(summary.dirty)
        period.action_request_approval()
        period.action_approve()
        self.assertEqual(period.state, 'approved')
        # approved period: recalculation blocked
        with self.assertRaises(UserError):
            summary.action_recalculate()
        period.action_close()
        self.assertEqual(period.state, 'closed')
        # reopen with reason
        period._do_reopen('test reopen reason')
        self.assertEqual(period.state, 'in_progress')
        self.assertTrue(summary.dirty)

    def test_auto_generate_creates_once(self):
        self.env['hr.payroll.attendance.period']._cron_auto_generate()
        before = self.env['hr.payroll.attendance.period'].search_count([])
        self.env['hr.payroll.attendance.period']._cron_auto_generate()
        after = self.env['hr.payroll.attendance.period'].search_count([])
        self.assertEqual(before, after)
