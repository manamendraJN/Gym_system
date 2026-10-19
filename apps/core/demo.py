"""Demo data for local development and automated end-to-end checks.

Everything is created through the real service layer (so business rules, ledgers, audit entries and notifications are
produced exactly as in normal use). It only ADDS records – it never modifies or deletes existing data – and refuses to
run twice."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

DEMO_PASSWORD = "DemoPass-12345!"


def _aware(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())


def seed_demo(actor, today=None):
    """Create demo data on behalf of `actor` (a staff user with full rights). Returns a dict of the main objects, or None
    if demo data already exists."""
    from apps.accounts.models import Role, User
    from apps.announcements.models import Announcement
    from apps.appointments import services as appointments
    from apps.attendance import services as attendance
    from apps.equipment import services as equipment
    from apps.equipment.models import Equipment
    from apps.expenses import services as expenses
    from apps.inventory import services as inventory
    from apps.inventory.models import InventoryItem
    from apps.members.models import Member
    from apps.memberships import services as memberships
    from apps.memberships.models import Membership, MembershipPlan
    from apps.notifications.services import run_all
    from apps.nutrition.models import MealItem, MealPlan, NutritionProfile
    from apps.payments import services as payments
    from apps.progress.models import ProgressRecord
    from apps.suppliers.models import Supplier
    from apps.trainers import services as trainers
    from apps.trainers.models import Trainer, TrainerAvailability
    from apps.workouts import services as workouts
    from apps.workouts.models import Exercise, PlanExercise, WorkoutPlan

    today = today or timezone.localdate()
    if Member.objects.filter(full_name__startswith="Demo ").exists():
        return None

    # --- plans -----------------------------------------------------------------------------------------------
    monthly = MembershipPlan.objects.create(name="Demo Monthly", price=Decimal("5000"), benefits="Gym floor\nLockers", description="Standard monthly plan")
    MembershipPlan.objects.create(name="Demo Quarterly", duration_type="quarterly", price=Decimal("13500"))
    MembershipPlan.objects.create(name="Demo Off-peak", price=Decimal("3500"), allowed_weekdays="0,1,2,3,4", access_from=time(9), access_until=time(17))

    # --- members ---------------------------------------------------------------------------------------------
    def member(n, name, **kw):
        return Member.objects.create(
            full_name=f"Demo {name}", phone=f"07790{n:05d}", email=f"demo.{name.lower()}@example.com", date_of_birth=today - timedelta(days=365 * 30),
            emergency_contact_name="Demo Contact", emergency_contact_phone=f"07791{n:05d}", registration_date=today - timedelta(days=30 - n), **kw)

    alice, bob, cara, dan, eve = (member(i, n) for i, n in enumerate(("Alice", "Bob", "Cara", "Dan", "Eve"), start=1))
    for m in (alice, bob):
        login = User.objects.create_user(f"demo_{m.full_name.split()[1].lower()}", m.email, DEMO_PASSWORD, role=Role.MEMBER, first_name=m.full_name.split()[1])
        m.user = login
        m.save(update_fields=["user"])

    # --- memberships & payments ------------------------------------------------------------------------------
    m_alice = memberships.assign_membership(alice, monthly, today, actor)
    m_bob = memberships.assign_membership(bob, monthly, today, actor)
    m_eve = memberships.assign_membership(eve, monthly, today, actor)
    memberships.suspend_membership(m_eve, actor, "Demo: travelling")
    Membership.objects.create(member=cara, plan=monthly, start_date=today - timedelta(days=25), end_date=today + timedelta(days=5), price=monthly.price)
    Membership.objects.create(member=dan, plan=monthly, start_date=today - timedelta(days=50), end_date=today - timedelta(days=20), price=monthly.price)
    paid, _ = payments.record_payment(m_alice, Decimal("5000"), "cash", actor, "demo-a1")
    payments.refund_payment(paid, Decimal("500"), "cash", actor, "demo-a2", "Demo goodwill refund")
    payments.record_payment(m_bob, Decimal("2000"), "bank_transfer", actor, "demo-b1", external_reference="SLIP-DEMO")
    payments.record_payment(m_bob, Decimal("1000"), "bank_transfer", actor, "demo-b2", pending=True)
    payments.adjust_charge(m_bob, Decimal("-250"), actor, "demo-b3", "Demo loyalty discount")

    # --- trainer, availability, assignments, sessions ---------------------------------------------------------
    coach_user = User.objects.create_user("demo_trainer", "demo.trainer@example.com", DEMO_PASSWORD, role=Role.TRAINER, first_name="Demo")
    coach = Trainer.objects.create(full_name="Demo Coach", phone="0771110000", user=coach_user, hourly_rate=Decimal("3000"), specialties="Strength, Mobility",
                                   qualifications="Level 3 Personal Trainer")
    for weekday in range(7):
        TrainerAvailability.objects.create(trainer=coach, weekday=weekday, start_time=time(6), end_time=time(20))
    trainers.assign_trainer(coach, alice, actor)
    trainers.assign_trainer(coach, bob, actor)
    upcoming = appointments.book(alice, coach, _aware(today + timedelta(days=3), 10), 60, actor)
    payments.record_payment(upcoming, Decimal("1000"), "cash", actor, "demo-s1")
    cancelled = appointments.book(bob, coach, _aware(today + timedelta(days=4), 11), 60, actor)
    appointments.cancel(cancelled, actor, "Demo cancellation")
    from apps.appointments.models import Appointment

    Appointment.objects.create(member=alice, trainer=coach, start=_aware(today - timedelta(days=2), 9), end=_aware(today - timedelta(days=2), 10),
                               status="completed", fee=Decimal("3000"))

    # --- workouts, progress, nutrition -----------------------------------------------------------------------
    squat = Exercise.objects.create(name="Demo Squat", muscle_group="legs", instructions="Keep your chest up.", video_url="https://example.com/squat")
    press = Exercise.objects.create(name="Demo Bench Press", muscle_group="chest")
    plan = WorkoutPlan.objects.create(member=alice, trainer=coach, name="Demo Strength Plan", goal="Build strength", created_by=actor)
    item = PlanExercise.objects.create(plan=plan, exercise=squat, weekday=0, sets=4, reps=8, weight_kg=Decimal("60"), rest_seconds=90)
    PlanExercise.objects.create(plan=plan, exercise=press, weekday=2, sets=3, reps=10)
    workouts.log_session(alice, plan, today, [{"item": item, "sets": 4, "reps": 8, "weight": Decimal("62.5"), "duration": None}], actor)
    ProgressRecord.objects.create(member=alice, weight_kg=Decimal("68.5"), height_cm=Decimal("170"), notes="Demo start", recorded_by=actor)
    NutritionProfile.objects.create(member=alice, food_preferences="Vegetarian", allergies="Peanuts", nutrition_goals="More fibre")
    meals = MealPlan.objects.create(member=alice, name="Demo Meal Plan", goal="Balanced eating", created_by=actor)
    MealItem.objects.create(plan=meals, weekday=0, meal_type="breakfast", title="Oats and fruit", ingredients="Oats\nBanana")

    # --- equipment, inventory, expenses, suppliers -----------------------------------------------------------
    supplier = Supplier.objects.create(name="Demo Supplier", phone="0112000000")
    treadmill = Equipment.objects.create(name="Demo Treadmill", category="cardio", location="Floor 1", supplier=supplier, purchase_cost=Decimal("250000"),
                                         maintenance_interval_days=90, next_maintenance_date=today + timedelta(days=5), warranty_expires=today + timedelta(days=200))
    fault = equipment.report_fault(treadmill, "Demo: belt slips", "minor", actor)
    equipment.record_maintenance(treadmill, actor, kind="repair", performed_on=today, description="Demo belt tension", cost=Decimal("1200"), supplier=supplier, fault=fault)
    towels = InventoryItem.objects.create(name="Demo Towels", min_stock=10, supplier=supplier, category="towels")
    inventory.move_stock(towels, "initial", 25, actor, reason="Opening stock")
    inventory.move_stock(towels, "purchase", 5, actor, unit_cost=Decimal("300"), supplier=supplier, record_expense=True)
    inventory.move_stock(towels, "usage", 22, actor, reason="Laundry loss")
    from apps.expenses.models import ExpenseCategory

    expenses.record_expense(ExpenseCategory.objects.get(name="Rent"), Decimal("45000"), today, "Demo monthly rent", actor, supplier=supplier)

    # --- attendance, announcements, notifications -------------------------------------------------------------
    attendance.check_in(alice, actor)
    Announcement.objects.create(title="Demo: holiday hours", body="The gym closes at 6pm on public holidays.", kind="operational", audience="everyone", created_by=actor)
    run_all()
    return {"alice": alice, "bob": bob, "cara": cara, "dan": dan, "eve": eve, "coach": coach, "plan": plan, "membership": m_alice,
            "appointment": upcoming, "equipment": treadmill, "item": towels}
