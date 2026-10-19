"""Permission registry and role defaults.

Permissions are named "<module>.<action>". Which roles hold which permissions is stored in the
database (RolePermission) and editable by the Super Admin; DEFAULT_ROLE_PERMISSIONS seeds it.
The Super Admin implicitly holds every permission and cannot be restricted.
"""

# Permissions that can be granted to roles (code -> label). Later phases add their own here.
PERMISSIONS = {
    "dashboard.view": "View the administrative dashboard",
    "users.view": "View user accounts",
    "users.manage": "Create, edit and (de)activate user accounts",
    "members.view": "View members",
    "members.manage": "Create and edit members",
    "plans.view": "View membership plans",
    "plans.manage": "Create, edit and deactivate membership plans",
    "plans.price": "Set and change membership plan prices",
    "memberships.view": "View memberships and their history",
    "memberships.manage": "Assign, renew, change, suspend and reactivate memberships",
    "memberships.cancel": "Cancel memberships",
    "attendance.view": "View attendance",
    "attendance.manage": "Record attendance",
    "payments.view": "View payments and invoices",
    "payments.manage": "Record and confirm payments",
    "payments.refund": "Issue refunds",
    "payments.adjust": "Adjust charges and void payments",
    "reports.view": "View reports",
    "trainers.view": "View trainer profiles",
    "trainers.manage": "Manage trainers, assignments and availability",
    "trainers.compensation": "View and record trainer salary / commission",
    "appointments.view": "View training appointments",
    "appointments.manage": "Book, reschedule and cancel training appointments",
    "workouts.view": "View exercises and workout plans",
    "workouts.manage": "Create workout plans and record workouts",
    "progress.view": "View members' fitness progress",
    "progress.manage": "Record fitness progress and manage measurement types",
    "nutrition.view": "View meal plans and nutrition profiles (if the module is enabled)",
    "nutrition.manage": "Manage meal plans and nutrition profiles (if the module is enabled)",
    "suppliers.view": "View suppliers",
    "suppliers.manage": "Create and edit suppliers",
    "equipment.view": "View equipment and report faults",
    "equipment.manage": "Register equipment, record maintenance and repairs",
    "inventory.view": "View inventory and stock history",
    "inventory.manage": "Manage inventory items and record stock movements",
    "expenses.view": "View expenses and expense receipts",
    "expenses.manage": "Record and edit expenses, manage expense categories",
    "expenses.void": "Void expenses",
    "announcements.manage": "Create, edit and deactivate announcements",
    "notifications.configure": "Configure reminder periods and notification channels",
}

# Only the Super Admin holds these; they never appear in the editable matrix.
SUPER_ADMIN_ONLY = {"permissions.manage"}

PERMISSION_GROUPS = [
    ("Dashboard", ["dashboard.view"]),
    ("Users", ["users.view", "users.manage"]),
    ("Members", ["members.view", "members.manage"]),
    ("Membership plans", ["plans.view", "plans.manage", "plans.price"]),
    ("Memberships", ["memberships.view", "memberships.manage", "memberships.cancel"]),
    ("Attendance", ["attendance.view", "attendance.manage"]),
    ("Payments", ["payments.view", "payments.manage", "payments.refund", "payments.adjust"]),
    ("Reports", ["reports.view"]),
    ("Trainers", ["trainers.view", "trainers.manage", "trainers.compensation"]),
    ("Training appointments", ["appointments.view", "appointments.manage"]),
    ("Workouts", ["workouts.view", "workouts.manage"]),
    ("Fitness progress", ["progress.view", "progress.manage"]),
    ("Nutrition", ["nutrition.view", "nutrition.manage"]),
    ("Suppliers", ["suppliers.view", "suppliers.manage"]),
    ("Equipment", ["equipment.view", "equipment.manage"]),
    ("Inventory", ["inventory.view", "inventory.manage"]),
    ("Expenses", ["expenses.view", "expenses.manage", "expenses.void"]),
    ("Announcements & notifications", ["announcements.manage", "notifications.configure"]),
]

DEFAULT_ROLE_PERMISSIONS = {
    "manager": [
        "dashboard.view", "users.view", "users.manage", "members.view", "members.manage",
        "plans.view", "plans.manage", "plans.price", "memberships.view", "memberships.manage",
        "memberships.cancel", "attendance.view", "attendance.manage", "payments.view", "payments.manage", "payments.refund", "payments.adjust",
        "reports.view", "trainers.view", "trainers.manage", "trainers.compensation", "appointments.view",
        "appointments.manage", "workouts.view", "workouts.manage", "progress.view", "progress.manage",
        "nutrition.view", "nutrition.manage", "suppliers.view", "suppliers.manage", "equipment.view", "equipment.manage",
        "inventory.view", "inventory.manage", "expenses.view", "expenses.manage", "expenses.void", "announcements.manage",
        "notifications.configure",
    ],
    "receptionist": [
        "dashboard.view", "members.view", "members.manage", "plans.view", "memberships.view",
        "memberships.manage", "attendance.view", "attendance.manage", "payments.view", "payments.manage",
        "trainers.view", "appointments.view", "appointments.manage",
        "suppliers.view", "equipment.view", "inventory.view", "inventory.manage",
    ],
    "trainer": ["dashboard.view", "members.view", "plans.view", "memberships.view", "attendance.view", "trainers.view",
                "appointments.view", "appointments.manage", "workouts.view", "workouts.manage", "progress.view",
                "progress.manage", "nutrition.view", "nutrition.manage", "equipment.view"],
    "accountant": ["dashboard.view", "members.view", "plans.view", "memberships.view", "payments.view", "payments.manage",
                   "payments.refund", "payments.adjust", "reports.view", "trainers.view", "trainers.compensation",
                   "suppliers.view", "suppliers.manage", "expenses.view", "expenses.manage", "equipment.view", "inventory.view"],
    "member": [],
}
