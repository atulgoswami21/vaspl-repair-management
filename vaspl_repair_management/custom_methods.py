import frappe

@frappe.whitelist()
def generate_unique_naming(doctype, series, field, app, module):
    count = frappe.db.count(doctype, {"name": ("like", "{}%".format(series)), "docstatus": ["!=", 2]})
    return "{}{:05d}".format(series, count + 1)
