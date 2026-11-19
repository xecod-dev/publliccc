"""Legacy golden-set regression for hr_payroll_attendance 16.0.1.2.0.

Run against the production DB via odoo shell BEFORE upgrading:

    odoo-bin shell -c /etc/odoo-server.conf -d db \
        --no-http < scripts/regression_check.py

What it does (READ-ONLY for stored data):
  1. Loads all legacy summaries (period_id IS NULL, last_computed_date set).
  2. Re-runs the NEW deterministic engine on a throw-away copy of each row's
     date window and compares quantities against the stored legacy values.
  3. Emits a CSV diff report. Only the documented approved differences are
     expected: the permission-overlap correction and the pending-hour
     scheduled-day conversion.

NOTE: this script must be run BEFORE the production upgrade and its output
archived in /home/odoohr/payroll_audit/ as part of the release gates.
"""

import csv
import hashlib
import logging

_logger = logging.getLogger(__name__)

OUT_CSV = '/home/odoohr/payroll_audit/golden_set_regression.csv'
LIMIT = None  # None = all rows; set an int for a sample run


def _hash(absence, penalty, leave, pending, late_min, extra_min, forgot):
    raw = "%.4f|%.4f|%.4f|%.4f|%.0f|%.0f|%d" % (
        absence, penalty, leave, pending, late_min, extra_min, forgot)
    return hashlib.md5(raw.encode()).hexdigest()


def main(env):
    Summary = env['hr.payroll.attendance.summary']
    Engine = env['hr.payroll.attendance.engine'].sudo()
    rows = Summary.search([
        ('period_id', '=', False),
        ('last_computed_date', '!=', False),
    ], order='id')
    if LIMIT:
        rows = rows[:LIMIT]
    _logger.info('Golden-set regression: %d legacy rows', len(rows))

    out = []
    matched = 0
    for rec in rows:
        stored = _hash(
            rec.total_absence_days, rec.total_penalty_days,
            rec.total_leave_days, rec.pending_leave_days,
            (rec.total_lateness_hours or 0.0) * 60,
            (rec.total_extra_hours or 0.0) * 60,
            rec.forgot_attendance_count or 0)
        vals = Engine._compute_summary_vals(rec)
        fresh = vals['quantity_hash']
        same = stored == fresh
        if same:
            matched += 1
        out.append({
            'summary_id': rec.id,
            'employee_id': rec.employee_id.id,
            'employee': rec.employee_id.name,
            'date_from': rec.date_from,
            'date_to': rec.date_to,
            'stored_hash': stored,
            'recomputed_hash': fresh,
            'match': 'MATCH' if same else 'DIFF',
            'stored_absence': rec.total_absence_days,
            'new_absence': vals['total_absence_days'],
            'stored_penalty': rec.total_penalty_days,
            'new_penalty': vals['total_penalty_days'],
            'stored_leave': rec.total_leave_days,
            'new_leave': vals['total_leave_days'],
            'stored_pending': rec.pending_leave_days,
            'new_pending': vals['pending_leave_days'],
            'stored_lateness_hours': rec.total_lateness_hours,
            'new_lateness_hours': vals['total_lateness_hours'],
            'stored_extra_hours': rec.total_extra_hours,
            'new_extra_hours': vals['total_extra_hours'],
            'stored_forgot': rec.forgot_attendance_count,
            'new_forgot': vals['forgot_attendance_count'],
        })

    with open(OUT_CSV, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        writer.writeheader()
        writer.writerows(out)

    _logger.info(
        'Golden-set regression: matched=%d/%d -> %s',
        matched, len(rows), OUT_CSV)
    env.cr.rollback()  # never persist anything; engine writes nothing but be safe
    return matched, len(rows)


main(env)
