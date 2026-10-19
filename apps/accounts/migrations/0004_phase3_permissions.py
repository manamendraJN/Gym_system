from django.db import migrations

# Frozen copy of the defaults introduced in Phase 3.
NEW_DEFAULTS = {
    "manager": ["payments.refund", "payments.adjust"],
    "accountant": ["payments.refund", "payments.adjust"],
    "receptionist": ["payments.manage"],
}


def seed(apps, schema_editor):
    RolePermission = apps.get_model("accounts", "RolePermission")
    RolePermission.objects.bulk_create(
        [RolePermission(role=r, permission=p) for r, perms in NEW_DEFAULTS.items() for p in perms],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_phase2_permissions")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
