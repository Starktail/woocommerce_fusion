# Copyright (c) 2023, Dirk van der Laarse and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ItemWooCommerceServer(Document):
	def validate(self):
		if self.woocommerce_id is not None:
			cleaned = str(self.woocommerce_id).strip()
			self.woocommerce_id = cleaned if cleaned else None
