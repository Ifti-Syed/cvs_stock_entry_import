# -*- coding: utf-8 -*-
from setuptools import setup, find_packages

with open('requirements.txt') as f:
	install_requires = f.read().strip().split('\n')

# get version from __version__ variable
from cvs_stock_entry_import import __version__ as version

setup(
	name='cvs_stock_entry_import',
	version=version,
	description='Extracts Material Issuance Summary documents with AI and generates standard ERPNext Stock Entries (Material Issue)',
	author='Iftikhar Hussain Syed',
	author_email='iftikhar.hussain@cvshvac.com',
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
