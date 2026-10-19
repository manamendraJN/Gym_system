from django.db import transaction

from apps.core.models import ActivityLog

from .models import ProgressMeasurement, ProgressRecord


@transaction.atomic
def add_record(member, form, user, source):
    c = form.cleaned_data
    record = ProgressRecord.objects.create(
        member=member, recorded_on=c["recorded_on"], weight_kg=c.get("weight_kg"), height_cm=c.get("height_cm"),
        notes=c.get("notes", "").strip(), source=source, recorded_by=user)
    ProgressMeasurement.objects.bulk_create([ProgressMeasurement(record=record, type=t, value=v) for t, v in form.values().items()])
    ActivityLog.record(user, "progress_recorded", f"Progress recorded for {member.full_name}")
    return record


def chart_data(member):
    """Series for the charts: oldest to newest, with None where a value was not recorded."""
    records = list(member.progress_records.prefetch_related("measurements__type").order_by("recorded_on", "id"))
    labels = [r.recorded_on.strftime("%b %d, %Y") for r in records]
    types = {}
    for r in records:
        for m in r.measurements.all():
            types.setdefault(m.type_id, m.type)
    measurements = []
    for tid, t in types.items():
        series = []
        for r in records:
            value = next((m.value for m in r.measurements.all() if m.type_id == tid), None)
            series.append(float(value) if value is not None else None)
        measurements.append({"label": f"{t.name} ({t.unit})", "values": series})
    return {
        "labels": labels,
        "weight": [float(r.weight_kg) if r.weight_kg is not None else None for r in records],
        "measurements": measurements,
    }
