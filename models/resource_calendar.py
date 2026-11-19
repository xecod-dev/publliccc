from odoo import models, fields


class ResourceCalendar(models.Model):
    _inherit = 'resource.calendar'

    use_fixed_work_hours = fields.Boolean(
        string='Fixed Work Hours',
        help='Enable to use a fixed number of daily work hours when calculating attendance summaries.'
    )
    fixed_work_hours = fields.Float(
        string='Fixed Hours/Day',
        help='Number of hours to assume per day when fixed work hours are enabled.'
    )
    # is_multi_week is now provided by resource_multi_week_calendar module
