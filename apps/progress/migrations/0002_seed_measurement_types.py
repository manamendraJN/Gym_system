from django.db import migrations

DEFAULTS = [("Body fat", "%"), ("Waist", "cm"), ("Chest", "cm"), ("Hips", "cm"), ("Arm", "cm"), ("Thigh", "cm")]


def seed(apps, schema_editor):
    MeasurementType = apps.get_model("progress", "MeasurementType")
    for position, (name, unit) in enumerate(DEFAULTS):
        MeasurementType.objects.get_or_create(name=name, defaults={"unit": unit, "position": position})


class Migration(migrations.Migration):
    dependencies = [("progress", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
