import frappe
from erpnext.setup.utils import _enable_all_roles_for_admin, set_defaults_for_tests
from frappe.utils.data import now_datetime


def before_tests():
	frappe.clear_cache()
	# complete setup if missing
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	if not frappe.db.exists("Company", "Some Company (Pty) Ltd"):
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

	_enable_all_roles_for_admin()

	set_defaults_for_tests()
	create_curr_exchange_record()

	# set_defaults_for_tests() points the default customer_group at the ROOT (a group) node, which
	# v16 rejects as a Customer's group; point it at a real (non-group) customer group instead.
	non_group_customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	if non_group_customer_group:
		frappe.db.set_default("customer_group", non_group_customer_group)
		frappe.db.set_single_value("Selling Settings", "customer_group", non_group_customer_group)

	# Set default for v16
	if frappe.db.exists("Warehouse", "Stores - SC"):
		frappe.db.set_single_value("Stock Settings", "default_warehouse", "Stores - SC")

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
