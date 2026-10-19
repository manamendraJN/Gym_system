from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.models import ActivityLog, Notification
from apps.expenses import services as expense_services

from .models import Equipment, EquipmentStatus, FaultReport, FaultSeverity, MaintenanceRecord


class EquipmentError(Exception):
    """Safe-to-show message."""


def maintenance_staff():
    from apps.notifications.services import users_with_permission

    return users_with_permission("equipment.manage")


@transaction.atomic
def set_status(equipment, status, user, note=""):
    equipment = Equipment.objects.select_for_update().get(pk=equipment.pk)
    if status not in EquipmentStatus.values:
        raise EquipmentError("Invalid status.")
    if equipment.status == status:
        raise EquipmentError("The equipment already has this status.")
    old = equipment.get_status_display()
    equipment.status = status
    equipment.save(update_fields=["status"])
    ActivityLog.record(user, "equipment_status", f"{equipment.code}: {old} → {equipment.get_status_display()}" + (f" ({note})" if note else ""))
    return equipment


@transaction.atomic
def report_fault(equipment, description, severity, user):
    """Anyone who can see equipment can report a fault. A major fault takes the item out of service immediately."""
    equipment = Equipment.objects.select_for_update().get(pk=equipment.pk)
    if not description.strip():
        raise EquipmentError("Describe the fault.")
    fault = FaultReport.objects.create(equipment=equipment, description=description.strip(), severity=severity, reported_by=user)
    if severity == FaultSeverity.MAJOR and equipment.status in (EquipmentStatus.AVAILABLE, EquipmentStatus.IN_USE):
        equipment.status = EquipmentStatus.OUT_OF_SERVICE
        equipment.save(update_fields=["status"])
    ActivityLog.record(user, "equipment_fault", f"Fault reported on {equipment.code} ({fault.get_severity_display()})")
    for staff in maintenance_staff():
        if user is None or staff.pk != user.pk:
            Notification.send(staff, "Equipment fault reported", f"{equipment.code} {equipment.name}: {description.strip()[:150]}",
                              equipment.get_absolute_url(), kind="equipment-fault", dedupe_key=f"equipment-fault:{fault.pk}")
    return fault


@transaction.atomic
def record_maintenance(equipment, user, *, kind, performed_on, description, cost=0, technician="", supplier=None, fault=None,
                       next_due=None, condition_after="", return_to_service=False, record_as_expense=True):
    """Log a service or repair. In one transaction this: stores the record, creates ONE linked Maintenance expense when
    there is a cost (and record_as_expense), updates last/next maintenance dates, optionally returns the item to service
    and resolves the linked fault."""
    equipment = Equipment.objects.select_for_update().get(pk=equipment.pk)
    today = timezone.localdate()
    if performed_on > today:
        raise EquipmentError("The date cannot be in the future.")
    if cost < 0:
        raise EquipmentError("The cost cannot be negative.")
    if fault is not None and fault.equipment_id != equipment.pk:
        raise EquipmentError("That fault belongs to another item.")
    if fault is not None and not fault.is_open:
        raise EquipmentError("That fault is already resolved.")
    if next_due is not None and next_due <= performed_on:
        raise EquipmentError("The next maintenance date must be after the service date.")
    record = MaintenanceRecord.objects.create(
        equipment=equipment, kind=kind, performed_on=performed_on, description=description.strip(), cost=cost, technician=technician,
        supplier=supplier, fault=fault, next_due=next_due, condition_after=condition_after, recorded_by=user)
    if cost > 0 and record_as_expense:
        expense_services.record_expense(
            expense_services.category("Maintenance"), cost, performed_on, f"{equipment.code} {equipment.name}: {description.strip()}"[:255], user,
            supplier=supplier, maintenance_record=record)
    if equipment.last_maintenance_date is None or performed_on >= equipment.last_maintenance_date:
        equipment.last_maintenance_date = performed_on
        if next_due:
            equipment.next_maintenance_date = next_due
        elif equipment.maintenance_interval_days:
            equipment.next_maintenance_date = performed_on + timedelta(days=equipment.maintenance_interval_days)
        elif equipment.next_maintenance_date and equipment.next_maintenance_date <= performed_on:
            equipment.next_maintenance_date = None  # the scheduled service has now been done
    if condition_after:
        equipment.condition = condition_after
    if return_to_service:
        equipment.status = EquipmentStatus.AVAILABLE
    if fault is not None:
        fault.resolved_at, fault.resolved_by = timezone.now(), user
        fault.save(update_fields=["resolved_at", "resolved_by"])
    equipment.save()
    ActivityLog.record(user, "equipment_maintenance", f"{record.get_kind_display()} on {equipment.code}" + (f", LKR {cost:,.2f}" if cost else ""))
    return record
