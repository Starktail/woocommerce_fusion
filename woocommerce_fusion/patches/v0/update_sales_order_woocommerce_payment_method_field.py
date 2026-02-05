import traceback

import frappe
from frappe.utils.fixtures import sync_fixtures

from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


@frappe.whitelist()
def execute():
	"""
	Updates the woocommerce_payment_method field on all sales orders where the field is blank
	"""
	# Sync fixtures to ensure that the custom field `woocommerce_payment_method` exists
	sync_fixtures("woocommerce_fusion")

	# Get the Sales Orders
	sales_orders = frappe.db.get_all(
		"Sales Order",
		fields=["name", "woocommerce_server", "woocommerce_id", "woocommerce_payment_method"],
		order_by="name",
	)

	s = 0
	for so in sales_orders:
		if so.woocommerce_server and so.woocommerce_id and not so.woocommerce_payment_method:
			try:
				# Get the Sales Order doc
				sales_order = frappe.get_doc("Sales Order", so.name)

				# Get the WooCommerce Order doc
				wc_order = frappe.get_doc(
					{
						"doctype": "WooCommerce Order",
						"name": generate_woocommerce_record_name_from_domain_and_id(
							so.woocommerce_server, so.woocommerce_id
						),
					}
				)
				wc_order.load_from_db()

				# Set the payment_method_title field
				sales_order.meta.get_field("woocommerce_payment_method").allow_on_submit = 1
				sales_order.woocommerce_payment_method = wc_order.payment_method_title
				print(f"Updating {so.name}")
				sales_order.save()
				sales_order.meta.get_field("woocommerce_payment_method").allow_on_submit = 0

			except Exception as err:
				frappe.log_error(
					f"v0 WooCommerce Sales Orders Patch: Sales Order {so.name}",
					"".join(traceback.format_exception(err)),
				)

		# Commit every 10 changes to avoid "Too many writes in one request. Please send smaller requests" error
		if s > 10:
			frappe.db.commit()
			s = 0

	frappe.db.commit()
