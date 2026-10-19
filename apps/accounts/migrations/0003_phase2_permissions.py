from django.db import migrations

# Frozen copy of the defaults introduced in Phase 2 (existing databases were seeded before these existed).
NEW_DEFAULTS = {
    "manager": ["plans.view", "plans.manage", "plans.price", "memberships.view", "memberships.manage", "memberships.cancel"],
    "receptionist": ["plans.view", "memberships.view", "memberships.manage"],
    "trainer": ["members.view", "plans.view", "memberships.view"],
    "accountant": ["members.view", "plans.view", "memberships.view"],
}


def seed(apps, schema_editor):
    RolePermission = apps.get_model("accounts", "RolePermission")
    RolePermission.objects.bulk_create(
        [RolePermission(role=r, permission=p) for r, perms in NEW_DEFAULTS.items() for p in perms],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_seed_role_permissions")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
