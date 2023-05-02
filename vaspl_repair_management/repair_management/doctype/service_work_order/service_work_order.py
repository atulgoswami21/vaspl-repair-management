# Copyright (c) 2023, Vijatshi Software and contributors
# For license information, please see license.txt

# import frappe
import frappe
from frappe import _, throw
from frappe.utils import cint, flt, cstr,logger
from frappe.model.document import Document
from erpnext.stock.stock_balance import get_planned_qty, update_bin_qty
from erpnext.manufacturing.doctype.bom.bom import add_additional_cost, validate_bom_no
from erpnext.stock.doctype.item.item import get_item_defaults
from erpnext.stock.utils import get_bin
from pypika import functions as fn
from console import console
import json
class ServiceWorkOrder(Document):
	def onload(self):
		ms = frappe.get_doc("Manufacturing Settings")
		self.set_onload("material_consumption", ms.material_consumption)
		self.set_onload("backflush_raw_materials_based_on", ms.backflush_raw_materials_based_on)
		self.set_onload("overproduction_percentage", ms.overproduction_percentage_for_work_order)

	def update_status(self, status=None):
		"""Update status of work order if unknown"""
		if status != "Stopped" and status != "Closed":
			status = self.get_status(status)

		if status != self.status:
			self.db_set("status", status)

		self.update_required_items()

		return status

	def get_status(self, status=None):
		"""Return the status based on stock entries against this work order"""
		if not status:
			status = self.status

		if self.docstatus == 0:
			status = "Draft"
		elif self.docstatus == 1:
			if status != "Stopped":
				stock_entries = frappe._dict(
					frappe.db.sql(
						"""select purpose, sum(fg_completed_qty)
					from `tabStock Entry` where service_work_order=%s and docstatus=1
					group by purpose""",
						self.name,
					)
				)

				status = "Not Started"
				if stock_entries:
					status = "In Process"
					produced_qty = stock_entries.get("Manufacture")
					if flt(produced_qty) >= flt(self.qty):
						status = "Completed"
		else:
			status = "Cancelled"

		return status
	def update_planned_qty(self,param):
		# update the quantity of all the items in the child table.
		for row in param["stock_entry_doc"].items:
			update_bin_qty(
				row.item_code,
				row.t_warehouse,
				{"planned_qty": get_planned_qty(row.item_code, row.t_warehouse)},
			)

		if self.material_request:
			mr_obj = frappe.get_doc("Material Request", self.material_request)
			mr_obj.update_requested_qty([self.material_request_item])
			
	def update_batch_produced_qty(self, stock_entry_doc):
		if not cint(
			frappe.db.get_single_value("Manufacturing Settings", "make_serial_no_batch_from_work_order")
		):
			return

		for row in stock_entry_doc.items:
			if row.batch_no and (row.is_finished_item or row.is_scrap_item):
				qty = frappe.get_all(
					"Stock Entry Detail",
					filters={"batch_no": row.batch_no, "docstatus": 1},
					or_filters={"is_finished_item": 1, "is_scrap_item": 1},
					fields=["sum(qty)"],
					as_list=1,
				)[0][0]

				frappe.db.set_value("Batch", row.batch_no, "produced_qty", flt(qty))

	def update_required_items(self):
		"""
		update bin reserved_qty_for_production
		called from Stock Entry for production, after submit, cancel
		"""
		# calculate consumed qty based on submitted stock entries
		self.update_consumed_qty_for_required_items()

		if self.docstatus == 1:
			# calculate transferred qty based on submitted stock entries
			self.update_transferred_qty_for_required_items()
			self.update_returned_qty()

			# update in bin
			self.update_reserved_qty_for_production()

	def update_returned_qty(self):
		ste = frappe.qb.DocType("Stock Entry")
		ste_child = frappe.qb.DocType("Stock Entry Detail")

		query = (
			frappe.qb.from_(ste)
			.inner_join(ste_child)
			.on((ste_child.parent == ste.name))
			.select(
				ste_child.item_code,
				ste_child.original_item,
				fn.Sum(ste_child.qty).as_("qty"),
			)
			.where(
				(ste.docstatus == 1)
				& (ste.service_work_order == self.name)
				& (ste.purpose == "Material Transfer for Manufacture")
				& (ste.is_return == 1)
			)
			.groupby(ste_child.item_code)
		)

		data = query.run(as_dict=1) or []
		returned_dict = frappe._dict({d.original_item or d.item_code: d.qty for d in data})

		for row in self.required_items:
			row.db_set("returned_qty", (returned_dict.get(row.item_code) or 0.0), update_modified=False)

	def update_transferred_qty_for_required_items(self):
		ste = frappe.qb.DocType("Stock Entry")
		ste_child = frappe.qb.DocType("Stock Entry Detail")

		query = (
			frappe.qb.from_(ste)
			.inner_join(ste_child)
			.on((ste_child.parent == ste.name))
			.select(
				ste_child.item_code,
				ste_child.original_item,
				fn.Sum(ste_child.qty).as_("qty"),
			)
			.where(
				(ste.docstatus == 1)
				& (ste.service_work_order == self.name)
				& (ste.purpose == "Material Transfer for Manufacture")
				& (ste.is_return == 0)
			)
			.groupby(ste_child.item_code)
		)

		data = query.run(as_dict=1) or []
		transferred_items = frappe._dict({d.original_item or d.item_code: d.qty for d in data})

		for row in self.required_items:
			row.db_set(
				"transferred_qty", (transferred_items.get(row.item_code) or 0.0), update_modified=False
			)

	def update_consumed_qty_for_required_items(self):
		"""
		Update consumed qty from submitted stock entries
		against a work order for each stock item
		"""

		for item in self.required_items:
			consumed_qty = frappe.db.sql(
				"""
				SELECT
					SUM(qty)
				FROM
					`tabStock Entry` entry,
					`tabStock Entry Detail` detail
				WHERE
					entry.service_work_order = %(name)s
						AND (entry.purpose = "Material Consumption for Manufacture"
							OR entry.purpose = "Manufacture")
						AND entry.docstatus = 1
						AND detail.parent = entry.name
						AND detail.s_warehouse IS NOT null
						AND (detail.item_code = %(item)s
							OR detail.original_item = %(item)s)
				""",
				{"name": self.name, "item": item.item_code},
			)[0][0]

			item.db_set("consumed_qty", flt(consumed_qty), update_modified=False)

	def update_reserved_qty_for_production(self, items=None):
		"""update reserved_qty_for_production in bins"""
		for d in self.required_items:
			if d.source_warehouse:
				stock_bin = get_bin(d.item_code, d.source_warehouse)
				stock_bin.update_reserved_qty_for_production()
	def update_swo_production_items_qty(self,param):
		#logger.info("Inside update_swo_production_items_qty and trying to update the completed qty for items")
		transferred_items = frappe._dict({d.original_item or d.item_code: d.qty for d in param["stock_entry_doc"].items})
		#logger.info(f"transferred items: {transferred_items}")
		for row in self.production_items:
			if flt(row.pending_qty or 0.0) > 0:
				qty= flt(row.completed_qty)+ (transferred_items.get(row.item_code) or 0.0)
				pending_qty=flt(row.pending_qty or 0.0)-flt(qty)
				logger.info(f"transferred item_code {row.item_code}, qty {qty}")
				row.db_set(
					"completed_qty", qty, update_modified=False
				)
				row.db_set(
					"pending_qty", pending_qty, update_modified=False
				)
	def update_service_work_order_qty(self):
		"""Update **Manufactured Qty** and **Material Transferred for Qty** in Service Work Order
		based on Stock Entry"""

		allowance_percentage = flt(
			frappe.db.get_single_value("Manufacturing Settings", "overproduction_percentage_for_work_order")
		)

		for purpose, fieldname in (
			("Manufacture", "produced_qty"),
			("Material Transfer for Manufacture", "material_transferred_for_manufacturing"),
		):
			if (
				purpose == "Material Transfer for Manufacture"
				and self.operations
				and self.transfer_material_against == "Job Card"
			):
				continue

			qty = flt(
				frappe.db.sql(
					"""select sum(fg_completed_qty)
				from `tabStock Entry` where service_work_order=%s and docstatus=1
				and purpose=%s""",
					(self.name, purpose),
				)[0][0]
			)

			completed_qty = self.qty + (allowance_percentage / 100 * self.qty)
			if qty > completed_qty:
				frappe.throw(
					_("{0} ({1}) cannot be greater than planned quantity ({2}) in Work Order {3}").format(
						self.meta.get_label(fieldname), qty, completed_qty, self.name
					),
					StockOverProductionError,
				)

			self.db_set(fieldname, qty)
			#self.set_process_loss_qty()

			#from erpnext.selling.doctype.sales_order.sales_order import update_produced_qty_in_so_item

			#if self.sales_order and self.sales_order_item:
			#	update_produced_qty_in_so_item(self.sales_order, self.sales_order_item)

		#if self.production_plan:
		#	self.update_production_plan_status()
	# def validate_work_order(doc):
	# 	if doc.purpose in (
	# 		"Manufacture",
	# 		"Material Transfer for Manufacture",
	# 		"Material Consumption for Manufacture",
	# 	):
	# 		# check if work order is entered

	# 		if (
	# 			doc.purpose == "Manufacture" or doc.purpose == "Material Consumption for Manufacture"
	# 		) and doc.work_order:
	# 			if not doc.fg_completed_qty:
	# 				frappe.throw(_("For Quantity (Manufactured Qty) is mandatory"))
	# 			doc.check_if_operations_completed()
	# 			doc.check_duplicate_entry_for_work_order()
	# 	elif doc.purpose != "Material Transfer":
	# 		doc.work_order = None
	
	# def validate_work_order(self,stock_entry):
	# 	if stock_entry.purpose in (
	# 		"Manufacture",
	# 		"Material Transfer for Manufacture",
	# 		"Material Consumption for Manufacture",
	# 	):
	# 		# check if work order is entered

	# 		if (
	# 			stock_entry.purpose == "Manufacture" or stock_entry.purpose == "Material Consumption for Manufacture"
	# 		) and stock_entry.work_order:
	# 			if not stock_entry.fg_completed_qty:
	# 				frappe.throw(_("For Quantity (Manufactured Qty) is mandatory"))
	# 			stock_entry.check_if_operations_completed()
	# 			stock_entry.check_duplicate_entry_for_work_order()
	# 	elif stock_entry.purpose != "Material Transfer":
	# 		stock_entry.work_order = None

	# def set_work_order_details(stock_entry):
	# 	if not getattr(stock_entry, "pro_doc", None):
	# 		stock_entry.pro_doc = frappe._dict()

	# 	if stock_entry.service_work_order:
	# 		# common validations
	# 		if not stock_entry.pro_doc:
	# 			stock_entry.pro_doc = frappe.get_doc("Service Work Order", stock_entry.service_work_order)

	# 		if stock_entry.pro_doc:
	# 			stock_entry.bom_no = stock_entry.pro_doc.bom_no
	# 		else:
	# 			# invalid work order
	# 			stock_entry.service_work_order = None

	# def get_items(self,stock_entry):
	# 	stock_entry.set("items", [])
	# 	self.validate_work_order(stock_entry)

	# 	if not stock_entry.posting_date or not stock_entry.posting_time:
	# 		frappe.throw(_("Posting date and posting time is mandatory"))

	# 	self.set_work_order_details(stock_entry)
	# 	stock_entry.flags.backflush_based_on = frappe.db.get_single_value(
	# 		"Manufacturing Settings", "backflush_raw_materials_based_on"
	# 	)

	# 	if stock_entry.bom_no:

	# 		backflush_based_on = frappe.db.get_single_value(
	# 			"Manufacturing Settings", "backflush_raw_materials_based_on"
	# 		)

	# 		if stock_entry.purpose in [
	# 			"Material Issue",
	# 			"Material Transfer",
	# 			"Manufacture",
	# 			"Repack",
	# 			"Send to Subcontractor",
	# 			"Material Transfer for Manufacture",
	# 			"Material Consumption for Manufacture",
	# 		]:

	# 			if stock_entry.service_work_order and stock_entry.purpose == "Material Transfer for Manufacture":
	# 				item_dict = stock_entry.get_pending_raw_materials(backflush_based_on)
	# 				if stock_entry.to_warehouse and stock_entry.pro_doc:
	# 					for item in item_dict.values():
	# 						item["to_warehouse"] = stock_entry.pro_doc.wip_warehouse
	# 				stock_entry.add_to_stock_entry_detail(item_dict)

	# 			elif (
	# 				stock_entry.service_work_order
	# 				and (stock_entry.purpose == "Manufacture" or stock_entry.purpose == "Material Consumption for Manufacture")
	# 				and not stock_entry.pro_doc.skip_transfer
	# 				and stock_entry.flags.backflush_based_on == "Material Transferred for Manufacture"
	# 			):
	# 				stock_entry.add_transfered_raw_materials_in_items()

	# 			elif (
	# 				stock_entry.service_work_order
	# 				and (stock_entry.purpose == "Manufacture" or stock_entry.purpose == "Material Consumption for Manufacture")
	# 				and stock_entry.flags.backflush_based_on == "BOM"
	# 				and frappe.db.get_single_value("Manufacturing Settings", "material_consumption") == 1
	# 			):
	# 				stock_entry.get_unconsumed_raw_materials()

	# 			else:
	# 				if not stock_entry.fg_completed_qty:
	# 					frappe.throw(_("Manufacturing Quantity is mandatory"))

	# 				item_dict = stock_entry.get_bom_raw_materials(stock_entry.fg_completed_qty)

	# 				# Get Subcontract Order Supplied Items Details
	# 				if stock_entry.get(stock_entry.subcontract_data.order_field) and stock_entry.purpose == "Send to Subcontractor":
	# 					# Get Subcontract Order Supplied Items Details
	# 					parent = frappe.qb.DocType(stock_entry.subcontract_data.order_doctype)
	# 					child = frappe.qb.DocType(stock_entry.subcontract_data.order_supplied_items_field)

	# 					item_wh = (
	# 						frappe.qb.from_(parent)
	# 						.inner_join(child)
	# 						.on(parent.name == child.parent)
	# 						.select(child.rm_item_code, child.reserve_warehouse)
	# 						.where(parent.name == stock_entry.get(stock_entry.subcontract_data.order_field))
	# 					).run(as_list=True)

	# 					item_wh = frappe._dict(item_wh)

	# 				for item in item_dict.values():
	# 					if stock_entry.pro_doc and cint(stock_entry.pro_doc.from_wip_warehouse):
	# 						item["from_warehouse"] = stock_entry.pro_doc.wip_warehouse
	# 					# Get Reserve Warehouse from Subcontract Order
	# 					if stock_entry.get(stock_entry.subcontract_data.order_field) and stock_entry.purpose == "Send to Subcontractor":
	# 						item["from_warehouse"] = item_wh.get(item.item_code)
	# 					item["to_warehouse"] = stock_entry.to_warehouse if stock_entry.purpose == "Send to Subcontractor" else ""

	# 				stock_entry.add_to_stock_entry_detail(item_dict)

	# 		# fetch the serial_no of the first stock entry for the second stock entry
	# 		if stock_entry.service_work_order and stock_entry.purpose == "Manufacture":
	# 			service_work_order = frappe.get_doc("Service Work Order", stock_entry.service_work_order)
	# 			add_additional_cost(stock_entry, service_work_order)

	# 		# add finished goods item
	# 		if stock_entry.purpose in ("Manufacture", "Repack"):
	# 			stock_entry.load_items_from_bom()

	# 	stock_entry.set_scrap_items()
	# 	stock_entry.set_actual_qty()
	# 	stock_entry.update_items_for_process_loss()
	# 	stock_entry.validate_customer_provided_item()
	# 	stock_entry.calculate_rate_and_amount(raise_error_if_no_rate=False)

class StockOverProductionError(frappe.ValidationError):
	pass


def validate_work_order(stock_entry):
	if stock_entry.purpose in (
		"Manufacture",
		"Material Transfer for Manufacture",
		"Material Consumption for Manufacture",
	):
		# check if work order is entered

		if (
			stock_entry.purpose == "Manufacture" or stock_entry.purpose == "Material Consumption for Manufacture"
		) and stock_entry.work_order:
			if not stock_entry.fg_completed_qty:
				frappe.throw(_("For Quantity (Manufactured Qty) is mandatory"))
			stock_entry.check_if_operations_completed()
			stock_entry.check_duplicate_entry_for_work_order()
	elif stock_entry.purpose != "Material Transfer":
		stock_entry.work_order = None

def set_work_order_details(stock_entry):
	if not getattr(stock_entry, "pro_doc", None):
		stock_entry.pro_doc = frappe._dict()
	#logger.info(f"Service work order: {stock_entry.service_work_order}")
	if stock_entry.service_work_order:
		# common validations
		if not stock_entry.pro_doc:
			#logger.info(f"Fetching Service Work Order : {stock_entry.service_work_order}")
			stock_entry.pro_doc = frappe.get_doc("Service Work Order", stock_entry.service_work_order)

		if stock_entry.pro_doc:
			stock_entry.bom_no = stock_entry.pro_doc.bom_no
		else:
			# invalid work order
			stock_entry.service_work_order = None
	#logger.info(f"pro_doc_items: {stock_entry.pro_doc.production_items}")

def load_items_from_service_work_order(stock_entry):
	#logger.info(f"Inside load_items_from_service_work_order having items {stock_entry.pro_doc}")
	#stock_entry.fg_completed_qty;
	rem_qty=flt(stock_entry.fg_completed_qty)
	logger.info(f"remaing qty {rem_qty}")
	for entry in stock_entry.pro_doc.production_items:
		logger.info(f"pending qty {flt(entry.pending_qty)} for item {entry.item_code}")
		if flt(entry.pending_qty)==0:
			continue;
		pending_qty=flt(entry.pending_qty)
		qty=0;
		if rem_qty >= pending_qty:
			qty = pending_qty
		else:
			qty = rem_qty
		if qty==0:
			break	
		rem_qty-=qty 	
		item_code = entry.item_code
		to_warehouse = stock_entry.pro_doc.fg_warehouse
		item = get_item_defaults(item_code, stock_entry.company)

		if not stock_entry.service_work_order and not to_warehouse:
			# in case of BOM
			to_warehouse = item.get("default_warehouse")

		args = {
			"to_warehouse": to_warehouse,
			"from_warehouse": "",
			"qty": qty,
			"item_name": item.item_name,
			"description": item.description,
			"stock_uom": item.stock_uom,
			"expense_account": item.get("expense_account"),
			"cost_center": item.get("buying_cost_center"),
			"is_finished_item": 1,
		}

		if (
			stock_entry.service_work_order
			and stock_entry.pro_doc.has_batch_no
			and cint(
				frappe.db.get_single_value(
					"Manufacturing Settings", "make_serial_no_batch_from_work_order", cache=True
				)
			)
		):
			stock_entry.set_batchwise_finished_goods(args, item)
		else:
			stock_entry.add_finished_goods(args, item)

def get_items(stock_entry):
	logger.info("Inside getItems")
	stock_entry.set("items", [])
	validate_work_order(stock_entry)
	logger.info("after validate work order")
	if not stock_entry.posting_date or not stock_entry.posting_time:
		frappe.throw(_("Posting date and posting time is mandatory"))

	set_work_order_details(stock_entry)
	logger.info("after set work order details")
	stock_entry.flags.backflush_based_on = frappe.db.get_single_value(
		"Manufacturing Settings", "backflush_raw_materials_based_on"
	)

	# if stock_entry.bom_no:

	# 	stock_entry.flag.backflush_based_on = frappe.db.get_single_value(
	# 		"Manufacturing Settings", "backflush_raw_materials_based_on"
	# 	)

	# 	if stock_entry.purpose in [
	# 		"Manufacture",
	# 	]:

	# 		if (
	# 			stock_entry.service_work_order
	# 			and (stock_entry.purpose == "Manufacture" or stock_entry.purpose == "Material Consumption for Manufacture")
	# 			and not stock_entry.pro_doc.skip_transfer
	# 			and stock_entry.flags.backflush_based_on == "Material Transferred for Manufacture"
	# 		):
	# 			stock_entry.add_transfered_raw_materials_in_items()

	# 		elif (
	# 			stock_entry.service_work_order
	# 			and (stock_entry.purpose == "Manufacture" or stock_entry.purpose == "Material Consumption for Manufacture")
	# 			and stock_entry.flags.backflush_based_on == "BOM"
	# 			and frappe.db.get_single_value("Manufacturing Settings", "material_consumption") == 1
	# 		):
	# 			stock_entry.get_unconsumed_raw_materials()

	# 		else:
	# 			if not stock_entry.fg_completed_qty:
	# 				frappe.throw(_("Manufacturing Quantity is mandatory"))

	# 			item_dict = stock_entry.get_bom_raw_materials(stock_entry.fg_completed_qty)
	# 			stock_entry.add_to_stock_entry_detail(item_dict)

	# 	# fetch the serial_no of the first stock entry for the second stock entry
	# 	if stock_entry.service_work_order and stock_entry.purpose == "Manufacture":
	# 		service_work_order = frappe.get_doc("Service Work Order", stock_entry.service_work_order)
	# 		add_additional_cost(stock_entry, service_work_order)

		
	# add finished goods item
	if stock_entry.purpose in ("Manufacture", "Repack"):
		load_items_from_service_work_order(stock_entry)
	stock_entry.set_scrap_items()
	stock_entry.set_actual_qty()
	stock_entry.update_items_for_process_loss()
	stock_entry.validate_customer_provided_item()
	stock_entry.calculate_rate_and_amount(raise_error_if_no_rate=False)

#frappe.utils.logger.set_log_level("DEBUG")
#logger = frappe.logger("service work order", allow_site=True, file_count=50)
logger.set_log_level("DEBUG")
logger = frappe.logger("api", allow_site=True, file_count=50)

@frappe.whitelist()
def make_stock_entry(work_order_id, purpose, qty=None):
	work_order = frappe.get_doc("Service Work Order", work_order_id)
	#print(work_order);
	if not frappe.db.get_value("Warehouse", work_order.wip_warehouse, "is_group"):
		wip_warehouse = work_order.wip_warehouse
	else:
		wip_warehouse = None
	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.purpose = purpose
	stock_entry.service_work_order = work_order_id
	stock_entry.company = work_order.company
	stock_entry.from_bom = 1
	stock_entry.stock_entry_type=purpose
	stock_entry.bom_no = work_order.bom_no
	stock_entry.use_multi_level_bom = work_order.use_multi_level_bom
	# accept 0 qty as well
	stock_entry.fg_completed_qty = (
		qty if qty is not None else (flt(work_order.qty) - flt(work_order.produced_qty))
	)
	if work_order.bom_no:
		stock_entry.inspection_required = frappe.db.get_value(
			"BOM", work_order.bom_no, "inspection_required"
		)
	if purpose == "Material Transfer for Manufacture":
		stock_entry.from_warehouse = work_order.source_warehouse
		stock_entry.to_warehouse = wip_warehouse
		stock_entry.project = work_order.project
	else:
		stock_entry.from_warehouse = wip_warehouse
		stock_entry.to_warehouse = work_order.fg_warehouse
		stock_entry.project = work_order.project
		stock_entry.set_stock_entry_type()

	#console(stock_entry.as_dict()).log()
	#frappe.logger("frappe.web").info(stock_entry)
	#console(stock_entry).log()	
	#logger.debug("Hello frappe debug")
	logger.info(f"Create stock entry for Service Work Order: {stock_entry.service_work_order} with purpose={purpose}")
	if(purpose=="Manufacture" and stock_entry.service_work_order):
		#frappe.logger("frappe.web").info(stock_entry)
		#console(stock_entry).log()
		#logger.info(stock_entry)
		#logger.info(stock_entry)
		get_items(stock_entry)
	else:		
		stock_entry.get_items()
	stock_entry.set_serial_no_batch_for_finished_good()
	return stock_entry.as_dict()


	
