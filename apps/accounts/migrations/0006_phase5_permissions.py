from django.db import migrations

# Frozen copy of the defaults introduced in Phase 5.
NEW_DEFAULTS = {
    "manager": ["suppliers.view", "suppliers.manage", "equipment.view", "equipment.manage", "inventory.view", "inventory.manage",
                "expenses.view", "expenses.manage", "expenses.void", "announcements.manage", "notifications.configure"],
    "receptionist": ["suppliers.view", "equipment.view", "inventory.view", "inventory.manage"],
    "trainer": ["equipment.view"],
    "accountant": ["suppliers.view", "suppliers.manage", "expenses.view", "expenses.manage", "equipment.view", "inventory.view"],
}


def seed(apps, schema_editor):
    RolePermission = apps.get_model("accounts", "RolePermission")
    RolePermission.objects.bulk_create(
        [RolePermission(role=r, permission=p) for r, perms in NEW_DEFAULTS.items() for p in perms],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_phase4_permissions")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
