## WooCommerce Fusion

![CI workflow](https://github.com/dvdl16/woocommerce_fusion/actions/workflows/ci.yml/badge.svg?branch=version-15)
[![codecov](https://codecov.io/gh/dvdl16/woocommerce_fusion/graph/badge.svg?token=A5OR5QIOUX)](https://codecov.io/gh/dvdl16/woocommerce_fusion)

WooCommerce connector for ERPNext v15

This app allows you to synchronise your ERPNext site with **multiple** WooCommerce websites

### Features

- [Sales Order Synchronisation](https://woocommerce-fusion-docs.finfoot.tech/features/sales-order)
- [Item Synchronisation](https://woocommerce-fusion-docs.finfoot.tech/features/items)
- [Sync Item Stock Levels](https://woocommerce-fusion-docs.finfoot.tech/features/item-stock-levels)
- [Sync Item Prices](https://woocommerce-fusion-docs.finfoot.tech/features/item-prices)
- [Integration with WooCommerce Plugins](https://woocommerce-fusion-docs.finfoot.tech/features/woocommerce-plugins)

### User documentation

User documentation is hosted at [woocommerce-fusion-docs.finfoot.tech](https://woocommerce-fusion-docs.finfoot.tech)

### Manual Installation

1. [Install bench](https://github.com/frappe/bench).
2. [Install ERPNext](https://github.com/frappe/erpnext#installation).
3. Once ERPNext is installed, add the woocommerce_fusion app to your bench by running

	```sh
	$ bench get-app https://github.com/dvdl16/woocommerce_fusion
	```
4. After that, you can install the woocommerce_fusion app on the required site by running
	```sh
	$ bench --site sitename install-app woocommerce_fusion
	```


### Tests

#### Requirements
For integration tests, we use [WordPress Playground](https://wordpress.org/playground/development-environments/) to spin up temporary Wordpress websites:

```shell
npm i @wp-playground/cli
```

You also need to install [Caddy](https://caddyserver.com/docs/install#install), which acts as a reverse proxy. This allows us to call the Wordpress/WooCommerce API over SSL so that the same authentication method is used as production sites

Furthermore, have a Frappe site available with ERPNext and WooCommerce Fusion pre-installed.

#### Run unit and integration tests

1. Navigate to the app directory
```
cd frappe-bench/apps/woocommerce_fusion
```

2. Start Wordpress Playground
```shell
npx @wp-playground/cli server --blueprint wp_woo_blueprint.json  --site-url=https://woo-test.localhost
```

3. Start Caddy
```shell
caddy run --config wp_woo_caddy --adapter caddyfile
```

*Should you want to check out the locally running wordpress instance, navigate to [https://woo-test.localhost](https://woo-test.localhost) in your browser. The default login details are `admin` and `password`*

4. Set the correct environment variables and run the tests
```shell
export WOO_INTEGRATION_TESTS_WEBSERVER="https://woo-test.localhost"
export WOO_API_CONSUMER_KEY="ck_test_123456789"
export WOO_API_CONSUMER_SECRET="cs_test_abcdefg"
bench --site test_site run-tests --app woocommerce_fusion --coverage
```

### Development

We use [pre-commit](https://pre-commit.com/) for linting. First time setup may be required:
```shell
# Install pre-commit
pip install pre-commit

# Install the git hook scripts
pre-commit install

#(optional) Run against all the files
pre-commit run --all-files
```

We use [Semgrep](https://semgrep.dev/docs/getting-started/) rules specific to [Frappe Framework](https://github.com/frappe/frappe)
```shell
# Install semgrep
python3 -m pip install semgrep

# Clone the rules repository
git clone --depth 1 https://github.com/frappe/semgrep-rules.git frappe-semgrep-rules

# Run semgrep specifying rules folder as config 
semgrep --config=/workspace/development/frappe-semgrep-rules/rules apps/woocommerce_fusion
```

If you use VS Code, you can specify the `.flake8` config file in your `settings.json` file:
```shell
"python.linting.flake8Args": ["--config=frappe-bench-v15/apps/woocommerce_fusion/.flake8_strict"]
```


The documentation has been generated using [mdBook](https://rust-lang.github.io/mdBook/guide/creating.html)

Make sure you have [mdbook](https://rust-lang.github.io/mdBook/guide/installation.html) installed/downloaded. To modify and test locally:
```shell
cd docs
mdbook serve --open
```

### License

GNU GPL V3

The code is licensed as GNU General Public License (v3) and the copyright is owned by Finfoot Tech (Pty) Ltd and Contributors.
