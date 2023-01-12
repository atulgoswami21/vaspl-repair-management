from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in vaspl_repair_management/__init__.py
from vaspl_repair_management import __version__ as version

setup(
	name="vaspl_repair_management",
	version=version,
	description="Repair Management System",
	author="Vijatshi Software",
	author_email="atul@vijatshisoftware.com",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
