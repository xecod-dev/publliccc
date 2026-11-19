# Copyright (C) 2026
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).


def migrate(cr, version):
    """Post-migration for 16.0.1.2.0.

    The ORM has now created the new tables (period, calc run, history).
    Ensure the run-level partial unique index exists; log a summary of
    seeded leave-type classifications for the upgrade report.
    """
    if not version:
        return

    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        hr_payroll_calc_run_period_active_uidx
        ON hr_payroll_attendance_calc_run (period_id)
        WHERE state IN ('draft', 'running')
    """)

    cr.execute("""
        CREATE INDEX IF NOT EXISTS
        hr_payroll_summary_history_summary_idx
        ON hr_payroll_attendance_summary_history (summary_id, computed_at DESC)
    """)

    # Upgrade-report: leave-type classification snapshot
    cr.execute("""
        SELECT id, name->>'ar_SY', name->>'en_US', request_unit,
               is_mission, is_permission, is_late_excuse, is_early_leave_excuse,
               classification_reviewed
        FROM hr_leave_type ORDER BY id
    """)
    reviewed, ambiguous = 0, 0
    for row in cr.fetchall():
        if row[8]:
            reviewed += 1
        else:
            ambiguous += 1
    cr.execute("""
        SELECT COUNT(*) FROM hr_payroll_attendance_summary
        WHERE period_id IS NULL
    """)
    legacy_rows = cr.fetchone()[0]
    cr.execute("""
        SELECT COUNT(*) FROM hr_payroll_attendance_summary
        WHERE period_id IS NULL AND calc_state = 'computed'
    """)
    legacy_computed = cr.fetchone()[0]
    import logging
    logging.getLogger(__name__).info(
        'hr_payroll_attendance 16.0.1.2.0 migration: leave types reviewed=%s '
        'ambiguous=%s; legacy summary rows=%s computed=%s',
        reviewed, ambiguous, legacy_rows, legacy_computed)
