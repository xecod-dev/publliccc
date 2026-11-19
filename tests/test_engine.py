from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestEngine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.ref('base.main_company')
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Engine 8h Calendar',
            'tz': 'UTC',
            'attendance_ids': [(0, 0, {
                'name': 'Mon-Fri',
                'dayofweek': str(d),
                'hour_from': 8.0,
                'hour_to': 16.0,
            }) for d in range(5)],
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Engine Employee',
            'company_id': cls.company.id,
            'resource_calendar_id': cls.calendar.id,
        })
        cls.type_mission_day = cls.env['hr.leave.type'].create({
            'name': 'Mission Days (test)',
            'request_unit': 'day',
            'time_type': 'other',
            'is_mission': True,
            'classification_reviewed': True,
        })
        cls.type_permission = cls.env['hr.leave.type'].create({
            'name': 'Permission (test)',
            'request_unit': 'hour',
            'is_permission': True,
            'classification_reviewed': True,
        })
        cls.type_early = cls.env['hr.leave.type'].create({
            'name': 'Early Excuse (test)',
            'request_unit': 'hour',
            'is_early_leave_excuse': True,
            'classification_reviewed': True,
        })
        cls.type_unreviewed = cls.env['hr.leave.type'].create({
            'name': 'Unreviewed Hours (test)',
            'request_unit': 'day',
            'classification_reviewed': False,
        })

    def setUp(self):
        super().setUp()
        self.env['hr.payroll.attendance.summary'].search([]).unlink()
        self.env['hr.payroll.attendance.period'].search([]).unlink()

    def tearDown(self):
        self.env['hr.payroll.attendance.summary'].search([]).unlink()
        self.env['hr.payroll.attendance.period'].search([]).unlink()
        super().tearDown()

    def _period(self, date_from='2026-12-07', date_to='2026-12-11', key='t'):
        return self.env['hr.payroll.attendance.period'].create({
            'company_id': self.company.id,
            'period_key': key,
            'date_from': date_from,
            'date_to': date_to,
        })

    def _summary(self, period):
        summary = self.env['hr.payroll.attendance.summary'].create({
            'employee_id': self.employee.id,
            'company_id': self.company.id,
            'period_id': period.id,
            'date_from': period.date_from,
            'date_to': period.date_to,
            'calc_state': 'uncomputed',
            'dirty': True,
        })
        return summary

    def _attend(self, day, hour_in, hour_out=None):
        vals = {
            'employee_id': self.employee.id,
            'check_in': f'2026-12-{day:02d} {hour_in:02d}:00:00',
        }
        if hour_out is not None:
            vals['check_out'] = f'2026-12-{day:02d} {hour_out:02d}:00:00'
        return self.env['hr.attendance'].create(vals)

    def _leave(self, ltype, day, h_from=None, h_to=None):
        vals = {
            'employee_id': self.employee.id,
            'holiday_status_id': ltype.id,
            'request_date_from': f'2026-12-{day:02d}',
            'request_date_to': f'2026-12-{day:02d}',
            'number_of_days': 1.0 if h_from is None else 0.125,
        }
        if h_from is not None:
            vals['date_from'] = f'2026-12-{day:02d} {h_from:02d}:00:00'
            vals['date_to'] = f'2026-12-{day:02d} {h_to:02d}:00:00'
        leave = self.env['hr.leave'].create(vals)
        leave.action_confirm()
        leave.action_validate()
        return leave

    def _compute(self, summary):
        engine = self.env['hr.payroll.attendance.engine']
        vals = engine._compute_summary_vals(summary)
        engine._apply_vals(summary, vals)
        return summary

    # Monday 2026-12-07 .. Friday 2026-12-11 are the 5 working days.

    def test_perfect_presence_zero_activity(self):
        period = self._period()
        summary = self._summary(period)
        for day in (7, 8, 9, 10, 11):
            self._attend(day, 8, 16)
        self._compute(summary)
        self.assertEqual(summary.calc_state, 'computed')
        self.assertFalse(summary.dirty)
        self.assertFalse(summary.has_activity)
        self.assertEqual(summary.total_absence_days, 0.0)
        self.assertEqual(summary.total_lateness_hours, 0.0)
        self.assertEqual(summary.total_extra_hours, 0.0)

    def test_permission_overlap_offsets_arrival_gap(self):
        # Arrives 10:00, permission window 09:00-10:00 -> offset 60, lateness 60
        period = self._period()
        summary = self._summary(period)
        self._leave(self.type_permission, 7, 9, 10)
        self._attend(7, 10, 16)
        self._compute(summary)
        self.assertEqual(summary.net_lateness_minutes_list, '60')
        self.assertEqual(summary.total_lateness_hours, 1.0)

    def test_permission_after_arrival_no_offset(self):
        # Arrives 10:00 late; permission 12:00-13:00 -> zero overlap -> full 360
        period = self._period()
        summary = self._summary(period)
        self._leave(self.type_permission, 8, 12, 13)
        self._attend(8, 10, 16)
        self._compute(summary)
        self.assertEqual(summary.net_lateness_minutes_list, '360')

    def test_permission_partially_before_arrival(self):
        # Arrives 09:30; permission 09:00-10:00 -> overlap 30 -> lateness 30
        period = self._period()
        summary = self._summary(period)
        self._leave(self.type_permission, 9, 9, 10)
        self._attend(9, 9, 16)
        self._attend(9, 9, 16)  # duplicate punch must not change results
        self._compute(summary)
        self.assertEqual(summary.net_lateness_minutes_list, '30')

    def test_early_departure_excuse_overlap(self):
        # In 08:00 out 15:00 -> 60min deficit; early excuse 15:30-16:00 -> 30min
        period = self._period()
        summary = self._summary(period)
        self._leave(self.type_early, 10, 15, 16)  # 15:30 windowed below
        # adjust window to 15:30-16:00
        leave = self.env['hr.leave'].search([
            ('employee_id', '=', self.employee.id),
            ('holiday_status_id', '=', self.type_early.id)])
        leave.write({
            'date_from': '2026-12-10 15:30:00',
            'date_to': '2026-12-10 16:00:00',
        })
        self._attend(10, 8, 15)
        self._compute(summary)
        self.assertEqual(summary.net_lateness_minutes_list, '30')

    def test_shared_ledger_no_double_count(self):
        # Late 2h (in 10:00) + early 1h (out 15:00) -> deficit 180;
        # permission 09-10 offsets 60, early excuse 15:30-16 offsets 30 -> 90
        period = self._period()
        summary = self._summary(period)
        self._leave(self.type_permission, 11, 9, 10)
        self._leave(self.type_early, 11, 15, 16)
        self.env['hr.leave'].search([
            ('employee_id', '=', self.employee.id),
            ('holiday_status_id', '=', self.type_early.id)]).write({
            'date_from': '2026-12-11 15:30:00',
            'date_to': '2026-12-11 16:00:00',
        })
        self._attend(11, 10, 15)
        self._compute(summary)
        self.assertEqual(summary.net_lateness_minutes_list, '90')

    def test_forgot_fingerprint_counted(self):
        period = self._period()
        summary = self._summary(period)
        self._attend(7, 8)  # no checkout
        self._compute(summary)
        self.assertEqual(summary.forgot_attendance_count, 1)
        self.assertTrue(summary.has_activity)

    def test_absence_day(self):
        period = self._period()
        summary = self._summary(period)
        # no punches at all -> 5 absence days
        self._compute(summary)
        self.assertEqual(summary.total_absence_days, 5.0)

    def test_classification_exception_blocks(self):
        period = self._period()
        summary = self._summary(period)
        self._leave(self.type_unreviewed, 7, 9, 10)
        self._attend(7, 9, 16)
        self._compute(summary)
        self.assertTrue(summary.classification_exception)
        period.action_open_calculation()
        with self.assertRaises(Exception):
            period.action_request_approval()

    def test_pending_hour_uses_scheduled_day(self):
        # Pending 2h permission on an 8h day -> 0.25 pending days (not /8.0
        # which would also give 0.25 here; assert against a 4h day calendar
        # variant instead: pending 2h / 4h = 0.5 vs legacy /8.0 = 0.25)
        calendar4 = self.env['resource.calendar'].create({
            'name': 'Engine 4h Calendar',
            'tz': 'UTC',
            'attendance_ids': [(0, 0, {
                'name': 'Mon-Fri',
                'dayofweek': '0',
                'hour_from': 8.0,
                'hour_to': 12.0,
            })],
        })
        emp4 = self.env['hr.employee'].create({
            'name': 'Engine Employee 4h',
            'company_id': self.company.id,
            'resource_calendar_id': calendar4.id,
        })
        period = self._period()
        summary = self.env['hr.payroll.attendance.summary'].create({
            'employee_id': emp4.id,
            'company_id': self.company.id,
            'period_id': period.id,
            'date_from': period.date_from,
            'date_to': period.date_to,
            'calc_state': 'uncomputed',
            'dirty': True,
        })
        self.env['hr.leave'].create({
            'employee_id': emp4.id,
            'holiday_status_id': self.type_permission.id,
            'request_date_from': '2026-12-07',
            'request_date_to': '2026-12-07',
            'date_from': '2026-12-07 09:00:00',
            'date_to': '2026-12-07 11:00:00',
            'number_of_days': 0.25,
            'state': 'confirm',  # pending
        })
        self._compute(summary)
        self.assertAlmostEqual(summary.pending_leave_days, 0.5)
