# Copyright (c) 2026, Dirk van der Laarse and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WooCommerceBatchLog(Document):
	def before_save(self):
		self.set_title()

	def set_title(self):
		"""
		The name is a random hash, so give the log a label that says what it did
		"""
		counts = f"{self.successful_items or 0}/{self.total_items or 0}"
		self.title = f"{self.resource_type or 'batch'} {counts} - {self.woocommerce_server}"

	@staticmethod
	def clear_old_logs(days=30):
		from frappe.query_builder import Interval
		from frappe.query_builder.functions import Now

		table = frappe.qb.DocType("WooCommerce Batch Log")
		frappe.db.delete(table, filters=(table.modified < (Now() - Interval(days=days))))
