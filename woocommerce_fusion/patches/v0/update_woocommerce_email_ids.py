import traceback

import frappe
from frappe.contacts.doctype.contact.contact import get_contact_details, get_contacts_linking_to


@frappe.whitelist()
def execute():
	"""
	Updates the woocommerce_email field on all customers with the ID from the linked contact
	"""

	customers = frappe.db.get_all(
		"Customer",
		fields=["name"],
		order_by="name",
	)

	s = 0
	for customer in customers:
		try:
			contacts = get_contacts_linking_to("Customer", customer.name)

			woocommerce_email = None
			for contact in contacts:
				details = get_contact_details(contact)
				if details:
					woocommerce_email = details["contact_email"]
					if woocommerce_email:
						break

			if woocommerce_email:
				frappe.db.set_single_value("Customer", customer.name, "woocommerce_email", woocommerce_email)
				print(f"Setting {customer.name}'s woocommerce_email to {woocommerce_email}")
				s += 1

		except Exception as err:
			frappe.log_error("v0 WooCommerce Contacts Patch", traceback.format_exception(err))

		# Commit every 10 changes to avoid "Too many writes in one request. Please send smaller requests" error
		if s > 10:
			frappe.db.commit()
			s = 0

	frappe.db.commit()
