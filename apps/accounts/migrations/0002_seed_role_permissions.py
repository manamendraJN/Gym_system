from django.db import migrations

from apps.accounts.permissions import DEFAULT_ROLE_PERMISSIONS


def seed(apps, schema_editor):
    RolePermission = apps.get_model("accounts", "RolePermission")
    RolePermission.objects.bulk_create(
        [RolePermission(role=role, permission=perm) for role, perms in DEFAULT_ROLE_PERMISSIONS.items() for perm in perms],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
