from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

# get version from __version__ variable in woocommerce_fusion/__init__.py
from woocommerce_fusion import __version__ as version

setup(
    name="woocommerce_fusion",
    version=version,
    description="WooCommerce connector for ERPNext v14+",
    author="Dirk van der Laarse",
    author_email="dirk@finfoot.work",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)
