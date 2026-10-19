from django.db import migrations

NEW_DEFAULTS = {"manager": ["settings.manage", "audit.view"]}


def seed(apps, schema_editor):
    RolePermission = apps.get_model("accounts", "RolePermission")
    RolePermission.objects.bulk_create(
        [RolePermission(role=r, permission=p) for r, perms in NEW_DEFAULTS.items() for p in perms], ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_phase5_permissions")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
