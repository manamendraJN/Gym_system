from django.contrib import admin

from .models import Trainer, TrainerAssignment

admin.site.register(Trainer)


@admin.register(TrainerAssignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("trainer", "member", "start_date", "end_date")
    raw_id_fields = ("member",)
