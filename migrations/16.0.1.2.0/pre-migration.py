# Copyright (C) 2026
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).


def migrate(cr, version):
    """Pre-migration for 16.0.1.2.0.

    Runs BEFORE the ORM applies schema changes:
    1. Adds the leave-type classification columns and seeds them once from
       the legacy classification (exact source logic, no runtime guessing).
    2. Adds the summary binding/state columns needed by partial indexes and
       marks the 2,176 legacy rows computed.
    3. Creates the partial unique index for (employee, period) summaries.
    """
    if not version:
        return

    # ---- 1. hr.leave.type classification columns -----------------------
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'hr_leave_type'
          AND column_name IN ('is_mission', 'is_permission', 'classification_reviewed')
    """)
    existing = {row[0] for row in cr.fetchall()}

    if 'is_mission' not in existing:
        cr.execute("ALTER TABLE hr_leave_type ADD COLUMN is_mission BOOLEAN DEFAULT FALSE")
    if 'is_permission' not in existing:
        cr.execute("ALTER TABLE hr_leave_type ADD COLUMN is_permission BOOLEAN DEFAULT FALSE")
    if 'classification_reviewed' not in existing:
        cr.execute(
            "ALTER TABLE hr_leave_type ADD COLUMN classification_reviewed BOOLEAN DEFAULT FALSE")

    # Seed is_mission using the exact legacy heuristic on both translations.
    # NOTE: most types in this database store the Arabic label in en_US
    # (ar_SY empty), so the Arabic pattern must be applied to BOTH keys.
    cr.execute("""
        UPDATE hr_leave_type
        SET is_mission = TRUE
        WHERE COALESCE(name->>'ar_SY', '') ~ 'مأمور|مامور'
           OR COALESCE(name->>'en_US', '') ~ 'مأمور|مامور'
           OR COALESCE(name->>'en_US', '') ILIKE '%mission%'
    """)

    # Seed is_permission: hour-request types OR types that already carry
    # validated hour-unit leaves.
    cr.execute("""
        UPDATE hr_leave_type
        SET is_permission = TRUE
        WHERE request_unit = 'hour'
           OR EXISTS (
               SELECT 1 FROM hr_leave l
               WHERE l.holiday_status_id = hr_leave_type.id
                 AND l.state = 'validate'
                 AND (l.request_unit_hours = TRUE))
    """)

    # classification_reviewed = FALSE only for ambiguous types: they carry
    # validated hour leaves but request_unit != 'hour'. Everything else is
    # unambiguous -> reviewed.
    cr.execute("""
        UPDATE hr_leave_type
        SET classification_reviewed = TRUE
        WHERE NOT (
            request_unit <> 'hour'
            AND request_unit IS DISTINCT FROM 'hour'
            AND EXISTS (
                SELECT 1 FROM hr_leave l
                WHERE l.holiday_status_id = hr_leave_type.id
                  AND l.state = 'validate'
                  AND (l.request_unit_hours = TRUE))
        )
    """)

    # ---- 2. hr.payroll.attendance.summary new columns ------------------
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'hr_payroll_attendance_summary'
          AND column_name IN (
              'period_id', 'calc_state', 'dirty', 'last_error', 'error_count',
              'last_checked', 'last_run_id', 'has_activity', 'quantity_hash',
              'data_quality_warning', 'classification_exception')
    """)
    summary_cols = {row[0] for row in cr.fetchall()}

    if 'period_id' not in summary_cols:
        cr.execute("ALTER TABLE hr_payroll_attendance_summary ADD COLUMN period_id INTEGER")
    if 'calc_state' not in summary_cols:
        cr.execute(
            "ALTER TABLE hr_payroll_attendance_summary "
            "ADD COLUMN calc_state VARCHAR DEFAULT 'uncomputed' NOT NULL")
    if 'dirty' not in summary_cols:
        cr.execute(
            "ALTER TABLE hr_payroll_attendance_summary "
            "ADD COLUMN dirty BOOLEAN DEFAULT FALSE")
    if 'last_error' not in summary_cols:
        cr.execute("ALTER TABLE hr_payroll_attendance_summary ADD COLUMN last_error TEXT")
    if 'error_count' not in summary_cols:
        cr.execute(
            "ALTER TABLE hr_payroll_attendance_summary "
            "ADD COLUMN error_count INTEGER DEFAULT 0")
    if 'last_checked' not in summary_cols:
        cr.execute("ALTER TABLE hr_payroll_attendance_summary ADD COLUMN last_checked TIMESTAMP")
    if 'last_run_id' not in summary_cols:
        cr.execute("ALTER TABLE hr_payroll_attendance_summary ADD COLUMN last_run_id INTEGER")
    if 'has_activity' not in summary_cols:
        cr.execute(
            "ALTER TABLE hr_payroll_attendance_summary "
            "ADD COLUMN has_activity BOOLEAN DEFAULT FALSE")
    if 'quantity_hash' not in summary_cols:
        cr.execute("ALTER TABLE hr_payroll_attendance_summary ADD COLUMN quantity_hash VARCHAR")
    if 'data_quality_warning' not in summary_cols:
        cr.execute(
            "ALTER TABLE hr_payroll_attendance_summary ADD COLUMN data_quality_warning TEXT")
    if 'classification_exception' not in summary_cols:
        cr.execute(
            "ALTER TABLE hr_payroll_attendance_summary ADD COLUMN classification_exception TEXT")

    # Legacy rows keep their computed quantities visible: rows that were
    # computed at least once are marked 'computed'; untouched placeholders
    # stay 'uncomputed'.
    cr.execute("""
        UPDATE hr_payroll_attendance_summary
        SET calc_state = 'computed'
        WHERE period_id IS NULL
          AND last_computed_date IS NOT NULL
          AND calc_state = 'uncomputed'
    """)

    # ---- 3. Partial unique index for period summaries ------------------
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        hr_payroll_summary_period_emp_uidx
        ON hr_payroll_attendance_summary (employee_id, company_id, period_id)
        WHERE period_id IS NOT NULL
    """)

    # Indexes supporting the dirty processor and reconcile queries
    cr.execute("""
        CREATE INDEX IF NOT EXISTS
        hr_payroll_summary_period_state_idx
        ON hr_payroll_attendance_summary (period_id, calc_state)
        WHERE period_id IS NOT NULL
    """)
