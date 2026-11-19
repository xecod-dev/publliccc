# Copyright (C) 2026
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).


def migrate(cr, version):
    """
    Migrate computed fields to stored fields.
    Add columns for total_absence_days, total_penalty_days, calculation_log, net_lateness_minutes_list.
    """
    if not version:
        return

    # Check if columns exist and add them if not
    cr.execute("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = 'hr_payroll_attendance_summary' 
        AND column_name IN ('total_absence_days', 'total_penalty_days', 'calculation_log', 'net_lateness_minutes_list')
    """)
    existing_columns = [row[0] for row in cr.fetchall()]

    if 'total_absence_days' not in existing_columns:
        cr.execute("""
            ALTER TABLE hr_payroll_attendance_summary 
            ADD COLUMN total_absence_days NUMERIC DEFAULT 0.0
        """)

    if 'total_penalty_days' not in existing_columns:
        cr.execute("""
            ALTER TABLE hr_payroll_attendance_summary 
            ADD COLUMN total_penalty_days NUMERIC DEFAULT 0.0
        """)

    if 'calculation_log' not in existing_columns:
        cr.execute("""
            ALTER TABLE hr_payroll_attendance_summary 
            ADD COLUMN calculation_log TEXT
        """)

    if 'net_lateness_minutes_list' not in existing_columns:
        cr.execute("""
            ALTER TABLE hr_payroll_attendance_summary 
            ADD COLUMN net_lateness_minutes_list VARCHAR
        """)
