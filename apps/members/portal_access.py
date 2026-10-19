import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from apps.accounts.forms import StyledPasswordResetForm
from apps.accounts.models import Role

User = get_user_model()


class PortalAccessError(Exception):
    """Safe-to-show message."""


@transaction.atomic
def create_portal_login(member, request):
    if member.user_id:
        raise PortalAccessError("This member already has a portal login.")
    if not member.email:
        raise PortalAccessError("Add an email address to the member first; the password-setup link is sent there.")
    if User.objects.filter(email__iexact=member.email).exists():
        raise PortalAccessError("An account with this email already exists. Link it instead.")
    username = member.member_id.lower()
    if User.objects.filter(username=username).exists():
        raise PortalAccessError("That username is already taken.")
    name = member.full_name.split(None, 1)
    user = User.objects.create_user(
        username=username, email=member.email, password=secrets.token_urlsafe(32),  # unknown; the member sets their own
        role=Role.MEMBER, first_name=name[0][:150], last_name=(name[1] if len(name) > 1 else "")[:150], phone=member.phone[:20])
    member.user = user
    member.save(update_fields=["user", "updated_at"])
    form = StyledPasswordResetForm({"email": member.email})
    if form.is_valid():
        form.save(request=request, use_https=request.is_secure(), extra_email_context={"GYM_NAME": settings.GYM_NAME},
                  email_template_name="registration/password_reset_email.txt",
                  subject_template_name="registration/password_reset_subject.txt")
    return user


@transaction.atomic
def link_portal_user(member, user_id):
    if member.user_id:
        raise PortalAccessError("This member already has a portal login.")
    user = User.objects.filter(pk=user_id if str(user_id).isdigit() else 0, role=Role.MEMBER, is_active=True).first()
    if user is None:
        raise PortalAccessError("Choose a member account.")
    try:
        member.user = user
        member.save(update_fields=["user", "updated_at"])
    except IntegrityError:
        raise PortalAccessError("That account is already linked to another member.")
    return user


def unlink_portal_user(member):
    if not member.user_id:
        raise PortalAccessError("This member has no portal login.")
    member.user = None
    member.save(update_fields=["user", "updated_at"])
