from django.db import migrations

# Frozen copy of the defaults introduced in Phase 4.
ALL_TRAINING = ["trainers.view", "appointments.view", "appointments.manage", "workouts.view", "workouts.manage",
                "progress.view", "progress.manage", "nutrition.view", "nutrition.manage"]
NEW_DEFAULTS = {
    "manager": ALL_TRAINING + ["trainers.manage", "trainers.compensation"],
    "receptionist": ["trainers.view", "appointments.view", "appointments.manage"],
    "trainer": ALL_TRAINING,
    "accountant": ["trainers.view", "trainers.compensation"],
}


def seed(apps, schema_editor):
    RolePermission = apps.get_model("accounts", "RolePermission")
    RolePermission.objects.bulk_create(
        [RolePermission(role=r, permission=p) for r, perms in NEW_DEFAULTS.items() for p in perms],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_phase3_permissions")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
