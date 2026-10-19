"""Whole-application link and form crawl.

With realistic demo data in every module, each role starts at its home page and follows every internal link it is shown.
A link that returns 404/500 is broken; a link that returns 403 is a visible control the role is not allowed to use (a
navigation bug); every POST form must carry a CSRF token and post to a URL that exists.
"""
import os
import re
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse

from django.core.cache import cache
from django.test import TestCase
from django.urls import Resolver404, resolve

from apps.accounts.models import Role
from apps.core.demo import DEMO_PASSWORD, seed_demo
from apps.core.testing import PASSWORD, make_users

HREF = re.compile(r'<a\s[^>]*?href="([^"]+)"', re.I)
FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
SKIP_PREFIXES = ("/static/", "/media/", "/django-admin/")
MAX_VARIANTS = 6  # per path: different ids are different paths; this only limits ?query variants of one page


def internal(href, current):
    href = href.replace("&amp;", "&").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "http://", "https://", "//")):
        return None
    url = urldefrag(urljoin(current, href))[0]
    path = urlparse(url).path
    return None if path.startswith(SKIP_PREFIXES) else url


def crawl(client, start="/", limit=600):
    """Breadth-first GET crawl. Returns ({url: status}, [form problems])."""
    seen, queue, problems, statuses = {start}, deque([start]), [], {}
    per_path = {}  # "previous day" / "next page" style links would otherwise be followed forever

    def enqueue(target):
        path = urlparse(target).path
        if target in seen or per_path.get(path, 0) >= MAX_VARIANTS:
            return
        seen.add(target)
        per_path[path] = per_path.get(path, 0) + 1
        queue.append(target)

    while queue and len(statuses) < limit:
        url = queue.popleft()
        response = client.get(url)
        statuses[url] = response.status_code
        if response.status_code in (301, 302):
            target = internal(response["Location"], url)
            if target:
                enqueue(target)
            continue
        if response.status_code != 200 or "text/html" not in response.get("Content-Type", ""):
            continue
        html = response.content.decode("utf-8", "replace")
        for href in HREF.findall(html):
            target = internal(href, url)
            if target:
                enqueue(target)
        for attrs, body in FORM.findall(html):
            method = (re.search(r'method="(\w+)"', attrs, re.I) or [None, "get"])[1].lower()
            action = (re.search(r'action="([^"]*)"', attrs, re.I) or [None, ""])[1].replace("&amp;", "&")
            if method == "post":
                if "csrfmiddlewaretoken" not in body:
                    problems.append(f"{url}: POST form without CSRF token (action={action!r})")
                path = urlparse(urljoin(url, action)).path
                try:
                    resolve(path)
                except Resolver404:
                    problems.append(f"{url}: form posts to unknown URL {path}")
    if os.environ.get("CRAWL_DEBUG"):
        from collections import Counter

        print("CRAWL", len(statuses), Counter("/".join(urlparse(u).path.split("/")[:3]) for u in statuses).most_common(12))
    return statuses, problems


class CrawlBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cache.clear()
        cls.users = make_users()
        cls.demo = seed_demo(cls.users["manager"])
        assert cls.demo is not None

    def crawl_as(self, username, password=PASSWORD):
        self.client.logout()
        self.assertTrue(self.client.login(username=username, password=password), username)
        return crawl(self.client)

    def assert_clean(self, username, password=PASSWORD, min_pages=5):
        statuses, problems = self.crawl_as(username, password)
        broken = {u: s for u, s in statuses.items() if s in (403, 404, 405, 500)}
        self.assertEqual(broken, {}, f"{username}: broken or forbidden links")
        self.assertEqual(problems, [], f"{username}: form problems")
        self.assertGreaterEqual(len(statuses), min_pages, f"{username}: crawl found too few pages")
        return statuses


class StaffCrawlTests(CrawlBase):
    def test_super_admin_sees_a_working_application(self):
        statuses = self.assert_clean("super_admin", min_pages=120)
        for fragment in ("/members/", "/memberships/", "/payments/", "/attendance/", "/trainers/", "/appointments/", "/workouts/plans/", "/progress/",
                         "/equipment/", "/inventory/", "/expenses/", "/suppliers/", "/announcements/", "/reports/", "/audit/", "/settings/gym/",
                         "/notifications/settings/", "/accounts/users/", "/accounts/permissions/"):
            self.assertTrue(any(u.startswith(fragment) for u in statuses), f"crawl never reached {fragment}")

    def test_manager(self):
        self.assert_clean("manager", min_pages=100)

    def test_receptionist(self):
        self.assert_clean("receptionist", min_pages=40)

    def test_accountant(self):
        self.assert_clean("accountant", min_pages=30)

    def test_trainer(self):
        self.assert_clean("trainer", min_pages=8)
        self.assert_clean("demo_trainer", DEMO_PASSWORD, min_pages=20)


class MemberCrawlTests(CrawlBase):
    def test_portal_pages_have_no_dead_links(self):
        statuses = self.assert_clean("demo_alice", DEMO_PASSWORD, min_pages=10)
        self.assertTrue(any(u.startswith("/me/workouts/") for u in statuses))
        self.assertTrue(all(u.startswith(("/me/", "/accounts/", "/notifications/", "/", )) for u in statuses))

    def test_members_never_reach_staff_pages_by_following_links(self):
        statuses, _ = self.crawl_as("demo_alice", DEMO_PASSWORD)
        staff_prefixes = ("/members/", "/memberships/", "/payments/", "/attendance/", "/trainers/", "/appointments/", "/expenses/", "/reports/",
                          "/equipment/", "/inventory/", "/audit/", "/accounts/users/")
        self.assertEqual([u for u in statuses if u.startswith(staff_prefixes)], [])
