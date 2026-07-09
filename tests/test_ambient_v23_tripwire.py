"""P4 — credential tripwire hardening (F02, security, deterministic). A sensitive
keyword embedded in an ALL-CAPS env identifier before '='/':' with a high-entropy
value must be caught, even in an arbitrarily-named file. False positives (public
keys, short values) must NOT trip. Linear-time (no ReDoS). See
docs/plans/2026-07-06-stress-test-remediation.md."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_tripwire", _BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_tripwire", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = _load_module()

_HI = "aQ7pR2xL9mZ4kT8vB1nC6wY3jD5sF0hG7uE2iO4a"  # 40 synthetic high-entropy chars


class TestAmbientV23Tripwire(unittest.TestCase):

    # --- the gap the stress test found: caught now, any filename ---------
    def test_env_secret_assignment_is_detected(self):
        for line in [
            f"AWS_SECRET_ACCESS_KEY={_HI}",
            f"DB_PASSWORD={_HI}",
            f"GITHUB_TOKEN={_HI}",
            f"export API_SECRET={_HI}",
            f"MY_APP_ACCESS_TOKEN = {_HI}",
            f'STRIPE_SECRET_KEY="{_HI}"',
            f"SERVICE_CREDENTIAL: {_HI}",
            "PASSWORD=p@ssw0rd!",                       # bare strong name, short value
            "TOKEN=p@ssw0rd!",
            "AWS_SECRET_PUBLIC_KEY=p@ssw0rd!",          # strong name, not exempted
            f"API_KEY={_HI}",                           # plain _KEY w/ high-entropy value
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    # --- false positives that must NOT trip -------------------------------
    def test_benign_lines_do_not_trip(self):
        for line in [
            f"PUBLIC_KEY={_HI}",            # a public key is not a secret
            f"RSA_PUBLIC_KEY={_HI}",
            "API_KEY=short",               # value too short / low entropy
            "MY_KEY=1",
            "SOME_TOKEN=todo",             # strong name but value < 8
            'PRIMARY_KEY = "customer_id"',  # schema column, not a secret
            'FOREIGN_KEY = "account_id"',
            "PARTITION_KEY = user_region",
            "def make_key(name):",         # not an assignment at all
            "the secret sauce is love",    # prose
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    # --- Codex-found bypasses (must all be caught now) --------------------
    def test_codex_bypasses_now_caught(self):
        for line in [
            '{"secret": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"}',   # base64 / and +
            "DB_PASSWORD='p@ssw0rd-rotated-2026!'",                     # punctuated pw
            "REDIS_PASSWORD=prod-7k9!",                                 # short pw
            "NEXT_PUBLIC_AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG+bPxRfiCY",  # PUBLIC substring abuse
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI\\",                    # backslash continuation (line 1)
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    # --- Codex round 3: JSON / lowercase / tab-gutter / hash-FP ----------
    def test_round3_secret_shapes_are_caught(self):
        for line in [
            '{"AWS_SECRET_ACCESS_KEY":"wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"}',
            '{"password":"p@ssw0rd!"}',
            "db_password=p@ssw0rd!",
            "DbPassword=p@ssw0rd!",
            'password: "p@ssw0rd!"',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_round3_false_positives_do_not_trip(self):
        for line in [
            "CACHE_KEY=0123456789abcdef0123456789abcdef01234567",  # git SHA, not a key
            "CACHE_KEY=550e8400e29b41d4a716446655440000",          # compact UUID
            "password = get_input()",                               # code, not a literal
            "foreign_key = other_table.id",                         # code reference
            "sort_key = compute(x)",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    # --- Codex round 4 ----------------------------------------------------
    def test_round4_bypasses_now_caught(self):
        for line in [
            "ok=1 AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",  # 2nd assignment
            "DB_PASSWORD=p(ass)w0rd!2026",                    # real secret with brackets
            '{"password":"p(ass)w0rd!2026"}',
            "SESSION_KEY=abcdefabcdefabcdefabcdef",            # sensitive key, hex value
            "ACCESS_KEY=0123456789abcdef0123456789abcdef",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_round4_false_positives_do_not_trip(self):
        for line in [
            "password = user.password_hash",       # code attribute reference
            "token = session.current_access_token",
            "CACHE_KEY=0123456789abcdef0123456789abcdef01234567",  # hash still not a key
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    # --- Codex round 5 ----------------------------------------------------
    def test_round5_bypasses_now_caught(self):
        for line in [
            "TOKEN=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",  # JWT
            "here is my key: AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",  # prose prefix
            "db_password: p(ass)w0rd!",                        # lowercase YAML w/ brackets
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_round5_code_refs_still_clean(self):
        for line in [
            "password = user.password_hash",        # still a code ref (lowercase)
            "token = session.current_access_token",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    # --- Codex round 6 ----------------------------------------------------
    def test_round6_azure_connection_string_caught(self):
        for line in [
            ('"AzureWebJobsStorage": "DefaultEndpointsProtocol=https;AccountName=devstore;'
             'AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;'
             'EndpointSuffix=core.windows.net"'),
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_round6_code_refs_not_false_positive(self):
        for line in [
            "const password = user.passwordHash;",      # JS/TS camelCase code ref
            "this.password = req.body.password;",
            "self.token = obj.session_token",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    # --- Codex round 7 ----------------------------------------------------
    def test_round7_azure_sas_signature_caught(self):
        line = ('AZURE_STORAGE_CONNECTION_STRING="BlobEndpoint=https://acct.blob.core.'
                'windows.net/;SharedAccessSignature=sv=2020-08-04&ss=b&srt=sco&sp=rwdlac'
                '&se=2026-01-01T00:00:00Z&sig=abcDEF123%2Fghi%2BjklMNO456pqr%3D"')
        self.assertIs(amb._line_has_secret(line), True)

    def test_round8_http_basic_auth_caught(self):
        self.assertIs(
            amb._line_has_secret("Authorization: Basic YWxpY2U6U3VwZXJTZWNyZXQxMjMheHl6"), True)

    def test_round8_basic_word_not_false_positive(self):
        self.assertIs(
            amb._line_has_secret("Basic understanding of the system is required"), False)

    def test_round9_password_only_redis_url_caught(self):
        self.assertIs(
            amb._line_has_secret("REDIS_URL=redis://:supersecret1@redis.example.com:6379/0"), True)

    def test_round11_gitlab_pat_caught(self):
        self.assertIs(amb._line_has_secret("GITLAB_TOKEN=glpat-ABC123def456GHI789jkl0"), True)
        self.assertIs(amb._line_has_secret("glpat-ABC123def456GHI789jkl0"), True)

    def test_round12_code_expression_values_not_false_positive(self):
        for line in [
            "password = getPasswordFromEnvironment()",   # function call (code)
            "token = refreshTokenFromRequest()",
            "const secret = config.getSecret();",
            "password = req.body.password",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_round12_real_secrets_still_caught(self):
        for line in [
            "password = 'wJalrXUtnFEMI/K7MDENG+bPxRf'",   # quoted literal still caught
            "secret = supersecret_value_12345",           # bare high-entropy still caught
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_round13_dotted_env_secrets_caught(self):
        for line in [
            "DB_PASSWORD=prod.db.password",              # ALL-CAPS env w/ dotted value
            "JWT_SECRET=correct.horse.battery.staple",
            "API_TOKEN=abc.def.ghi.jkl",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_round13_lowercase_code_refs_still_cleared(self):
        self.assertIs(amb._line_has_secret("password = user.password_hash"), False)
        self.assertIs(amb._line_has_secret("const password = user.passwordHash;"), False)

    def test_round14_azure_code_refs_not_false_positive(self):
        for line in [
            "account_key = settings.account_key",       # code ref, not Azure key
            "shared_key = configuration.sharedKey",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_round14_real_account_key_still_caught(self):
        self.assertIs(amb._line_has_secret(
            "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq=="), True)

    def test_round15_config_about_secrets_not_false_positive(self):
        for line in [
            "SECRET_NAME=my-service-secret-name",       # config ABOUT a secret
            "SECRET_PATH=/etc/myapp/secret-file",
            "TOKEN_EXPIRATION=2026-12-31",
            "PASSWORD_POLICY=minimum_length_12",
            "PASSWORD_MIN_LENGTH=12",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_round15_real_secrets_still_caught(self):
        for line in [
            "DB_PASSWORD=aQ7pR2xL9mZ4kT8v",             # real secret still caught
            "API_SECRET=wJalrXUtnFEMI123456",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_round16_env_plumbing_code_not_false_positive(self):
        for line in [
            'DB_PASSWORD = os.getenv("DB_PASSWORD")',        # #1 secret-plumbing pattern
            'API_SECRET = os.environ.get("API_SECRET")',
            "const DB_PASSWORD = process.env.DB_PASSWORD;",
            'AWS_SECRET_ACCESS_KEY = os.environ.get("KEY")',
            "password = hashPassword(raw)",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_round16_real_env_secrets_still_caught(self):
        for line in [
            "DB_PASSWORD=prod.db.password",          # .env dotted value (no spaces) = secret
            "DB_PASSWORD=aQ7pR2xL9mZ4kT8v",          # bare high-entropy = secret
            "DB_PASSWORD=p(ass)w0rd!",               # bracketed password = secret
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_round17_variable_passthrough_not_false_positive(self):
        for line in [
            "DB_PASSWORD=$DB_PASSWORD",                  # shell passthrough
            "DB_PASSWORD=${DB_PASSWORD}",
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}",   # docker-compose
            "API_TOKEN=%API_TOKEN%",                     # windows
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_round18_kebab_dotted_config_secrets_caught(self):
        for line in [
            "mysql-root-password: p@ssw0rd!",            # K8s/YAML kebab-case
            "client-secret: p@ssw0rd!",                  # OAuth
            "spring.datasource.password: p@ssw0rd!",     # Java dotted properties
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_round18_kebab_dotted_config_not_false_positive(self):
        for line in [
            "spring.datasource.url: jdbc://localhost/db",   # config, not a secret
            "mysql-max-connections: 100",
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_tab_gutter_bypass_blocked(self):
        # Codex round 3: an inner fake gutter with a TAB survived the space-only strip.
        chunks = [("x.txt", "   7| \t12| AWS_SECRET_ACCESS_KEY="
                            "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY\n")]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                amb.refuse_if_secrets(chunks, allow=False)
        self.assertIn("x.txt", str(cm.exception.code) + buf.getvalue())

    def test_real_public_key_still_excluded(self):
        # a genuine *_PUBLIC_KEY trailing component is still not a secret
        self.assertIs(amb._line_has_secret(f"RSA_PUBLIC_KEY={_HI}"), False)
        self.assertIs(amb._line_has_secret(f"PUBLIC_KEY={_HI}"), False)

    def test_double_gutter_bypass_blocked(self):
        # attacker embeds a fake gutter so a single strip leaves "12| SECRET=…"
        chunks = [("x.txt", f"   1| 12| AWS_SECRET_ACCESS_KEY={_HI}\n")]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                amb.refuse_if_secrets(chunks, allow=False)
        self.assertIn("x.txt", str(cm.exception.code) + buf.getvalue())

    # --- existing patterns still work (no regression) ----------------------
    def test_existing_patterns_still_detected(self):
        self.assertIs(amb._line_has_secret("api_key: 'abcdef1234567890XYZ'"), True)
        self.assertIs(amb._line_has_secret("AKIA1234567890ABCDEF"), True)
        self.assertIs(amb._line_has_secret("password = supersecret_value_123"), True)

    # --- refuse_if_secrets integration: creds.txt is now blocked -----------
    def test_refuse_if_secrets_blocks_creds_txt(self):
        chunks = [("creds.txt", f"AWS_SECRET_ACCESS_KEY={_HI}\n")]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                amb.refuse_if_secrets(chunks, allow=False)
        # exit_code==1 exits with the prose message AS the SystemExit arg
        msg = str(cm.exception.code) + buf.getvalue()
        self.assertIn("creds.txt", msg)
        self.assertIn("secrets", msg.lower())

    def test_refuse_if_secrets_blocks_gutter_prefixed_content(self):
        # Audit inputs are line-number-guttered BEFORE the tripwire — the exact
        # presentation that let creds.txt through live. Must still be caught.
        chunks = [("creds.txt", f"   1| AWS_SECRET_ACCESS_KEY={_HI}\n   2| ok\n")]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                amb.refuse_if_secrets(chunks, allow=False)
        msg = str(cm.exception.code) + buf.getvalue()
        self.assertIn("creds.txt:1", msg)  # reports the true absolute (gutter) line

    def test_allow_secrets_bypasses(self):
        chunks = [("creds.txt", f"AWS_SECRET_ACCESS_KEY={_HI}\n")]
        amb.refuse_if_secrets(chunks, allow=True)  # must NOT raise

    # --- ReDoS: the new pattern stays linear on a huge adversarial line ----
    def test_no_redos_on_pathological_line(self):
        line = "A" * 400_000  # no separator, no assignment — worst case for the anchor
        start = time.monotonic()
        amb._line_has_secret(line)
        self.assertLess(time.monotonic() - start, 1.0)
        line2 = ("KEY_" * 100_000) + "= " + _HI  # many underscore groups
        start = time.monotonic()
        amb._line_has_secret(line2)
        self.assertLess(time.monotonic() - start, 1.0)

    # --- Workflow adversarial findings (2026-07-07) ---
    def test_connection_string_password_is_a_leak(self):
        for line in [
            '"DefaultConnection": "Server=db;Database=app;User Id=sa;Password=MyP@ssw0rd123;"',
            'spring.datasource.url=jdbc:mysql://prod-db:3306/app?user=root&password=Xk9mQz7Lp2w',
            'ConnectionString: "Host=db;Username=admin;Password=s3cr3tP@ss99"',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_canonical_secret_plumbing_is_not_flagged(self):
        for line in [
            'SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]',       # subscript env read
            'const GITHUB_TOKEN = process.env.GITHUB_TOKEN as string;',  # TS cast trailing
            'DB_PASSWORD=${DB_PASSWORD:-postgres}',               # bash param expansion
            'TOKEN_ENDPOINT=https://auth.example.com/oauth/token',  # config ABOUT a token
            'db_host = config["DB_HOST"]',
            'const pw = process.env.DB_PASSWORD;',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_real_secrets_still_caught_after_fp_guards(self):
        for line in [
            'DB_PASSWORD=prod.db.password.literal',   # round-13: dotted no-spaces = real
            'AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            'DB_PASSWORD=aQ7pR2xL9mZ4kT8v',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    # --- Workflow round-2 security findings (2026-07-07) ---
    def test_stripe_underscore_key_is_a_leak(self):
        for line in [
            'STRIPE_KEY=sk_live_EXAMPLEONLYnotreal01',
            "const stripe = require('stripe')('sk_live_EXAMPLEONLYnotreal01')",
            'rk_test_abcdefghij1234567890XY',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_env_plumbing_variants_not_flagged(self):
        for line in [
            'SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]  # set via env, never commit',
            'const apiKey = process.env.OPENAI_API_KEY || "";',
            '"apiKey": process.env.FIREBASE_API_KEY,',
            'export SECRET_KEY=$(openssl rand -hex 32)',
            'existingSecret: "postgres-credentials"',
            'secretName: my-app-secrets',
            'secretKeyRef: db-creds',
            'const t = process.env.TOKEN as string;',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_labeled_secret_with_comment_still_caught(self):
        for line in [
            'api_key: aQ7pR2xL9mZ4kT8vNN  # prod',   # a labeled secret WITH a comment still fires
            'DATABASE_PASSWORD=SuperSecret123!@#',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_dotted_lowercase_password_key_is_a_leak(self):
        for line in [
            'spring.datasource.password=Pa55word123',
            'jdbc.password=Postgres123',
            'hibernate.connection.password=RootDb2024',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_template_and_nested_call_values_not_flagged(self):
        for line in [
            'DATABASE_URL = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"',
            'SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_urlsafe(32))',
            'password: ${{secrets.DOCKER_PASSWORD}}',
            'DATABASE_URL=postgres://${DB_USER}:${DB_PASS}@db:5432/app',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_real_url_creds_still_caught_after_template_guard(self):
        self.assertIs(
            amb._line_has_secret('DATABASE_URL=postgres://admin:realSecret123@db/app'), True)

    def test_r4_more_leaks_caught(self):
        for line in [
            'MYSQL_PWD=xK9mP2qR7vL4nB8wZ3tY6uH1jF5dC0aS',
            'PGPASSWORD=Secret123 psql -h db -U app',
            'SENDGRID_API_KEY=SG.ngeVfQFYQlKU0ufo8x5d1A.TwL2iGABf9DHYTfWZtms1XkLq',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), True)

    def test_r4_type_path_message_interp_not_flagged(self):
        for line in [
            'provideCompletionItems(document: TextDocument, token: CancellationToken) {',
            '    token: OAuth2Token',
            '  "password": "Passwords must match",',
            'export GOOGLE_APPLICATION_CREDENTIALS=/home/me/keys/service-account.json',
            '  ansible_password: "{{vault_ansible_password}}"',
        ]:
            with self.subTest(line=line):
                self.assertIs(amb._line_has_secret(line), False)

    def test_r4_finding_regex_redos_safe(self):
        for inp in ("HIGH" + " " * 80000 + "x", "CRITICAL " + "-" * 80000):
            t = time.monotonic()
            amb.parse_prose_findings(inp)
            self.assertLess(time.monotonic() - t, 2.0)


if __name__ == "__main__":
    unittest.main()
