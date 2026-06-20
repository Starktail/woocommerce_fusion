import frappe
from erpnext.setup.utils import _enable_all_roles_for_admin, set_defaults_for_tests
from frappe.utils.data import now_datetime


def before_tests():
	frappe.clear_cache()
	# complete setup if missing
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	if not frappe.db.a_row_exists("Company"):
		current_year = now_datetime().year
		setup_complete(
			{
				"currency": "INR",
				"full_name": "Test User",
				"company_name": "Some Company (Pty) Ltd",
				"timezone": "Africa/Johannesburg",
				"company_abbr": "SC",
				"industry": "Manufacturing",
				"country": "South Africa",
				"fy_start_date": f"{current_year}-01-01",
				"fy_end_date": f"{current_year}-12-31",
				"language": "english",
				"company_tagline": "Testing",
				"email": "test@erpnext.com",
				"password": "test",
				"chart_of_accounts": "Standard",
			}
		)
	elif not frappe.db.exists("Company", "Some Company (Pty) Ltd"):
		# For running tests in v16
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "Some Company (Pty) Ltd",
				"abbr": "SC",
				"default_currency": "INR",
				"country": "South Africa",
				"chart_of_accounts": "Standard",
			}
		).insert()

	if not frappe.db.exists("User", "test@erpnext.com"):
		# For running tests in v16
		frappe.get_doc(
			{
				"doctype": "User",
				"email": "test@erpnext.com",
				"first_name": "Test",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)

	# For running tests in v16
	global_defaults = frappe.get_single("Global Defaults")
	if global_defaults.default_company != "Some Company (Pty) Ltd":
		global_defaults.default_company = "Some Company (Pty) Ltd"
		global_defaults.save()

	_enable_all_roles_for_admin()

	set_defaults_for_tests()
	create_curr_exchange_record()

	# v16 scenario
	non_group_customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	if non_group_customer_group:
		frappe.db.set_default("customer_group", non_group_customer_group)
		frappe.db.set_single_value("Selling Settings", "customer_group", non_group_customer_group)

	# following same practice as in erpnext app to commit manually inside before_tests
	# nosemgrep
	frappe.db.commit()


def create_curr_exchange_record():
	"""
	Create Currency Exchange records for the currencies used in tests
	"""
	currencies = ["USD", "ZAR"]

	for currency in currencies:
		cur_exchange = frappe.new_doc("Currency Exchange")
		cur_exchange.date = "2016-01-01"
		cur_exchange.from_currency = currency
		cur_exchange.to_currency = "INR"
		cur_exchange.for_buying = 1
		cur_exchange.for_selling = 1
		cur_exchange.exchange_rate = 2.0

		cur_exchange.insert(ignore_if_duplicate=True)
