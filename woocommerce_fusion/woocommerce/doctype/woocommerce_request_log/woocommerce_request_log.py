# Copyright (c) 2023, Dirk van der Laarse and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WooCommerceRequestLog(Document):
    @staticmethod
    def clear_old_logs(days=7):
        from frappe.query_builder import Interval
        from frappe.query_builder.functions import Now

        table = frappe.qb.DocType("WooCommerce Request Log")
        frappe.db.delete(table, filters=(table.modified < (Now() - Interval(days=days))))
