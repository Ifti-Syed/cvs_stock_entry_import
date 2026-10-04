# -*- coding: utf-8 -*-
from __future__ import unicode_literals

app_name = "cvs_stock_entry_import"
app_title = "CVS Stock Entry Import"
app_publisher = "Iftikhar Hussain Syed"
app_description = "Extracts Material Issuance Summary documents with AI and generates standard ERPNext Stock Entries (Material Issue)."
app_icon = "octicon octicon-package"
app_color = "blue"
app_email = "iftikhar.hussain@cvshvac.com"
app_license = "Proprietary"
app_version = '0.0.1'

# include js in doctype views
doctype_js = {
}

# No fixtures: Stock Entry Detail.requested_qty is expected to already exist
# on the target site (either as a pre-existing Custom Field or, if not,
# create it manually once via Customize Form before using this app — see
# CVSStockEntryImport.generate_stock_entry(), which checks for it with
# frappe.get_meta("Stock Entry Detail").has_field("requested_qty") and
# simply skips setting it if it's genuinely absent).

# Scheduled Tasks
# ---------------

scheduler_events = {
}
