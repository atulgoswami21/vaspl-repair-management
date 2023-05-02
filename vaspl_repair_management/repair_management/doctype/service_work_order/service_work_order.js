// Copyright (c) 2023, Vijatshi Software and contributors
// For license information, please see license.txt
frappe.provide("vaspl_repair_management");
frappe.ui.form.on('Service Work Order', {
	 refresh: function(frm) {
		// if (!frm.doc.__islocal) return;
        // frappe.call({
		// 	method: "vaspl_repair_management.custom_methods.generate_unique_naming",
		// 	args: {
		// 		"doctype": frm.doc.doctype,
		// 		"series": "VASPL-WO",
		// 		"field": "name",
		// 		"app": "vaspl_repair_management",
		// 		"module": "repair_management"
		// 	},callback: function(r) {
        //         if (r.message) {
        //             frm.set_value("name", r.message);
        //         }
        //     }
        // });
		vaspl_repair_management.service_work_order.set_custom_buttons(frm);
		frm.set_intro("");

		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.set_intro(__("Submit this Service Work Order for further processing."));
		} else {
			frm.trigger("show_progress_for_items");
			//frm.trigger("show_progress_for_operations");
		}
	},
	show_progress_for_items: function(frm) {
		var bars = [];
		var message = '';
		var added_min = false;

		// produced qty
		var title = __('{0} items produced', [frm.doc.produced_qty]);
		bars.push({
			'title': title,
			'width': (frm.doc.produced_qty / frm.doc.qty * 100) + '%',
			'progress_class': 'progress-bar-success'
		});
		if (bars[0].width == '0%') {
			bars[0].width = '0.5%';
			added_min = 0.5;
		}
		message = title;
		// pending qty
		if(!frm.doc.skip_transfer){
			var pending_complete = frm.doc.material_transferred_for_manufacturing - frm.doc.produced_qty;
			if(pending_complete) {
				var width = ((pending_complete / frm.doc.qty * 100) - added_min);
				title = __('{0} items in progress', [pending_complete]);
				bars.push({
					'title': title,
					'width': (width > 100 ? "99.5" : width)  + '%',
					'progress_class': 'progress-bar-warning'
				});
				message = message + '. ' + title;
			}
		}
		frm.dashboard.add_progress(__('Status'), bars, message);
	},

	show_progress_for_operations: function(frm) {
		if (frm.doc.operations && frm.doc.operations.length) {

			let progress_class = {
				"Work in Progress": "progress-bar-warning",
				"Completed": "progress-bar-success"
			};

			let bars = [];
			let message = '';
			let title = '';
			let status_wise_oprtation_data = {};
			let total_completed_qty = frm.doc.qty * frm.doc.operations.length;

			frm.doc.operations.forEach(d => {
				if (!status_wise_oprtation_data[d.status]) {
					status_wise_oprtation_data[d.status] = [d.completed_qty, d.operation];
				} else {
					status_wise_oprtation_data[d.status][0] += d.completed_qty;
					status_wise_oprtation_data[d.status][1] += ', ' + d.operation;
				}
			});

			for (let key in status_wise_oprtation_data) {
				title = __("{0} Operations: {1}", [key, status_wise_oprtation_data[key][1].bold()]);
				bars.push({
					'title': title,
					'width': status_wise_oprtation_data[key][0] / total_completed_qty * 100  + '%',
					'progress_class': progress_class[key]
				});

				message += title + '. ';
			}

			frm.dashboard.add_progress(__('Status'), bars, message);
		}
	}
});
//vaspl_repair_management={};
vaspl_repair_management.generate_unique_naming = function(doc, args) {
    return frappe.call({
        method: "vaspl_repair_management.custom_methods.generate_unique_naming",
        args: {
            "doctype": doc.doctype,
            "series": "VASPL-WO",
            "field": "name",
            "app": args.app,
            "module": args.module
        }
    });
};
vaspl_repair_management.service_work_order = {
	set_custom_buttons: function(frm) {
		var doc = frm.doc;

		if (doc.status !== "Closed") {
			frm.add_custom_button(__('Close'), function() {
				frappe.confirm(__("Once the Service Work Order is Closed. It can't be resumed."),
					() => {
						vaspl_repair_management.service_work_order.change_work_order_status(frm, "Closed");
					}
				);
			}, __("Status"));
		}

		if (doc.docstatus === 1 && !in_list(["Closed", "Completed"], doc.status)) {
			if (doc.status != 'Stopped' && doc.status != 'Completed') {
				frm.add_custom_button(__('Stop'), function() {
					vaspl_repair_management.service_work_order.change_work_order_status(frm, "Stopped");
				}, __("Status"));
			} else if (doc.status == 'Stopped') {
				frm.add_custom_button(__('Re-open'), function() {
					vaspl_repair_management.service_work_order.change_work_order_status(frm, "Resumed");
				}, __("Status"));
			}

			const show_start_btn = (frm.doc.skip_transfer
				|| frm.doc.transfer_material_against == 'Job Card') ? 0 : 1;

			if (show_start_btn) {
				let pending_to_transfer = frm.doc.required_items.some(
					item => flt(item.transferred_qty) < flt(item.required_qty)
				);
				let pending_to_produce = frm.doc.production_items.some(
					item => flt(item.pending_qty) >0
				);
				if ((pending_to_transfer || pending_to_produce) && frm.doc.status != 'Stopped') {
					frm.has_start_btn = true;
					frm.add_custom_button(__('Create Pick List'), function() {
						vaspl_repair_management.service_work_order.create_pick_list(frm);
					});
					var start_btn = frm.add_custom_button(__('Start'), function() {
						vaspl_repair_management.service_work_order.make_se(frm, 'Material Transfer for Manufacture');
					});
					start_btn.addClass('btn-primary');
				}
			}

			if (frm.doc.status != 'Stopped') {
				// If "Material Consumption is check in Manufacturing Settings, allow Material Consumption
				if (frm.doc.__onload && frm.doc.__onload.material_consumption == 1) {
					if (flt(doc.material_transferred_for_manufacturing) > 0 || frm.doc.skip_transfer) {
						// Only show "Material Consumption" when required_qty > consumed_qty
						var counter = 0;
						var tbl = frm.doc.required_items || [];
						var tbl_lenght = tbl.length;
						for (var i = 0, len = tbl_lenght; i < len; i++) {
							let wo_item_qty = frm.doc.required_items[i].transferred_qty || frm.doc.required_items[i].required_qty;
							if (flt(wo_item_qty) > flt(frm.doc.required_items[i].consumed_qty)) {
								counter += 1;
							}
						}
						if (counter > 0) {
							var consumption_btn = frm.add_custom_button(__('Material Consumption'), function() {
								const backflush_raw_materials_based_on = frm.doc.__onload.backflush_raw_materials_based_on;
								vaspl_repair_management.service_work_order.make_consumption_se(frm, backflush_raw_materials_based_on);
							});
							consumption_btn.addClass('btn-primary');
						}
					}
				}

				if(!frm.doc.skip_transfer){
					if (flt(doc.material_transferred_for_manufacturing) > 0) {
						if ((flt(doc.produced_qty) < flt(doc.material_transferred_for_manufacturing))) {
							frm.has_finish_btn = true;

							var finish_btn = frm.add_custom_button(__('Finish'), function() {
								vaspl_repair_management.service_work_order.make_se(frm, 'Manufacture');
							});

							if(doc.material_transferred_for_manufacturing>=doc.qty) {
								// all materials transferred for manufacturing, make this primary
								finish_btn.addClass('btn-primary');
							}
						} else {
							frappe.db.get_doc("Manufacturing Settings").then((doc) => {
								let allowance_percentage = doc.overproduction_percentage_for_work_order;

								if (allowance_percentage > 0) {
									let allowed_qty = frm.doc.qty + ((allowance_percentage / 100) * frm.doc.qty);

									if ((flt(doc.produced_qty) < allowed_qty)) {
										frm.add_custom_button(__('Finish'), function() {
											vaspl_repair_management.service_work_order.make_se(frm, 'Manufacture');
										});
									}
								}
							});
						}
					}
				} else {
					if ((flt(doc.produced_qty) < flt(doc.qty))) {
						var finish_btn = frm.add_custom_button(__('Finish'), function() {
							vaspl_repair_management.service_work_order.make_se(frm, 'Manufacture');
						});
						finish_btn.addClass('btn-primary');
					}
				}
			}
		}
	},
	calculate_cost: function(doc) {
		if (doc.operations){
			var op = doc.operations;
			doc.planned_operating_cost = 0.0;
			for(var i=0;i<op.length;i++) {
				var planned_operating_cost = flt(flt(op[i].hour_rate) * flt(op[i].time_in_mins) / 60, 2);
				frappe.model.set_value('Work Order Operation', op[i].name,
					"planned_operating_cost", planned_operating_cost);
				doc.planned_operating_cost += planned_operating_cost;
			}
			refresh_field('planned_operating_cost');
		}
	},

	calculate_total_cost: function(frm) {
		let variable_cost = flt(frm.doc.actual_operating_cost) || flt(frm.doc.planned_operating_cost);
		frm.set_value("total_operating_cost", (flt(frm.doc.additional_operating_cost) + variable_cost));
	},

	set_default_warehouse: function(frm) {
		if (!(frm.doc.wip_warehouse || frm.doc.fg_warehouse)) {
			frappe.call({
				method: "vaspl_repair_management.repair_management.doctype.service_work_order.service_work_order.get_default_warehouse",
				callback: function(r) {
					if (!r.exe) {
						frm.set_value("wip_warehouse", r.message.wip_warehouse);
						frm.set_value("fg_warehouse", r.message.fg_warehouse);
						frm.set_value("scrap_warehouse", r.message.scrap_warehouse);
					}
				}
			});
		}
	},

	get_max_transferable_qty: (frm, purpose) => {
		let max = 0;
		if (frm.doc.skip_transfer) {
			max = flt(frm.doc.qty) - flt(frm.doc.produced_qty);
		} else {
			if (purpose === 'Manufacture') {
				max = flt(frm.doc.material_transferred_for_manufacturing) - flt(frm.doc.produced_qty);
			} else {
				max = flt(frm.doc.qty) - flt(frm.doc.material_transferred_for_manufacturing);
			}
		}
		return flt(max, precision('qty'));
	},

	show_prompt_for_qty_input: function(frm, purpose) {
		let max = this.get_max_transferable_qty(frm, purpose);
		return new Promise((resolve, reject) => {
			frappe.prompt({
				fieldtype: 'Float',
				label: __('Qty for {0}', [purpose]),
				fieldname: 'qty',
				description: __('Max: {0}', [max]),
				default: max
			}, data => {
				//max += (frm.doc.qty * (frm.doc.__onload.overproduction_percentage || 0.0)) / 100;
				if (data.qty > max) {
					frappe.msgprint(__('Quantity must not be more than {0}', [max]));
					reject();
				}
				data.purpose = purpose;
				resolve(data);
			}, __('Select Quantity'), __('Create'));
		});
	},

	make_se: function(frm, purpose) {
		this.show_prompt_for_qty_input(frm, purpose)
			.then(data => {
				return frappe.xcall('vaspl_repair_management.repair_management.doctype.service_work_order.service_work_order.make_stock_entry', {
					'work_order_id': frm.doc.name,
					'purpose': purpose,
					'qty': data.qty
				});
			}).then(stock_entry => {
				frappe.model.sync(stock_entry);
				frappe.set_route('Form', stock_entry.doctype, stock_entry.name);
			});

	},

	create_pick_list: function(frm, purpose='Material Transfer for Manufacture') {
		this.show_prompt_for_qty_input(frm, purpose)
			.then(data => {
				return frappe.xcall('vaspl_repair_management.repair_management.doctype.service_work_order.service_work_order.create_pick_list', {
					'source_name': frm.doc.name,
					'for_qty': data.qty
				});
			}).then(pick_list => {
				frappe.model.sync(pick_list);
				frappe.set_route('Form', pick_list.doctype, pick_list.name);
			});
	},

	make_consumption_se: function(frm, backflush_raw_materials_based_on) {
		if(!frm.doc.skip_transfer){
			var max = (backflush_raw_materials_based_on === "Material Transferred for Manufacture") ?
				flt(frm.doc.material_transferred_for_manufacturing) - flt(frm.doc.produced_qty) :
				flt(frm.doc.qty) - flt(frm.doc.produced_qty);
				// flt(frm.doc.qty) - flt(frm.doc.material_transferred_for_manufacturing);
		} else {
			var max = flt(frm.doc.qty) - flt(frm.doc.produced_qty);
		}

		frappe.call({
			method:"vaspl_repair_management.repair_management.doctype.service_work_order.service_work_order.make_stock_entry",
			args: {
				"work_order_id": frm.doc.name,
				"purpose": "Material Consumption for Manufacture",
				"qty": max
			},
			callback: function(r) {
				var doclist = frappe.model.sync(r.message);
				frappe.set_route("Form", doclist[0].doctype, doclist[0].name);
			}
		});
	},

	change_work_order_status: function(frm, status) {
		let method_name = status=="Closed" ? "close_work_order" : "stop_unstop";
		frappe.call({
			method: `vaspl_repair_management.repair_management.doctype.service_work_order.service_work_order.${method_name}`,
			freeze: true,
			freeze_message: __("Updating Work Order status"),
			args: {
				work_order: frm.doc.name,
				status: status
			},
			callback: function(r) {
				if(r.message) {
					frm.set_value("status", r.message);
					frm.reload_doc();
				}
			}
		});
	}
};
