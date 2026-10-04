# -*- coding: utf-8 -*-
from __future__ import unicode_literals

app_name = "cvs_stock_entry_import"
app_title = "CVS Stock Entry Import"
app_publisher = "Iftikhar Hussain Syed"
app_description = "Extracts Material Issuance Summary documents with AI and generates standard ERPNext Stock Entries (Material Issue)."
app_icon = "octicon octicon-package"
app_color = "blue"
app_email = "iftikhar.hussain@cvshvac.com"
app_license = "MIT"
app_version = '0.0.1'

# include js in doctype views
doctype_js = {
}

fixtures = [
	{
		"doctype": "Custom Field",
		"filters": [["name", "in", ["Stock Entry Detail-requested_qty"]]],
	}
]

# Scheduled Tasks
# ---------------

scheduler_events = {
}
