import frappe
from frappe import _
from erpnext.stock.doctype.stock_entry.stock_entry import (
	FinishedGoodError
)
from frappe.utils import (
	cint,
	date_diff,
	flt,
	get_datetime,
	get_link_to_form,
	getdate,
	nowdate,
	time_diff_in_hours,
)
logger = frappe.logger("api", allow_site=True, file_count=50)

def handle_Onsubmit_StockEntry(doc,event):
	if(doc.service_work_order):
		update_service_work_order(doc)
		if doc.purpose in ("Manufacture", "Repack","Repair"):
			mark_finished_and_scrap_items(doc)
			validate_finished_goods(doc)

def validate_finished_goods(doc):
	"""
	1. Check if FG exists (mfg, repack)
	2. Check if Multiple FG Items are present (mfg)
	3. Check FG Item and Qty against WO if present (mfg)
	"""
	production_item, wo_qty, finished_items = None, 0, []
	wo_details = frappe.db.get_value("Service Work Order", doc.service_work_order, ["production_item", "qty"])
	if wo_details:
		production_item, wo_qty = wo_details

	finished_qty=0;
	for d in doc.get("items"):
		if d.is_finished_item:
			# if not doc.work_order:
			# 	# Independent MFG Entry/ Repack Entry, no WO to match against
			# 	finished_items.append(d.item_code)
			# 	continue

			#if d.item_code != production_item:
			#	frappe.throw(
			#		_("Finished Item {0} does not match with Work Order {1}").format(
			#			d.item_code, doc.service_work_order
			#		)
			#	)
			#el
			if flt(d.transfer_qty) > flt(doc.fg_completed_qty): # check the quantity for the item_code in service workorder
				frappe.throw(
					_("Quantity in row {0} ({1}) must not be greater then manufactured quantity {2}").format(
						d.idx, d.transfer_qty, doc.fg_completed_qty
					)
				)

			finished_items.append(d.item_code)
			finished_qty+=flt(d.transfer_qty)

	if not finished_items:
		frappe.throw(
			msg=_("There must be atleast 1 Finished Good in this Stock Entry").format(doc.name),
			title=_("Missing Finished Good"),
			exc=FinishedGoodError,
		)

	if doc.purpose == "Manufacture":
		if flt(finished_qty) > flt(doc.fg_completed_qty):
			frappe.throw(
					_("Total Quantity for finished items {0} must not be greater then manufactured quantity {1}").format(
						finished_qty, doc.fg_completed_qty
					)
				)
		# if len(set(finished_items)) > 1:
		# 	frappe.throw(
		# 		msg=_("Multiple items cannot be marked as finished item"),
		# 		title=_("Note"),
		# 		exc=FinishedGoodError,
		# 	)

		allowance_percentage = flt(
			frappe.db.get_single_value(
				"Manufacturing Settings", "overproduction_percentage_for_work_order"
			)
		)
		allowed_qty = wo_qty + ((allowance_percentage / 100) * wo_qty)
		# No work order could mean independent Manufacture entry, if so skip validation
		if doc.service_work_order and doc.fg_completed_qty > allowed_qty:
			frappe.throw(
				_("For quantity {0} should not be greater than allowed quantity {1}").format(
					flt(doc.fg_completed_qty), allowed_qty
				)
			)
def get_finished_item(doc):
		finished_item = None
		if doc.service_work_order:
			finished_item = frappe.db.get_value("Service Work Order", doc.service_work_order, "production_item")
		#elif doc.bom_no:
		#	finished_item = frappe.db.get_value("BOM", doc.bom_no, "item")

		return finished_item
def mark_finished_and_scrap_items(doc):
	if any([d.item_code for d in doc.items if (d.is_finished_item and d.t_warehouse)]):
		return

	finished_item = get_finished_item(doc)

	if not finished_item and doc.purpose == "Manufacture":
		# In case of independent Manufacture entry, don't auto set
		# user must decide and set
		return

	# for d in doc.items:
	# 	if d.t_warehouse and not d.s_warehouse:
	# 		if doc.purpose == "Repack" or d.item_code == finished_item:
	# 			d.is_finished_item = 1
	# 		else:
	# 			d.is_scrap_item = 1
	# 	else:
	# 		d.is_finished_item = 0
	# 		d.is_scrap_item = 0

def update_service_work_order(doc):
		def _validate_service_work_order(pro_doc):
			msg, title = "", ""
			if flt(pro_doc.docstatus) != 1:
				msg = f"Work Order {doc.service_work_order} must be submitted"

			if pro_doc.status == "Stopped":
				msg = f"Transaction not allowed against stopped Work Order {doc.service_work_order}"

			if doc.is_return and pro_doc.status not in ["Completed", "Closed"]:
				title = _("Stock Return")
				msg = f"Work Order {doc.service_work_order} must be completed or closed"

			if msg:
				frappe.throw(_(msg), title=title)

		if doc.job_card:
			job_doc = frappe.get_doc("Job Card", doc.job_card)
			job_doc.set_transferred_qty(update_status=True)
			job_doc.set_transferred_qty_in_job_card_item(doc)

		if doc.service_work_order:
			pro_doc = frappe.get_doc("Service Work Order", doc.service_work_order)
			_validate_service_work_order(pro_doc)
			pro_doc.run_method("update_status")

			if doc.fg_completed_qty:
				pro_doc.run_method("update_service_work_order_qty")
				if doc.purpose == "Manufacture":
					pro_doc.run_method("update_swo_production_items_qty",{'stock_entry_doc': doc})
					pro_doc.run_method("update_planned_qty",{'stock_entry_doc': doc})
					pro_doc.update_batch_produced_qty(doc)

			#if not pro_doc.operations:
			#	pro_doc.set_actual_dates()