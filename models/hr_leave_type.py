from odoo import models, fields


class HrLeaveType(models.Model):
    _inherit = "hr.leave.type"

    is_late_excuse = fields.Boolean(
        string="Excuses Lateness",
        help="If checked, approved leaves of this type will prevent late penalties for the day.",
    )
    is_early_leave_excuse = fields.Boolean(
        string="Excuses Early Leave",
        help="If checked, approved hour-based leaves of this type offset early departure: "
             "only the overlap between the excuse window and the interval between the actual "
             "check-out and the scheduled shift end is excused.",
    )
    is_mission = fields.Boolean(
        string="Official Mission (مأمورية)",
        help="Official mission. Seeded once from the legacy classification by pre-migration. "
             "New types must set this flag explicitly; runtime never guesses by name.",
    )
    is_permission = fields.Boolean(
        string="Permission (إذن)",
        help="Hour-based permission. Seeded once from the legacy classification by pre-migration. "
             "New types must set this flag explicitly; runtime never guesses by name.",
    )
    classification_reviewed = fields.Boolean(
        string="Classification Reviewed",
        default=False,
        help="Set by migration for types whose legacy classification is unambiguous. "
             "Hour-based leaves on unreviewed types raise classification exceptions in the "
             "attendance summaries and block final period approval until HR classifies them.",
    )
