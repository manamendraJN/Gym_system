"""System customization: branding, theme and layout settings, their permissions, validation and system-wide application."""
import os
import shutil
import tempfile
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.accounts.models import RolePermission, User
from apps.core import appearance as ap
from apps.core.models import ActivityLog, Appearance, GymSettings
from apps.core.testing import PASSWORD, make_users

TMP_MEDIA = None


def image(fmt="PNG", size=(40, 40), name=None):
    buf = BytesIO()
    Image.new("RGB", size, "#336699").save(buf, fmt)
    ext = {"PNG": "png", "JPEG": "jpg", "ICO": "ico", "WEBP": "webp"}[fmt]
    return SimpleUploadedFile(name or f"file.{ext}", buf.getvalue(), content_type=f"image/{ext}")


def form_data(**over):
    preset = ap.PRESETS["graphite"]
    data = {"gym-name": "IronForge Gym", "gym-tagline": "", "login_title": "", "login_message": "", "theme_preset": "graphite", "mode": "light",
            **{f"{c}_color": preset[c] for c in ap.COLOR_FIELDS}, "sidebar_style": "dark", "sidebar_width": "expanded", "density": "comfortable",
            "font_scale": "default", "font_family": "inter", "stat_columns": "4"}
    data.update(over)
    return data


class Base(TestCase):
    def setUp(self):
        global TMP_MEDIA
        TMP_MEDIA = tempfile.mkdtemp()  # a fresh media folder per test, so file counts are exact
        self.addCleanup(shutil.rmtree, TMP_MEDIA, True)
        override = override_settings(MEDIA_ROOT=TMP_MEDIA)
        override.enable()
        self.addCleanup(override.disable)
        self.users = make_users()
        self.url = reverse("core:appearance")
        self.login("super_admin")

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=self.users[role].username, password=PASSWORD))

    def save(self, **over):
        return self.client.post(self.url, form_data(**over))


class PermissionTests(Base):
    def test_only_the_super_admin_by_default_and_never_anonymous(self):
        for role in ("manager", "receptionist", "trainer", "accountant", "member"):
            self.login(role)
            self.assertEqual(self.client.get(self.url).status_code, 403, role)
            self.assertEqual(self.client.post(self.url, form_data(primary_color="#123456")).status_code, 403, role)
            self.assertEqual(self.client.post(reverse("core:appearance_reset")).status_code, 403, role)
            self.assertEqual(self.client.post(reverse("core:appearance_preview"), {}).status_code, 403, role)
        self.assertFalse(Appearance.objects.exists())
        self.client.logout()
        for name in ("core:appearance", "core:appearance_preview", "core:appearance_reset"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)
        self.login("super_admin")
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_permission_can_be_granted_to_another_role(self):
        RolePermission.objects.create(role="manager", permission="settings.appearance")
        self.login("manager")
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertContains(self.client.get("/"), "Customization")  # sidebar link appears only with the permission
        self.login("receptionist")
        self.assertNotContains(self.client.get("/"), "Customization")

    def test_no_account_is_created_or_changed(self):
        before = list(User.objects.order_by("pk").values_list("username", "password", "is_superuser"))
        self.save(primary_color="#2c5f8a", theme_preset="custom")
        self.assertEqual(list(User.objects.order_by("pk").values_list("username", "password", "is_superuser")), before)


class SaveAndApplyTests(Base):
    def test_saved_settings_persist_and_apply_to_every_page_and_the_login_page(self):
        r = self.save(theme_preset="midnight", **{f"{c}_color": ap.PRESETS["midnight"][c] for c in ap.COLOR_FIELDS},
                      login_title="Welcome back", login_message="Train hard.", sidebar_style="light", sidebar_width="compact",
                      density="compact", font_scale="large", stat_columns="3", **{"gym-name": "Atlas Fitness"})
        self.assertRedirects(r, self.url)
        a = Appearance.objects.get()
        self.assertEqual((a.theme_preset, a.sidebar_style, a.density, a.stat_columns, a.updated_by), ("midnight", "light", "compact", "3", self.users["super_admin"]))
        self.assertEqual(GymSettings.objects.get().name, "Atlas Fitness")
        for url in ("/", "/members/", "/payments/", "/plans/"):
            page = self.client.get(url)
            self.assertContains(page, "--gym-accent: #2c5f8a")
            self.assertContains(page, "--sidebar-w: 208px")
            self.assertContains(page, 'data-sidebar="light"')
            self.assertContains(page, 'data-stat-cols="3"')
            self.assertContains(page, "Atlas Fitness")
        self.client.logout()  # public sign-in page uses the same settings, survives logout
        login = self.client.get(reverse("accounts:login"))
        self.assertContains(login, "--gym-accent: #2c5f8a")
        self.assertContains(login, "Welcome back")
        self.assertContains(login, "Train hard.")
        self.assertContains(login, "Atlas Fitness")
        self.assertTrue(ActivityLog.objects.filter(action="appearance_updated").exists())

    def test_defaults_when_nothing_is_saved(self):
        self.client.logout()
        page = self.client.get(reverse("accounts:login"))
        self.assertContains(page, "--gym-accent: #0f766e")
        self.assertContains(page, "Sign in to your account")

    def test_light_dark_and_system_modes(self):
        self.save(mode="dark")
        page = self.client.get("/")
        self.assertContains(page, "--gym-bg: #12161b")
        self.assertContains(page, 'var m="dark"')
        self.assertNotContains(page, "prefers-color-scheme: dark) {")
        self.save(mode="system")
        page = self.client.get("/")
        self.assertContains(page, "@media (prefers-color-scheme: dark)")
        self.assertContains(page, "--gym-bg: #f4f5f6")  # light palette first, dark inside the media query
        self.save(mode="light")
        self.assertNotContains(self.client.get("/"), "#12161b")

    def test_custom_colours_apply_in_light_mode_only_for_neutrals(self):
        self.save(theme_preset="custom", background_color="#eeeeee", surface_color="#fafafa", text_color="#111111", border_color="#cccccc")
        self.assertContains(self.client.get("/"), "--gym-bg: #eeeeee")
        self.save(mode="dark", theme_preset="custom", background_color="#eeeeee")
        page = self.client.get("/")
        self.assertContains(page, "--gym-bg: #12161b")
        self.assertNotContains(page, "--gym-bg: #eeeeee")

    def test_page_renders_with_preview_and_presets(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'id="theme-preview"')
        self.assertContains(page, 'id="preset-data"')
        self.assertContains(page, reverse("core:appearance_preview"))

    def test_every_preset_meets_the_readability_rules(self):
        for key, p in ap.PRESETS.items():
            self.assertGreaterEqual(ap.contrast(p["text"], p["background"]), 4.5, key)
            self.assertGreaterEqual(ap.contrast(p["text"], p["surface"]), 4.5, key)
            self.assertGreaterEqual(ap.contrast(p["primary"], "#ffffff"), 4.5, key)
            self.assertGreaterEqual(ap.contrast(p["secondary"], "#ffffff"), 7, key)
            self.save(theme_preset=key, **{f"{c}_color": p[c] for c in ap.COLOR_FIELDS})
            self.assertEqual(Appearance.objects.get().theme_preset, key)


class ValidationTests(Base):
    def assertRejected(self, response, field_text=None):
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Appearance.objects.exists())
        if field_text:
            self.assertContains(response, field_text, status_code=422)

    def test_unreadable_or_invalid_colours_are_rejected(self):
        self.assertRejected(self.save(text_color="#fafafa"), "hard to read")                      # light text on light background
        self.assertRejected(self.save(primary_color="#ffee00"), "White button text")               # yellow primary
        self.assertRejected(self.save(secondary_color="#aaaaaa"), "dark sidebar")
        self.assertRejected(self.save(primary_color="red"), "Enter a colour")
        self.assertRejected(self.save(primary_color="#12345"), "Enter a colour")
        self.assertRejected(self.save(primary_color="#123456;}</style><script>alert(1)</script>"))
        self.assertEqual(GymSettings.objects.count(), 0)  # nothing saved, not even the gym name

    def test_gym_name_is_required(self):
        self.assertRejected(self.save(**{"gym-name": " "}))

    def test_layout_values_outside_the_choices_are_rejected_and_never_reach_css(self):
        self.assertRejected(self.save(sidebar_width="999px; background:url(x)"))
        self.assertEqual(ap.build_css({"sidebar_width": "999px", "density": "<x>", "stat_columns": "9"}).count("999"), 0)
        css = ap.build_css({"primary_color": "#123456;}body{display:none", "theme_preset": "<script>"})
        self.assertNotIn("display:none", css)
        self.assertIn("--gym-accent: #0f766e", css)  # invalid colour and preset fall back to the defaults

    def test_images_must_be_real_small_allowed_images(self):
        svg = SimpleUploadedFile("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', content_type="image/svg+xml")
        fake = SimpleUploadedFile("logo.png", b"<?php echo 1; ?>", content_type="image/png")
        big = SimpleUploadedFile("logo.png", image("PNG", (10, 10)).read() + b"0" * (2 * 1024 * 1024 + 1), content_type="image/png")
        for bad in (svg, fake, big):
            r = self.client.post(self.url, {**form_data(), "logo": bad})
            self.assertRejected(r)
            self.assertFalse(os.path.exists(os.path.join(TMP_MEDIA, "branding")) and os.listdir(os.path.join(TMP_MEDIA, "branding")))
        self.assertRejected(self.client.post(self.url, {**form_data(), "favicon": image("JPEG")}), "Ico, Png")       # favicon: PNG/ICO only
        self.assertRejected(self.client.post(self.url, {**form_data(), "favicon": image("PNG", (600, 600))}), "too large")
        self.assertRejected(self.client.post(self.url, {**form_data(), "login_background": image("PNG", (6000, 10))}), "too large")


class ImageTests(Base):
    def files(self):
        folder = os.path.join(TMP_MEDIA, "branding")
        return set(os.listdir(folder)) if os.path.exists(folder) else set()

    def test_upload_replace_and_remove(self):
        r = self.client.post(self.url, {**form_data(), "logo": image("PNG", name="my logo.png"), "favicon": image("PNG", (32, 32)),
                                        "login_background": image("JPEG", (200, 100))})
        self.assertRedirects(r, self.url)
        a = Appearance.objects.get()
        self.assertEqual(len(self.files()), 3)
        for f in (a.logo, a.favicon, a.login_background):
            self.assertTrue(f.name.startswith("branding/") and "my logo" not in f.name)  # random names, never the user's file name
            self.assertTrue(os.path.exists(f.path))
        page = self.client.get("/")
        self.assertContains(page, a.logo.url)
        self.assertContains(page, f'rel="icon" href="{a.favicon.url}"')
        self.assertContains(self.client.get(self.url), "Current logo")  # preview with replace / remove options
        login = self.client.logout() or self.client.get(reverse("accounts:login"))
        self.assertContains(login, a.login_background.url)
        self.assertContains(login, a.logo.url)
        # saving again without files keeps them
        self.login("super_admin")
        self.save(login_title="Hi")
        self.assertEqual(Appearance.objects.get().logo.name, a.logo.name)
        # replacing deletes the old file
        old = a.logo.name
        self.client.post(self.url, {**form_data(), "logo": image("PNG", (50, 50))})
        a.refresh_from_db()
        self.assertNotEqual(a.logo.name, old)
        self.assertFalse(os.path.exists(os.path.join(TMP_MEDIA, old)))
        self.assertEqual(len(self.files()), 3)
        # removing clears the field and the file
        name = a.favicon.name
        self.client.post(self.url, {**form_data(), "remove_favicon": "on"})
        a.refresh_from_db()
        self.assertEqual(a.favicon.name, "")
        self.assertFalse(os.path.exists(os.path.join(TMP_MEDIA, name)))
        self.assertNotContains(self.client.get("/"), 'rel="icon"')

    def test_files_are_served_by_djangos_media_configuration(self):
        self.client.post(self.url, {**form_data(), "logo": image()})
        self.assertTrue(Appearance.objects.get().logo.url.startswith("/media/branding/"))


class PreviewAndResetTests(Base):
    def test_preview_returns_css_without_saving(self):
        r = self.client.post(reverse("core:appearance_preview"), form_data(primary_color="#2c5f8a", theme_preset="custom", resolved_mode="dark"))
        self.assertEqual(r.status_code, 200)
        css = r.json()["css"]
        self.assertIn("#theme-preview {", css)
        self.assertIn("--gym-accent: #2c5f8a", css)
        self.assertIn("--gym-bg: #12161b", css)  # resolved dark mode
        self.assertFalse(Appearance.objects.exists())
        self.assertEqual(self.client.get(reverse("core:appearance_preview")).status_code, 405)

    def test_reset_restores_theme_and_layout_but_keeps_branding(self):
        self.client.post(self.url, {**form_data(theme_preset="forest", **{f"{c}_color": ap.PRESETS["forest"][c] for c in ap.COLOR_FIELDS},
                                                mode="dark", sidebar_style="light", density="compact", login_title="Keep me", **{"gym-name": "Keep Gym"}),
                                    "logo": image()})
        logo = Appearance.objects.get().logo.name
        self.assertEqual(self.client.get(reverse("core:appearance_reset")).status_code, 405)  # a link cannot reset
        r = self.client.post(reverse("core:appearance_reset"), follow=True)
        self.assertContains(r, "reset to the defaults")
        a = Appearance.objects.get()
        self.assertEqual((a.theme_preset, a.mode, a.sidebar_style, a.density, a.primary_color), ("graphite", "light", "dark", "comfortable", ""))
        self.assertEqual((a.logo.name, a.login_title), (logo, "Keep me"))
        self.assertEqual(GymSettings.objects.get().name, "Keep Gym")
        self.assertContains(self.client.get("/"), "--gym-accent: #0f766e")
        self.assertTrue(ActivityLog.objects.filter(action="appearance_reset").exists())
