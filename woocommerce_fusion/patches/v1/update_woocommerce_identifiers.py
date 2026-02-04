import traceback

import frappe
from frappe.utils.fixtures import sync_fixtures


@frappe.whitelist()
def execute():
	"""
	Updates the woocommerce_identifier field on all customers
	"""

	sync_fixtures("woocommerce_fusion")

	customers = frappe.db.get_all(
		"Customer",
		filters=[["Customer", "woocommerce_email", "is", "set"]],
		fields=["name", "woocommerce_email"],
		order_by="name",
	)

	s = 0
	for customer in customers:
		print(f"Setting {customer.name}'s woocommerce_identifier to {customer.woocommerce_email}")
		try:
			frappe.db.set_value(
				"Customer", customer.name, "woocommerce_identifier", customer.woocommerce_email
			)
			s += 1

		except Exception as err:
			frappe.log_error("v1 WooCommerce Unique Identifier patch", traceback.format_exception(err))

		# Commit every 10 changes to avoid "Too many writes in one request. Please send smaller requests" error
		if s > 10:
			frappe.db.commit()
			s = 0

	frappe.db.commit()

	# Delete unused custom fields
	custom_field_names = [
		"Customer-woocommerce_email",
		"Address-woocommerce_server",
		"Address-woocommerce_email",
	]
	for field_name in custom_field_names:
		if frappe.db.exists("Custom Field", field_name):
			frappe.db.delete("Custom Field", field_name)
	frappe.db.commit()
	sync_fixtures("woocommerce_fusion")


if __name__ == "__main__":
	execute()
