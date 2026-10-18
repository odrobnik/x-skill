"""Offline tests for x.py. X is faked; run: python3 -m unittest discover tests"""
import base64
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # keep scripts/ free of __pycache__
_CONFIG = tempfile.TemporaryDirectory(prefix="x-skill-test-")
os.environ["X_SKILL_CONFIG_DIR"] = _CONFIG.name  # never touch the real config or sign-in
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import x  # noqa: E402

ENV_VARS = ("X_BEARER_TOKEN", "X_CLIENT_ID", "X_CLIENT_SECRET", "OPENCLAW_STATE_DIR")


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def http_error(status, body, headers=None):
    return urllib.error.HTTPError("https://api.x.com", status, "error", headers or {},
                                  io.BytesIO(json.dumps(body).encode()))


class FakeX:
    """Stands in for the X API: answers by method and path, and records every request."""

    def __init__(self, routes=None):
        self.routes = routes or {}  # {(method, path): reply, or a callable(request) -> reply}
        self.requests = []

    def urlopen(self, req, timeout=None):
        self.requests.append(req)
        route = (req.get_method(), urllib.parse.urlparse(req.full_url).path)
        if route not in self.routes:
            raise AssertionError(f"unexpected request: {req.get_method()} {req.full_url}")
        reply = self.routes[route]
        reply = reply(req) if callable(reply) else reply
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)

    def query(self, i=-1):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.requests[i].full_url).query))

    def form(self, i=-1):
        return dict(urllib.parse.parse_qsl(self.requests[i].data.decode()))


def run(*argv, fake=None):
    """Run the CLI in-process against a fake X; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    x.JSON_MODE, x._RESULT = False, None
    x._load_config.cache_clear()
    fake = fake or FakeX()
    code = 0
    with mock.patch.object(sys, "argv", ["x.py", *argv]), \
            mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
            mock.patch.object(x.urllib.request, "urlopen", fake.urlopen):
        try:
            x.main()
        except SystemExit as e:
            code = e.code if e.code is not None else 0
    return code, out.getvalue(), err.getvalue()


def write(path, data, mode=0o600):
    path.write_text(json.dumps(data))
    os.chmod(path, mode)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def sign_in(**extra):
    tokens = {"access_token": "A", "refresh_token": "R", "expires_in": 7200,
              "issued_at": int(time.time()), "user": {"id": "100", "username": "me", "name": "Me"}}
    tokens.update(extra)
    write(x.TOKEN_FILE, tokens)
    return tokens


class XTestCase(unittest.TestCase):
    def setUp(self):
        for child in x.CONFIG_DIR.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ENV_VARS:
            os.environ.pop(var, None)


class ConfigTests(XTestCase):
    def test_config_folder_order(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, "home", return_value=Path(home)):
            default, state = Path(home) / ".openclaw" / "x", Path(home) / "state"
            with mock.patch.dict(os.environ, {"X_SKILL_CONFIG_DIR": "/somewhere/else"}):
                self.assertEqual(x._config_dir(), Path("/somewhere/else"))
            os.environ.pop("X_SKILL_CONFIG_DIR")
            self.assertEqual(x._config_dir(), default)
            os.environ["OPENCLAW_STATE_DIR"] = str(state)
            self.assertEqual(x._config_dir(), state / "x")  # nothing set up yet: follow the state folder
            default.mkdir(parents=True)
            self.assertEqual(x._config_dir(), state / "x")  # an empty default folder doesn't count
            write(default / "credentials.json", {"bearer_token": "OLD"})
            self.assertEqual(x._config_dir(), default)  # 1.0's folder stays in use...
            (state / "x").mkdir(parents=True)
            self.assertEqual(x._config_dir(), default)  # ...also next to an empty new folder...
            write(state / "x" / "config.json", {"bearer_token": "NEW"})
            self.assertEqual(x._config_dir(), state / "x")  # ...until the files move

    def test_legacy_files_still_work_and_config_json_wins(self):
        legacy_creds, legacy_oauth = x.LEGACY_CONFIG_FILES
        write(legacy_creds, {"bearer_token": "OLD", "consumer_key": "OPTIONAL"})
        write(legacy_oauth, {"client_id": "CID", "client_secret": "SECRET"})
        x._load_config.cache_clear()
        self.assertEqual(x._load_config(), {"bearer_token": "OLD", "client_id": "CID", "client_secret": "SECRET"})
        write(x.CONFIG_FILE, {"bearer_token": "NEW"})
        x._load_config.cache_clear()
        self.assertEqual(x._load_config()["bearer_token"], "NEW")
        self.assertEqual(x._load_config()["client_id"], "CID")
        os.environ["X_BEARER_TOKEN"] = "ENV"
        x._load_config.cache_clear()
        self.assertEqual(x._load_config()["bearer_token"], "ENV")

    def test_warns_about_config_readable_by_others(self):
        write(x.CONFIG_FILE, {"bearer_token": "T"}, mode=0o644)
        fake = FakeX({("GET", "/2/users/by/username/XDevelopers"): {"data": {"id": "1", "username": "XDevelopers"}}})
        code, _, err = run("user", "XDevelopers", fake=fake)
        self.assertEqual(code, 0)
        self.assertIn("readable by other users", err)

    def test_missing_credentials(self):
        code, out, _ = run("--json", "user", "XDevelopers")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "missing_credentials")


class SignInStorageTests(XTestCase):
    def test_sign_in_readable_by_others_is_rewritten_owner_only(self):
        write(x.TOKEN_FILE, {"access_token": "A", "refresh_token": "R", "issued_at": time.time()}, mode=0o644)
        self.assertEqual(x.load_tokens()["access_token"], "A")
        self.assertEqual(mode(x.TOKEN_FILE), 0o600)
        self.assertEqual(json.loads(x.TOKEN_FILE.read_text())["refresh_token"], "R")

    def test_expired_sign_in_is_renewed_before_the_request(self):
        write(x.CONFIG_FILE, {"client_id": "CID", "client_secret": "SECRET"})
        sign_in(issued_at=int(time.time()) - 8000)
        fake = FakeX({
            ("POST", "/2/oauth2/token"): {"access_token": "B", "expires_in": 7200, "token_type": "bearer"},
            ("GET", "/2/users/100/bookmarks"): {"data": [{"id": "7", "text": "saved"}]},
        })
        code, out, _ = run("--json", "bookmarks", fake=fake)
        self.assertEqual(code, 0, out)
        renew = fake.requests[0]
        self.assertEqual(fake.form(0), {"grant_type": "refresh_token", "refresh_token": "R"})
        self.assertEqual(renew.get_header("Authorization"), "Basic " + base64.b64encode(b"CID:SECRET").decode())
        self.assertEqual(fake.requests[1].get_header("Authorization"), "Bearer B")
        tokens = json.loads(x.TOKEN_FILE.read_text())
        self.assertEqual((tokens["access_token"], tokens["refresh_token"]), ("B", "R"))  # X sent no new one
        self.assertGreater(tokens["issued_at"], time.time() - 60)  # 1.0's field, so 1.0 can share the sign-in
        self.assertEqual(tokens["user"]["username"], "me")
        self.assertEqual(mode(x.TOKEN_FILE), 0o600)

    def test_a_401_renews_the_sign_in_once_and_retries(self):
        write(x.CONFIG_FILE, {"client_id": "CID"})  # public client: no secret
        sign_in()
        answers = [http_error(401, {"title": "Unauthorized", "status": 401, "detail": "Unauthorized"}),
                   {"data": [{"id": "7", "text": "saved"}]}]
        fake = FakeX({
            ("POST", "/2/oauth2/token"): {"access_token": "B", "refresh_token": "R2", "expires_in": 7200},
            ("GET", "/2/users/100/bookmarks"): lambda req: answers.pop(0),
        })
        code, out, _ = run("--json", "bookmarks", fake=fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.form(1), {"grant_type": "refresh_token", "refresh_token": "R", "client_id": "CID"})
        self.assertIsNone(fake.requests[1].get_header("Authorization"))
        self.assertEqual(fake.requests[2].get_header("Authorization"), "Bearer B")
        self.assertEqual(json.loads(x.TOKEN_FILE.read_text())["refresh_token"], "R2")

    def test_reading_without_bearer_token_uses_the_sign_in(self):
        sign_in()
        fake = FakeX({("GET", "/2/users/by/username/XDevelopers"): {"data": {"id": "1", "username": "XDevelopers"}}})
        code, out, _ = run("--json", "user", "@XDevelopers", fake=fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.requests[0].get_header("Authorization"), "Bearer A")
        self.assertEqual(json.loads(out)["url"], "https://x.com/XDevelopers")


class PostTests(XTestCase):
    def test_without_yes_only_previews(self):
        sign_in()
        fake = FakeX()
        code, out, err = run("post", "Hello X", "--json", fake=fake)
        self.assertEqual(code, x.NEEDS_CONFIRMATION_EXIT)
        self.assertEqual(json.loads(out), {"text": "Hello X", "characters": 7, "account": "me", "ok": False,
                                           "needs_confirmation": True, "dry_run": True})
        self.assertEqual(fake.requests, [])
        self.assertIn("Would post as @me", err)

    def test_with_yes_posts(self):
        sign_in()
        fake = FakeX({("POST", "/2/tweets"): {"data": {"id": "42", "text": "Hello X"}}})
        code, out, _ = run("--json", "post", "Hello X", "--yes", fake=fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out), {"ok": True, "posted": True, "id": "42", "text": "Hello X",
                                           "account": "me", "url": "https://x.com/me/status/42"})
        self.assertEqual(json.loads(fake.requests[0].data), {"text": "Hello X"})
        self.assertEqual(fake.requests[0].get_header("Authorization"), "Bearer A")

    def test_preview_names_the_account_of_a_sign_in_that_never_stored_it(self):
        sign_in(user=None)  # what 1.0 stored, or a sign-in whose account lookup failed
        fake = FakeX({("GET", "/2/users/me"): {"data": {"id": "100", "username": "me", "name": "Me"}}})
        code, out, err = run("--json", "post", "Hello X", fake=fake)
        self.assertEqual(code, x.NEEDS_CONFIRMATION_EXIT)
        self.assertEqual(json.loads(out)["account"], "me")
        self.assertIn("Would post as @me", err)
        self.assertEqual(json.loads(x.TOKEN_FILE.read_text())["user"]["username"], "me")

    def test_yes_does_not_post_when_the_account_is_unknown(self):
        sign_in(user=None)
        fake = FakeX({("GET", "/2/users/me"): http_error(503, {"title": "Service Unavailable"})})
        code, out, _ = run("--json", "post", "Hello X", "--yes", fake=fake)
        self.assertEqual(code, 1)
        self.assertEqual([r.get_method() for r in fake.requests], ["GET"])  # nothing was posted

    def test_needs_the_sign_in_even_for_the_preview(self):
        code, out, _ = run("--json", "post", "Hello X")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "not_signed_in")

    def test_empty_text_is_a_usage_error(self):
        sign_in()
        code, out, _ = run("--json", "post", "   ")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"]["code"], "usage")


class AuthTests(XTestCase):
    def manual(self):
        code, out, _ = run("--json", "auth", "--manual")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def test_manual_sign_in_uses_state_and_pkce(self):
        write(x.CONFIG_FILE, {"client_id": "CID"})
        result = self.manual()
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(result["auth_url"]).query))
        pending = json.loads(x.PENDING_AUTH_FILE.read_text())
        self.assertEqual(mode(x.PENDING_AUTH_FILE), 0o600)
        self.assertEqual(query["state"], pending["state"])
        self.assertGreaterEqual(len(pending["state"]), 32)
        self.assertNotEqual(pending["state"], "state")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(pending["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual((query["code_challenge"], query["code_challenge_method"]), (challenge, "S256"))
        self.assertEqual(query["redirect_uri"], "http://localhost:8080/callback")
        self.assertEqual(query["scope"].split(), list(x.SCOPES))
        self.assertNotEqual(self.manual()["auth_url"], result["auth_url"])  # a fresh state every time

    def test_code_from_the_redirect_address_finishes_the_sign_in(self):
        write(x.CONFIG_FILE, {"client_id": "CID", "client_secret": "SECRET"})
        self.manual()
        pending = json.loads(x.PENDING_AUTH_FILE.read_text())
        fake = FakeX({
            ("POST", "/2/oauth2/token"): {"access_token": "A", "refresh_token": "R", "expires_in": 7200,
                                          "scope": "tweet.read users.read offline.access"},
            ("GET", "/2/users/me"): {"data": {"id": "100", "username": "me", "name": "Me"}},
        })
        address = f"http://localhost:8080/callback?state={pending['state']}&code=THE_CODE"
        code, out, _ = run("--json", "auth", "--code", address, fake=fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["account"], "me")
        self.assertEqual(fake.form(0), {"grant_type": "authorization_code", "code": "THE_CODE",
                                        "redirect_uri": "http://localhost:8080/callback",
                                        "code_verifier": pending["code_verifier"]})
        tokens = json.loads(x.TOKEN_FILE.read_text())
        self.assertEqual((tokens["access_token"], tokens["user"]["id"]), ("A", "100"))
        self.assertEqual(mode(x.TOKEN_FILE), 0o600)
        self.assertFalse(x.PENDING_AUTH_FILE.exists())

    def test_redirect_address_from_another_attempt_is_rejected(self):
        write(x.CONFIG_FILE, {"client_id": "CID"})
        self.manual()
        fake = FakeX()
        code, out, _ = run("--json", "auth", "--code",
                           "http://localhost:8080/callback?state=WRONG&code=THE_CODE", fake=fake)
        self.assertEqual(code, 1)
        self.assertIn("different sign-in attempt", json.loads(out)["error"]["message"])
        self.assertEqual(fake.requests, [])
        self.assertFalse(x.TOKEN_FILE.exists())

    def test_logout_revokes_and_deletes_the_sign_in(self):
        write(x.CONFIG_FILE, {"client_id": "CID", "client_secret": "SECRET"})
        sign_in()
        fake = FakeX({("POST", "/2/oauth2/revoke"): {"revoked": True}})
        code, out, _ = run("--json", "logout", fake=fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out), {"ok": True, "revoked": True, "tokens_deleted": True,
                                           "was_signed_in": True})
        self.assertEqual([fake.form(i)["token_type_hint"] for i in (0, 1)], ["refresh_token", "access_token"])
        self.assertFalse(x.TOKEN_FILE.exists())


class ReadTests(XTestCase):
    def setUp(self):
        super().setUp()
        os.environ["X_BEARER_TOKEN"] = "APP"

    def test_post_urls_and_ids(self):
        for value in ("1234567890", " 1234567890 ", "https://x.com/someone/status/1234567890",
                      "https://twitter.com/someone/status/1234567890?s=20",
                      "https://mobile.twitter.com/someone/status/1234567890",
                      "https://x.com/i/web/status/1234567890", "x.com/i/status/1234567890"):
            self.assertEqual(x.extract_tweet_id(value), "1234567890", value)
        code, out, _ = run("--json", "tweet", "https://example.com/1234567890")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (1, "invalid_tweet"))

    def test_usernames(self):
        for value in ("XDevelopers", "@XDevelopers", "https://x.com/XDevelopers", "x.com/XDevelopers?s=1",
                      "https://twitter.com/XDevelopers/"):
            self.assertEqual(x.parse_username(value), "XDevelopers", value)
        code, out, _ = run("--json", "user", "not a name")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (1, "invalid_username"))

    def test_tweets_keeps_the_order_asked_and_reports_missing_posts(self):
        fake = FakeX({("GET", "/2/tweets"): {
            "data": [{"id": "2", "text": "two", "author_id": "9"}, {"id": "1", "text": "one", "author_id": "9"}],
            "includes": {"users": [{"id": "9", "username": "someone"}]},
        }})
        code, out, _ = run("--json", "tweets", "1", "https://x.com/someone/status/2", "3", "1", fake=fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.query()["ids"], "1,2,3")
        result = json.loads(out)
        self.assertEqual([r["id"] for r in result], ["1", "2", "3"])
        self.assertEqual(result[0]["url"], "https://x.com/someone/status/1")
        self.assertEqual(result[2]["found"], False)
        self.assertEqual(fake.requests[0].get_header("Authorization"), "Bearer APP")

    def test_thread_is_oldest_first(self):
        fake = FakeX({
            ("GET", "/2/tweets/5"): {"data": {"id": "5", "author_id": "9", "conversation_id": "3"}},
            ("GET", "/2/tweets/search/recent"): {"data": [{"id": "5"}, {"id": "4"}, {"id": "3"}]},
        })
        code, out, _ = run("--json", "thread", "https://x.com/someone/status/5", fake=fake)
        self.assertEqual(code, 0, out)
        self.assertEqual([t["id"] for t in json.loads(out)], ["3", "4", "5"])
        self.assertEqual(fake.query()["query"], "conversation_id:3 from:9")

    def test_timeline_options(self):
        fake = FakeX({
            ("GET", "/2/users/by/username/someone"): {"data": {"id": "9", "username": "someone"}},
            ("GET", "/2/users/9/tweets"): {"meta": {"result_count": 0}},
        })
        code, out, _ = run("--json", "timeline", "someone", "--max", "5", "--exclude", "replies,retweets",
                           fake=fake)
        self.assertEqual((code, json.loads(out)), (0, []))
        self.assertEqual((fake.query()["max_results"], fake.query()["exclude"]), ("5", "replies,retweets"))

    def test_rate_limit_error(self):
        reset = int(time.time()) + 60
        fake = FakeX({("GET", "/2/users/by/username/XDevelopers"): http_error(
            429, {"title": "Too Many Requests", "detail": "Too Many Requests", "type": "about:blank"},
            {"x-rate-limit-reset": str(reset)})})
        code, out, _ = run("--json", "user", "XDevelopers", fake=fake)
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], "rate_limited")
        self.assertIn("resets at", error["message"])

    def test_not_found_uses_x_explanation(self):
        fake = FakeX({("GET", "/2/users/by/username/nobody"): {"errors": [{
            "detail": "Could not find user with username: [nobody].", "title": "Not Found Error",
            "type": "https://api.twitter.com/2/problems/resource-not-found"}]}})
        code, out, _ = run("--json", "user", "nobody", fake=fake)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"], {"message": "Could not find user with username: [nobody].",
                                                    "code": "resource-not-found"})

    def test_usage_errors_are_json_too(self):
        for argv in (["--json"], ["--json", "timeline", "someone", "--max", "1"],
                     ["search", "query", "--max", "5", "--json"], ["--json", "timeline", "a", "--exclude", "likes"]):
            code, out, _ = run(*argv)
            self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "usage"), argv)


class BookmarkTests(XTestCase):
    REPLY = {
        "data": [
            {"id": "1", "text": "plain post", "entities": {"urls": [{"url": "https://t.co/a",
                                                                       "expanded_url": "https://example.com/a"}]}},
            {"id": "2", "text": "quote", "referenced_tweets": [{"type": "quoted", "id": "9"}]},
        ],
        "includes": {"tweets": [{"id": "9", "text": "x", "article": {"title": "The Needle Article"}}]},
    }

    def test_contains_also_searches_referenced_articles(self):
        sign_in()
        fake = FakeX({("GET", "/2/users/100/bookmarks"): self.REPLY})
        code, out, _ = run("--json", "bookmarks", "--contains", "needle", fake=fake)
        self.assertEqual(code, 0, out)
        self.assertEqual([t["id"] for t in json.loads(out)], ["2"])

    def test_urls_lists_only_the_links(self):
        sign_in()
        fake = FakeX({("GET", "/2/users/100/bookmarks"): self.REPLY})
        code, out, _ = run("bookmarks", "--urls", "--json", fake=fake)
        self.assertEqual((code, json.loads(out)), (0, ["https://example.com/a"]))

    def test_account_is_looked_up_once_and_kept(self):
        sign_in(user=None)
        fake = FakeX({("GET", "/2/users/me"): {"data": {"id": "100", "username": "me", "name": "Me"}},
                      ("GET", "/2/users/100/bookmarks"): {"meta": {"result_count": 0}}})
        self.assertEqual(run("--json", "bookmarks", fake=fake)[0], 0)
        self.assertEqual(run("--json", "bookmarks", fake=fake)[0], 0)
        paths = [urllib.parse.urlparse(r.full_url).path for r in fake.requests]
        self.assertEqual(paths.count("/2/users/me"), 1)
        self.assertEqual(json.loads(x.TOKEN_FILE.read_text())["user"]["id"], "100")


class HomeTests(XTestCase):
    PATH = "/2/users/100/timelines/reverse_chronological"

    def test_home_uses_the_sign_in_and_adds_authors(self):
        sign_in()
        os.environ["X_BEARER_TOKEN"] = "APP"  # must not be used: the home timeline needs the user
        fake = FakeX({("GET", self.PATH): {
            "data": [{"id": "2", "text": "short", "author_id": "9",
                      "note_tweet": {"text": "the whole long post"}}, {"id": "1", "text": "hi", "author_id": "8"}],
            "includes": {"users": [{"id": "9", "username": "someone", "name": "Some One"}]},
            "meta": {"result_count": 2}}})
        code, out, err = run("home", "--json", "--max", "5", "--exclude", "replies", fake=fake)
        self.assertEqual(code, 0, out)
        result = json.loads(out)
        self.assertEqual([t["id"] for t in result], ["2", "1"])
        self.assertEqual((result[0]["author_username"], result[0]["author_name"]), ("someone", "Some One"))
        self.assertEqual(result[0]["url"], "https://x.com/someone/status/2")
        self.assertNotIn("author_username", result[1])
        q = fake.query()
        self.assertEqual((q["max_results"], q["exclude"], q["expansions"].split(",")[0]), ("5", "replies", "author_id"))
        self.assertIn("note_tweet", q["tweet.fields"])
        self.assertEqual(fake.requests[0].get_header("Authorization"), "Bearer A")
        self.assertIn("the whole long post", err)
        self.assertEqual(result[0]["full_text"], "the whole long post")
        self.assertIn("referenced_tweets.id", q["expansions"])

    def test_home_reposts_get_the_original_full_text(self):
        sign_in()
        fake = FakeX({("GET", self.PATH): {
            "data": [{"id": "3", "text": "RT @orig: truncated…", "author_id": "8",
                      "referenced_tweets": [{"type": "retweeted", "id": "7"}]}],
            "includes": {"tweets": [{"id": "7", "author_id": "9", "text": "short",
                                     "note_tweet": {"text": "the original, in full"}}],
                         "users": [{"id": "8", "username": "reposter", "name": "R"},
                                   {"id": "9", "username": "orig", "name": "O"}]}}})
        code, out, err = run("--json", "home", fake=fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)[0]["full_text"], "RT @orig: the original, in full")
        self.assertIn("the original, in full", err)

    def test_home_pages_through_for_more_than_100(self):
        sign_in()
        pages = iter([
            {"data": [{"id": str(i)} for i in range(100)], "meta": {"next_token": "T1"}},
            {"data": [{"id": str(i)} for i in range(100, 200)], "meta": {"next_token": "T2"}},
        ])
        fake = FakeX({("GET", self.PATH): lambda req: next(pages)})
        code, out, _ = run("--json", "home", "--max", "150", fake=fake)
        self.assertEqual((code, len(json.loads(out))), (0, 150))
        self.assertEqual((fake.query(0)["max_results"], fake.query(1)["max_results"]), ("100", "50"))
        self.assertEqual(fake.query(1)["pagination_token"], "T1")

    def test_home_needs_the_sign_in(self):
        os.environ["X_BEARER_TOKEN"] = "APP"
        code, out, _ = run("--json", "home")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (1, "not_signed_in"))

    def test_home_max_is_checked(self):
        for value in ("0", "3201"):
            code, out, _ = run("--json", "home", "--max", value)
            self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "usage"), value)
        sign_in()
        fake = FakeX({("GET", self.PATH): {"meta": {"result_count": 0}}})
        self.assertEqual(run("--json", "home", "--max", "3200", fake=fake)[0], 0)


if __name__ == "__main__":
    unittest.main()
