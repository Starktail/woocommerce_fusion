import frappe
from frappe import _
from frappe.core.doctype.log_settings.log_settings import _supports_log_clearing
from frappe.utils.data import cint


def execute():
	"""
	Register the Batch API log doctypes with Log Settings for automatic cleanup
	"""
	frappe.reload_doc("woocommerce", "doctype", "WooCommerce Batch Log")
	frappe.reload_doc("woocommerce", "doctype", "WooCommerce Sync Queue")

	WOOCOMMERCE_LOGTYPES_RETENTION = {
		"WooCommerce Batch Log": 30,
		"WooCommerce Sync Queue": 30,
	}

	log_settings = frappe.get_single("Log Settings")
	existing_logtypes = {d.ref_doctype for d in log_settings.logs_to_clear}
	added_logtypes = set()
	for logtype, retention in WOOCOMMERCE_LOGTYPES_RETENTION.items():
		if logtype not in existing_logtypes and _supports_log_clearing(logtype):
			if not frappe.db.exists("DocType", logtype):
				continue

			log_settings.append("logs_to_clear", {"ref_doctype": logtype, "days": cint(retention)})
			added_logtypes.add(logtype)
			log_settings.save()
			frappe.db.commit()

	if added_logtypes:
		print(_("Added default log doctypes: {}").format(",".join(added_logtypes)))
