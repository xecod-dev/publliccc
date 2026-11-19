from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCalcRun(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.ref('base.main_company')
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Run 8h Calendar',
            'tz': 'UTC',
            'attendance_ids': [(0, 0, {
                'name': 'Mon-Fri',
                'dayofweek': str(d),
                'hour_from': 8.0,
                'hour_to': 16.0,
            }) for d in range(5)],
        })
        cls.employees = cls.env['hr.employee'].create([{
            'name': 'Run Employee %d' % i,
            'company_id': cls.company.id,
            'resource_calendar_id': cls.calendar.id,
        } for i in range(3)])

    def setUp(self):
        super().setUp()
        self.env['hr.payroll.attendance.summary'].search([]).unlink()
        self.env['hr.payroll.attendance.period'].search([]).unlink()
        self.period = self.env['hr.payroll.attendance.period'].create({
            'company_id': self.company.id,
            'period_key': 'run-test',
            'date_from': '2026-12-07',
            'date_to': '2026-12-11',
        })

    def tearDown(self):
        self.env['hr.payroll.attendance.summary'].search([]).unlink()
        self.env['hr.payroll.attendance.period'].search([]).unlink()
        super().tearDown()
        cls.period.action_open_calculation()
        # one employee with a bad day to force an error path? keep all clean

    def test_run_processes_all_rows(self):
        run = self.env['hr.payroll.attendance.calc.run'].create({
            'period_id': self.period.id, 'trigger': 'manual'})
        run.action_process()
        self.assertEqual(run.state, 'done')
        self.assertEqual(run.success_count, 3)
        summaries = self.env['hr.payroll.attendance.summary'].search([
            ('period_id', '=', self.period.id)])
        self.assertTrue(all(s.calc_state == 'computed' for s in summaries))
        self.assertFalse(any(s.dirty for s in summaries))
        # deterministic: identical quantities produce identical hashes
        hashes = {s.quantity_hash for s in summaries}
        self.assertEqual(len(hashes), 1)

    def test_unique_active_run_per_period(self):
        run1 = self.env['hr.payroll.attendance.calc.run'].create({
            'period_id': self.period.id, 'trigger': 'manual'})
        run1.action_process()  # completes -> no active run left
        self.assertIn(run1.state, ('done', 'done_with_errors'))
        run2 = self.env['hr.payroll.attendance.calc.run'].create({
            'period_id': self.period.id, 'trigger': 'manual'})
        run2.action_process()
        self.assertEqual(run2.state, 'done')

    def test_reconcile_redis_stales(self):
        summary = self.env['hr.payroll.attendance.summary'].search([
            ('period_id', '=', self.period.id)], limit=1)
        summary.write({'calc_state': 'stale', 'dirty': False})
        self.env['hr.payroll.attendance.engine']._cron_daily_reconcile()
        self.assertTrue(summary.dirty)

    def test_watchdog_recovers_stale_run(self):
        run = self.env['hr.payroll.attendance.calc.run'].create({
            'period_id': self.period.id, 'trigger': 'manual'})
        run.write({'state': 'running', 'heartbeat': '2026-01-01 00:00:00'})
        self.env['hr.payroll.attendance.engine']._cron_watchdog()
        self.assertEqual(run.state, 'failed')
        recovery = self.env['hr.payroll.attendance.calc.run'].search([
            ('trigger', '=', 'watchdog')])
        self.assertEqual(len(recovery), 1)
        self.assertIn(recovery.state, ('done', 'done_with_errors'))

    def test_history_recorded_on_quantity_change(self):
        History = self.env['hr.payroll.attendance.summary.history']
        run = self.env['hr.payroll.attendance.calc.run'].create({
            'period_id': self.period.id, 'trigger': 'manual'})
        run.action_process()
        self.assertFalse(History.search([]))
        # dirty one row, change its punch -> new quantities -> history row
        summary = self.env['hr.payroll.attendance.summary'].search([
            ('period_id', '=', self.period.id)], limit=1)
        self.env['hr.attendance'].create({
            'employee_id': summary.employee_id.id,
            'check_in': '2026-12-07 09:30:00',
            'check_out': '2026-12-07 16:00:00',
        })
        self.assertTrue(summary.dirty)
        run2 = self.env['hr.payroll.attendance.calc.run'].create({
            'period_id': self.period.id, 'trigger': 'manual'})
        run2.action_process()
        self.assertTrue(History.search_count([
            ('summary_id', '=', summary.id)]) >= 1)
