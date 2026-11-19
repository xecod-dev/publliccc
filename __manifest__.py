{
    "name": "HR Payroll Attendance Summary",
    "version": "16.0.1.2.0",
    "category": "Human Resources",
    "description": """
HR Payroll Attendance Summary
=============================

Calculates attendance-based payroll effects including:

- Deterministic period-aware calculation engine (26th -> 25th periods)
  with HR-manager approval / closure workflow
- Permission offset only when the permission interval overlaps
  [scheduled start, actual arrival]
- Early-departure excuse offsets only the overlap between the excuse
  window and [actual check-out, scheduled shift end]; a shared per-day
  deficit ledger prevents double counting
- No runtime Arabic-name heuristics for new periods; leave types carry
  explicit classification flags seeded once by migration
- Dirty tracking on attendance / leave / contract / calendar changes
  with bounded batch calculation runs, watchdog recovery and daily
  reconcile
- Absence tracking with policy-based penalties
- Forgot fingerprint detection and penalties
- Fixed hours per day support (with visible data-quality warnings)
- Multi-week calendar support for rotating schedules
- Temporary shift management
- Portal integration (computed/preliminary/approved rows only)
    """,
    "depends": ["hr", "hr_attendance", "hr_holidays", "hr_payroll_community", "hr_policies", "resource_multi_week_calendar", "portal"],
    "data": [
        "security/ir.rule.xml",
        "security/ir.model.access.csv",
        "wizards/attendance_summary_wizard_views.xml",
        "views/hr_temporary_shift_views.xml",
        "views/attendance_summary_views.xml",
        "views/resource_calendar_views.xml",
        "views/hr_leave_type_views.xml",
        "views/hr_attendance_views.xml",
        "views/hr_payroll_period_views.xml",
        "views/portal_attendance_templates.xml",
        "data/ir_cron_data.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}