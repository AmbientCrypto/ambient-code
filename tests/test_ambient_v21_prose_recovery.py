"""P2 — the keystone: recover audit findings from a model that ignored the JSON
schema but followed the prose format (GLM 5.2), and LEARN from it. See
docs/plans/2026-07-06-stress-test-remediation.md."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin", "ambient")


def _load_module():
    loader = importlib.machinery.SourceFileLoader("ambient_cli_prose", _BIN)
    spec = importlib.util.spec_from_loader("ambient_cli_prose", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


amb = _load_module()

_MISSING = object()

# Verbatim shape of what GLM 5.2 actually returned in the stress test (prose,
# em-dashes, markdown bold on some headers) — the fixture that must recover.
GLM_PROSE = """## Audit: fixtures/stats.py

**HIGH (confidence: HIGH) — stats.py:10 — `top_k` slices `s[0:k-1]`, returning one too few elements.**
Scenario: `top_k([5,3,8,1], 3)` → sorted desc `[8,5,3,1]`, slice `[0:2]` returns `[8,5]` — only 2 of 3. Fix: `return s[0:k]`.

HIGH (confidence: HIGH) — stats.py:14 — `moving_avg` loop bound is off by one, dropping the final window.
Scenario: `moving_avg([1,2,3,4], 2)` → range(2) yields i=0,1 → misses the [3,4] window. Fix: `range(len(nums) - window + 1)`.

MEDIUM (confidence: HIGH) — stats.py:5 — `average([])` divides by zero.
Scenario: `average([])` → len 0 → ZeroDivisionError. Fix: guard empty input.

Verdict: FIX FIRST.
"""


# --- helpers ---------------------------------------------------------------
def _render_json(raw, model):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        amb.render_findings(raw, "json", api_key="", model=model)
    return json.loads(buf.getvalue())


def _rj(raw):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        amb.render_findings(raw, "json", api_key="", model="m")
    return json.loads(b.getvalue())


def _is_clean(e):
    return e["verdict"] == "SHIP" and not e.get("findings") and e["exit_code"] == 0


class ProseRecoveryTests(unittest.TestCase):
    def setUp(self):
        """Mirror the old autouse pytest fixture: point CAPABILITY_PATH at a
        temp file, reset the _CAP_CACHE process memo, and clear
        AMBIENT_TELEMETRY for the duration of each test."""
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        self.tmp_path = Path(tmp_dir)
        self._orig_capability_path = amb.CAPABILITY_PATH
        self._orig_cap_cache = amb._CAP_CACHE
        self._orig_telemetry = os.environ.pop("AMBIENT_TELEMETRY", _MISSING)
        amb.CAPABILITY_PATH = str(self.tmp_path / "caps.json")
        amb._CAP_CACHE = None

    def tearDown(self):
        amb._CAP_CACHE = None  # reset after, as before (fixture post-yield)
        amb.CAPABILITY_PATH = self._orig_capability_path
        amb._CAP_CACHE = self._orig_cap_cache
        if self._orig_telemetry is _MISSING:
            os.environ.pop("AMBIENT_TELEMETRY", None)
        else:
            os.environ["AMBIENT_TELEMETRY"] = self._orig_telemetry

    # --- parser -----------------------------------------------------------
    def test_recovers_all_findings_from_real_glm_prose(self):
        obj = amb.parse_prose_findings(GLM_PROSE)
        assert obj is not None
        assert len(obj["findings"]) == 3
        sevs = [f["severity"] for f in obj["findings"]]
        assert sevs == ["HIGH", "HIGH", "MEDIUM"]
        first = obj["findings"][0]
        assert first["file"] == "stats.py" and first["line"] == 10
        assert first["confidence"] == "HIGH"
        assert "top_k" in first["title"]
        assert "top_k" in first["scenario"]
        assert obj["verdict"] == "FIX FIRST"

    def test_title_strips_markdown_bold_and_trailing_period(self):
        obj = amb.parse_prose_findings(GLM_PROSE)
        assert not obj["findings"][0]["title"].endswith("*")
        assert not obj["findings"][0]["title"].endswith(".")

    def test_clean_code_prose_yields_empty_findings_with_verdict(self):
        obj = amb.parse_prose_findings("The code is sound, no defects.\nVerdict: SHIP")
        assert obj is not None
        assert obj["findings"] == []
        assert obj["verdict"] == "SHIP"

    def test_garbage_returns_none(self):
        assert amb.parse_prose_findings("hello, this is not an audit at all") is None
        assert amb.parse_prose_findings("") is None

    # --- Codex-found prose bugs --------------------------------------------
    def test_bulleted_finding_header_falls_to_raw_not_faked_clean(self):
        # A '- ' bulleted finding header is a diff/list marker we DON'T parse as a
        # live finding — but its severity+confidence+file:line means we must NOT
        # fake a clean SHIP either; it falls to the raw envelope (returns None).
        txt = ("- HIGH (confidence: HIGH) — stats.py:10 — off-by-one bug.\n"
               "Scenario: x.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_unparsable_finding_lines_do_not_fake_clean_verdict(self):
        # A finding-shaped header (severity + confidence + file:line) we CAN'T fully
        # parse must NOT be reported as a clean SHIP with zero findings.
        txt = ("HIGH (confidence: HIGH) — a.py:42 malformed, missing 2nd separator\n"
               "Verdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_last_verdict_wins_over_quoted_one(self):
        # Codex: a 'Verdict: SHIP' quoted in a scenario preceded the real verdict.
        txt = ("HIGH (confidence: HIGH) — a.py:3 — bug.\n"
               "Scenario: the doc says 'Verdict: SHIP' but it's wrong.\n"
               "Verdict: FIX FIRST\n")
        obj = amb.parse_prose_findings(txt)
        assert obj["verdict"] == "FIX FIRST"

    def test_clean_ship_prose_mentioning_severity_is_not_rejected(self):
        # Codex round 2: "no HIGH (confidence: HIGH) issues remain" has no file:line
        # → it is a real clean SHIP, not an unparseable finding.
        txt = "No defects found. No HIGH (confidence: HIGH) issues remain.\nVerdict: SHIP\n"
        obj = amb.parse_prose_findings(txt)
        assert obj is not None and obj["findings"] == [] and obj["verdict"] == "SHIP"

    def test_diff_plus_line_is_not_parsed_as_finding(self):
        # Codex round 2: a quoted '+ HIGH (confidence…) — f:1' inside a diff must not
        # become a live finding (it now falls to the safe raw envelope instead).
        txt = ("```diff\n+ HIGH (confidence: HIGH) — README.md:1 — old quoted output\n"
               "```\nVerdict: SHIP\n")
        obj = amb.parse_prose_findings(txt)
        assert obj is None or len(obj["findings"]) == 0

    def test_numbered_finding_is_parsed_not_faked_clean(self):
        # Codex round 3: '1. HIGH …' numbered findings were dropped, then faked a
        # clean SHIP. They must now parse as real findings.
        txt = ("1. HIGH (confidence: HIGH) — a.py:1 — auth bypass.\n"
               "Scenario: x.\nFix: y.\nVerdict: SHIP\n")
        obj = amb.parse_prose_findings(txt)
        assert obj is not None and len(obj["findings"]) == 1
        assert obj["findings"][0]["file"] == "a.py"

    def test_clean_prose_without_fileline_stays_clean(self):
        # A clean SHIP that names a severity WITHOUT a file:line stays clean
        # (the round-2 guarantee — no false rejection on "no HIGH issues").
        txt = ("No defects found. No HIGH (confidence: HIGH) severity issues.\n"
               "Verdict: SHIP\n")
        obj = amb.parse_prose_findings(txt)
        assert obj is not None and obj["findings"] == [] and obj["verdict"] == "SHIP"

    def test_severity_with_fileline_biases_to_raw_not_fake_clean(self):
        # Codex round 8: a line with severity + confidence + file:line (any
        # separator) can't be reliably told apart from a real colon/comma finding —
        # so we bias to the SAFE raw envelope rather than risk faking a clean SHIP.
        for txt in (
            "HIGH (confidence: HIGH) at a.py:7: auth bypass — real\nVerdict: SHIP\n",
            "HIGH (confidence: HIGH), a.py:7, auth bypass\nVerdict: SHIP\n",
        ):
            with self.subTest(txt=txt):
                assert amb.parse_prose_findings(txt) is None

    def test_colon_separated_finding_parses(self):
        # Codex round 4/12: a colon-separated finding now PARSES (better than raw).
        obj = amb.parse_prose_findings(
            "HIGH (confidence: HIGH): a.py:7 — hidden real defect\nVerdict: SHIP\n")
        assert obj is not None and len(obj["findings"]) == 1
        assert obj["findings"][0]["file"] == "a.py"

    def test_at_style_finding_does_not_fake_clean(self):
        # Codex round 4: an 'at'-style finding (no separator char before file:line)
        # falls to the safe raw envelope rather than faking a clean SHIP.
        assert amb.parse_prose_findings(
            "HIGH (confidence: HIGH) at a.py:7 — hidden real defect\nVerdict: SHIP\n") is None

    def test_space_after_colon_finding_does_not_fake_clean(self):
        # Codex round 5: 'a.py: 7' (space after colon) must still parse / not fake clean.
        txt = ("HIGH (confidence: HIGH) — a.py: 7 — hidden real defect\n"
               "Scenario: x.\nVerdict: SHIP\n")
        obj = amb.parse_prose_findings(txt)
        assert obj is not None and len(obj["findings"]) == 1
        assert obj["findings"][0]["line"] == 7

    def test_markdown_heading_finding_does_not_fake_clean(self):
        # Codex round 6: a '### HIGH …' Markdown-heading finding must parse, not
        # fake a clean SHIP.
        txt = ("### HIGH (confidence: HIGH) — a.py:7 — auth bypass.\n"
               "Scenario: unauthenticated request succeeds.\n"
               "Fix: check auth.\nVerdict: SHIP\n")
        obj = amb.parse_prose_findings(txt)
        assert obj is not None and len(obj["findings"]) == 1
        assert obj["findings"][0]["file"] == "a.py"

    def test_labeled_heading_finding_does_not_fake_clean(self):
        # Codex round 7: '### Finding 1: HIGH …' (severity not first) must not fake
        # a clean SHIP — it falls to the safe raw envelope.
        txt = ("### Finding 1: HIGH (confidence: HIGH) — a.py:7 — auth bypass.\n"
               "Scenario: x.\nFix: y.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_confidence_last_finding_does_not_fake_clean(self):
        # Codex round 9/11: a header with confidence AFTER the file:line must not
        # fake a clean SHIP. Since confidence is now optional in the parser, this
        # PARSES as a real finding (even better than falling to raw).
        txt = ("HIGH — a.py:7 — auth bypass, unauthenticated access (confidence: HIGH).\n"
               "Verdict: SHIP\n")
        obj = amb.parse_prose_findings(txt)
        assert obj is not None and len(obj["findings"]) == 1
        assert obj["findings"][0]["file"] == "a.py"

    def test_unparenthesized_confidence_finding_does_not_fake_clean(self):
        # Codex round 10: 'HIGH — Confidence: HIGH — a.py:7 — …' (confidence not in
        # parens) must not fake a clean SHIP.
        txt = ("HIGH — Confidence: HIGH — a.py:7 — auth bypass.\nVerdict: SHIP\n")
        obj = amb.parse_prose_findings(txt)
        # It PARSES the finding (a HIGH finding forces FIX FIRST at render time) —
        # the point is it is not silently dropped into a clean SHIP.
        assert obj is not None and len(obj["findings"]) == 1
        assert obj["findings"][0]["file"] == "a.py"

    def test_finding_without_confidence_parses_not_faked_clean(self):
        # Codex round 11: a finding that omits the confidence label entirely must
        # still parse (not fake a clean SHIP).
        txt = ("HIGH — a.py:7 — auth bypass lets unauthenticated users read data.\n"
               "Scenario: x.\nFix: y.\nVerdict: SHIP\n")
        obj = amb.parse_prose_findings(txt)
        assert obj is not None and len(obj["findings"]) == 1
        assert obj["findings"][0]["file"] == "a.py"
        assert obj["findings"][0]["confidence"] == "HIGH"

    def test_colon_no_confidence_finding_parses(self):
        # Codex round 12: 'HIGH: file:line — title' (no confidence, colon separator)
        # must parse, not fake a clean SHIP.
        obj = amb.parse_prose_findings("HIGH: app/auth.py:42 — auth bypass\nVerdict: SHIP\n")
        assert obj is not None and len(obj["findings"]) == 1
        assert obj["findings"][0]["file"] == "app/auth.py"

    def test_no_confidence_labeled_findings_do_not_fake_clean(self):
        # Codex round 13: no-confidence labeled/bulleted finding headers the parser
        # can't reach must fall to raw, not fake a clean SHIP.
        for txt in [
            "### Finding 1: HIGH — app/auth.py:42 — auth bypass\nVerdict: SHIP\n",
            "- HIGH — app/auth.py:42 — auth bypass\nVerdict: SHIP\n",
        ]:
            with self.subTest(txt=txt):
                assert amb.parse_prose_findings(txt) is None

    def test_fieldlist_multiline_finding_does_not_fake_clean(self):
        # Codex round 14: a field-list finding (Severity:/File:/Line: on separate
        # lines) must not fake a clean SHIP.
        txt = ("Finding 1:\nSeverity: HIGH\nConfidence: HIGH\nFile: app/auth.py\n"
               "Line: 42\nDefect: auth bypass.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_fieldlist_with_file_line_label_does_not_fake_clean(self):
        # Codex round 15: a field-list finding with 'File: a.py:42' (file:line on the
        # File line, not a separate 'Line:') must not fake a clean SHIP.
        txt = ("Finding:\nSeverity: HIGH\nFile: app/auth.py:42\n"
               "Defect: auth bypass.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_fieldlist_location_line_word_does_not_fake_clean(self):
        # Codex round 16: 'Location: app/auth.py line 42' ('line 42', no colon).
        txt = ("Finding:\nSeverity: HIGH\nLocation: app/auth.py line 42\n"
               "Defect: auth bypass.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_fieldlist_plural_lines_range_does_not_fake_clean(self):
        # Codex round 17: 'Location: app/auth.py lines 42-45' (plural, range).
        txt = ("Finding:\nSeverity: HIGH\nLocation: app/auth.py lines 42-45\n"
               "Defect: bug.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_fieldlist_any_location_phrasing_does_not_fake_clean(self):
        # Codex round 15-18: any location phrasing in a field-list finding.
        for loc in [
            "Location: app/auth.py, line number 42",
            "Location: app/auth.py lines 42-45",
            "File: app/auth.py:42",
            "Line: 42",
        ]:
            with self.subTest(loc=loc):
                txt = f"Finding:\nSeverity: HIGH\n{loc}\nDefect: bug.\nVerdict: SHIP\n"
                assert amb.parse_prose_findings(txt) is None

    def test_fieldlist_standalone_line_number_does_not_fake_clean(self):
        # Codex round 19: standalone 'Line number 42' (no colon, no File: label).
        txt = ("Finding:\nSeverity: HIGH\nLine number 42\n"
               "Defect: empty token bypass.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_genuinely_clean_audits_stay_clean(self):
        for txt in [
            "The code is sound. No defects found.\nVerdict: SHIP\n",
            "Reviewed auth.py thoroughly. No HIGH or MEDIUM issues.\nVerdict: SHIP\n",
        ]:
            with self.subTest(txt=txt):
                obj = amb.parse_prose_findings(txt)
                assert obj is not None and obj["findings"] == [] and obj["verdict"] == "SHIP"

    def test_dash_separated_fieldlist_does_not_fake_clean(self):
        # Codex round 20: a field-list using dash separators ('Severity - HIGH').
        txt = ("Finding 1:\nSeverity - HIGH\nConfidence - HIGH\nFile - app/auth.py\n"
               "Line number 42\nDefect: auth bypass.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_dash_bulleted_fieldlist_does_not_fake_clean(self):
        # Codex round 21: a '- ' dash-bulleted field-list finding.
        txt = ("Finding 1:\n- Severity: HIGH\n- Confidence: HIGH\n- File: app/auth.py\n"
               "- Line: 42\n- Defect: auth bypass.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_path_line_word_finding_parses(self):
        # Codex round 22: 'app/auth.py line 42' (word 'line', no colon) now parses.
        o = amb.parse_prose_findings(
            "HIGH (confidence: HIGH) — app/auth.py line 42 — auth bypass.\nVerdict: SHIP\n")
        assert o is not None and len(o["findings"]) == 1
        assert o["findings"][0]["file"] == "app/auth.py" and o["findings"][0]["line"] == 42

    def test_clean_audit_mentioning_lines_count_stays_clean(self):
        o = amb.parse_prose_findings("Reviewed 500 lines across 3 files. No issues.\nVerdict: SHIP\n")
        assert o is not None and o["findings"] == [] and o["verdict"] == "SHIP"

    def test_github_anchor_finding_parses(self):
        # Codex round 23: 'app/auth.py#L42' GitHub-style anchor.
        o = amb.parse_prose_findings(
            "HIGH (confidence: HIGH) — app/auth.py#L42 — auth bypass.\nVerdict: SHIP\n")
        assert o is not None and len(o["findings"]) == 1
        assert o["findings"][0]["file"] == "app/auth.py" and o["findings"][0]["line"] == 42

    def test_all_single_line_fileline_notations_parse(self):
        # Codex round 22-24: every file:line notation must parse, not fake clean.
        for loc in ["line number 42", "line no. 42", "line no 42", "#L42", "line 42", ":42"]:
            with self.subTest(loc=loc):
                sep = "" if loc[0] in ":#" else " "
                o = amb.parse_prose_findings(
                    f"HIGH (confidence: HIGH) — app/auth.py{sep}{loc} — auth bypass.\nVerdict: SHIP\n")
                assert o is not None and len(o["findings"]) == 1
                assert o["findings"][0]["line"] == 42

    def test_bold_markdown_fieldlist_does_not_fake_clean(self):
        # Codex round 25: markdown-bold field labels ('**Severity:** HIGH').
        txt = ("Finding 1:\n**Severity:** HIGH\n**File:** app/auth.py\n**Line:** 42\n"
               "**Defect:** auth bypass.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_fieldlist_any_location_format_does_not_fake_clean(self):
        # ROOT fix: a 'Severity: <level>' label alone marks a field-list finding —
        # ANY location format falls to raw, never a fake clean SHIP.
        for loc in [
            "Line: 42", "Line 42", "Line number 42", "File: a.py:42",
            "Location: a.py, line 42", "Where: an unpredicted format 42", "Position: 42",
        ]:
            with self.subTest(loc=loc):
                txt = f"Finding:\nSeverity: HIGH\n{loc}\nDefect: bug.\nVerdict: SHIP\n"
                assert amb.parse_prose_findings(txt) is None

    def test_fieldlist_root_fix_keeps_clean_audits_clean(self):
        for txt in [
            "Code is sound.\nVerdict: SHIP\n",
            "No defects. Overall severity: LOW. Reviewed 500 lines.\nVerdict: SHIP\n",
        ]:
            with self.subTest(txt=txt):
                obj = amb.parse_prose_findings(txt)
                assert obj is not None and obj["findings"] == [] and obj["verdict"] == "SHIP"

    def test_severity_label_any_prefix_does_not_fake_clean(self):
        # ROOT fix + round 26: a 'Severity: <level>' field label with ANY prefix
        # (numbered/bulleted/bold/heading) marks a finding -> raw, never fake clean.
        for pre in ["", "1. ", "2) ", "- ", "* ", "**", "### "]:
            with self.subTest(pre=pre):
                txt = f"Finding:\n{pre}Severity: HIGH\nFile: a.py\nLine: 42\nVerdict: SHIP\n"
                assert amb.parse_prose_findings(txt) is None

    def test_markdown_table_finding_does_not_fake_clean(self):
        # Codex round 27: a Markdown table finding row.
        txt = ("| Severity | File | Line | Defect |\n|---|---|---|---|\n"
               "| HIGH | app/auth.py | 42 | Auth bypass. |\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_benign_table_without_severity_stays_clean(self):
        txt = "| File | Status |\n|---|---|\n| auth.py | OK |\nVerdict: SHIP\n"
        obj = amb.parse_prose_findings(txt)
        assert obj is not None and obj["findings"] == [] and obj["verdict"] == "SHIP"

    def test_any_finding_overrides_ship_verdict(self):
        # Codex round 28: a SHIP verdict can't coexist with ANY finding.
        for sev, want in [("HIGH", "FIX FIRST"), ("CRITICAL", "FIX FIRST"),
                          ("MEDIUM", "NEEDS WORK"), ("LOW", "NEEDS WORK")]:
            with self.subTest(sev=sev, want=want):
                raw = json.dumps({"findings": [{"severity": sev, "confidence": "HIGH", "file": "a.py",
                                                "line": 1, "title": "x", "defect": "d",
                                                "scenario": "s", "fix": "f"}], "verdict": "SHIP"})
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    amb.render_findings(raw, "json", api_key="", model="m")
                assert json.loads(buf.getvalue())["verdict"] == want

    def test_empty_json_plus_prose_finding_recovers(self):
        # Codex round 29: empty JSON '{"findings":[],"verdict":"SHIP"}' followed by a
        # real prose finding must recover the finding, not fake clean.
        raw = ('{"findings":[],"verdict":"SHIP"}\n'
               'HIGH (confidence: HIGH) — app/auth.py:42 — auth bypass.\nVerdict: FIX FIRST\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            amb.render_findings(raw, "json", api_key="", model="m")
        env = json.loads(buf.getvalue())
        assert len(env["findings"]) == 1 and env["verdict"] != "SHIP"

    def test_empty_json_plus_fieldlist_or_table_not_clean(self):
        # Codex round 30: empty JSON then a field-list/table finding must not be clean.
        for prose in [
            "Finding 1:\nSeverity: HIGH\nFile: app/auth.py\nLine: 42\nDefect: bug.",
            "| HIGH | app/auth.py | 42 | bug |",
        ]:
            with self.subTest(prose=prose):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    amb.render_findings('{"findings":[],"verdict":"SHIP"}\n' + prose + "\n",
                                        "json", api_key="", model="m")
                env = json.loads(buf.getvalue())
                assert env["verdict"] != "SHIP" and env["exit_code"] != 0

    def test_all_inline_fileline_notations_parse(self):
        # Codex round 22-31: comprehensive inline file:line notation coverage.
        for loc in [":42", ":L42", "#42", "#L42", " L42", " line 42", " line number 42"]:
            with self.subTest(loc=loc):
                o = amb.parse_prose_findings(
                    f"HIGH (confidence: HIGH) — app/auth.py{loc} — auth bypass.\nVerdict: SHIP\n")
                assert o is not None and len(o["findings"]) == 1 and o["findings"][0]["line"] == 42

    def test_severity_label_variants_do_not_fake_clean(self):
        # Codex round 25-32: any 'Severity [word]: <level>' field label -> raw.
        for lbl in ["Severity: HIGH", "Severity level: HIGH",
                    "Severity rating: HIGH", "**Severity:** HIGH", "1. Severity: HIGH", "Severity - HIGH"]:
            with self.subTest(lbl=lbl):
                txt = f"Finding:\n{lbl}\nFile: a.py\nLine: 42\nVerdict: SHIP\n"
                assert amb.parse_prose_findings(txt) is None

    def test_severity_word_in_clean_prose_stays_clean(self):
        o = amb.parse_prose_findings(
            "Severity of low-priority items is minimal, all reviewed.\nVerdict: SHIP\n")
        assert o is not None and o["findings"] == [] and o["verdict"] == "SHIP"

    def test_finding_heading_with_level_does_not_fake_clean(self):
        # Codex round 33: severity in the 'Finding'/'Issue'/'Bug' heading, not a label.
        for h in ["Finding 1: HIGH", "Issue: CRITICAL", "Bug 3 - MEDIUM", "Vulnerability: HIGH"]:
            with self.subTest(h=h):
                assert amb.parse_prose_findings(f"{h}\nFile: a.py\nLine: 42\nVerdict: SHIP\n") is None

    def test_findings_none_summary_stays_clean(self):
        o = amb.parse_prose_findings("Findings: none. No HIGH-severity issues.\nVerdict: SHIP\n")
        assert o is not None and o["findings"] == [] and o["verdict"] == "SHIP"

    def test_no_severity_fieldlist_finding_does_not_fake_clean(self):
        # Codex round 34: a field-list finding with Defect/File/Line but NO severity.
        txt = ("Finding 1:\nFile: app/auth.py\nLine: 42\nDefect: missing auth check.\n"
               "Scenario: x.\nFix: require auth.\nVerdict: SHIP\n")
        assert amb.parse_prose_findings(txt) is None

    def test_coverage_stats_prose_stays_clean(self):
        o = amb.parse_prose_findings(
            "File coverage: 95%. Line coverage: 90%. All good.\nVerdict: SHIP\n")
        assert o is not None and o["findings"] == [] and o["verdict"] == "SHIP"

    def test_prose_regexes_are_redos_safe(self):
        # A crafted 500KB adversarial line must not hang the prose scanner.
        for inp in ("Finding: " + "a/" * 250000 + ":x",
                    "HIGH (confidence: HIGH) — " + "a" * 500000,
                    "a/" * 300000 + ":5"):
            t = time.monotonic()
            amb._text_has_unparsed_finding(inp)
            amb.parse_prose_findings(inp)
            # Generous budget: linear scans of these ~600KB inputs finish in
            # well under a second normally, but can spike on a slow CI runner
            # (Windows + old Python under matrix load). A genuine ReDoS would
            # backtrack for many seconds to minutes, so 10s still catches it
            # while tolerating a slow-but-linear run.
            assert time.monotonic() - t < 10.0

    def test_no_severity_finding_with_scenario_field_not_clean(self):
        # Codex round 35: a 'Finding:' heading + file:line + Scenario/Fix (no severity).
        raw = ('{"findings":[],"verdict":"SHIP"}\n'
               'Finding: app/auth.py:42 missing auth check.\n'
               'Scenario: GET /admin without login.\nFix: require auth.\nVerdict: SHIP\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            amb.render_findings(raw, "json", api_key="", model="m")
        env = json.loads(buf.getvalue())
        assert env["verdict"] != "SHIP" and env["exit_code"] != 0

    def test_workflow_hidden_findings_are_not_clean(self):
        for raw in [
            # Workflow round-2: severity phrased as an ADJECTIVE label, P/Sev taxonomy,
            # XML tags, CWE ref, 'Where:' label — all real hidden findings.
            '{"findings": [], "verdict": "SHIP"}\n- HIGH-risk: SQL injection in db/query.py:88 lets any user dump users.',
            '{"findings": [], "verdict": "SHIP"}\nCritical-severity: auth bypass in api/auth.py:14.',
            '{"findings": [], "verdict": "SHIP"}\nhigh-severity issue in db/query.py:88 allows SQLi.',
            '{"findings": [], "verdict": "SHIP"}\n| Sev | File | Line |\n|--|--|--|\n| P0 | pay/charge.py | 210 | client-controlled amount |',
            '{"findings": [], "verdict": "SHIP"}\nSev1: predictable reset token in auth/reset.py:9.',
            '{"findings": [], "verdict": "SHIP"}\nBlocker: SQL injection in db/query.py:42.',
            '{"findings": [], "verdict": "SHIP"}\n<severity>HIGH</severity> <file>src/auth.py</file> <line>42</line> - auth bypass.',
            '{"findings": []}\nCWE-798: hardcoded AWS key in config.py, line 12.',
            '{"findings": []}\nWhere: auth.py line 42 - token compared with == (timing attack).',
        ]:
            with self.subTest(raw=raw):
                assert not _is_clean(_rj(raw))

    def test_workflow_clean_audits_stay_clean(self):
        for raw in [
            # Workflow: a severity ADJECTIVE in a clean 'sections audited' sentence, a
            # valid clean JSON with innocuous prose, a numbered review summary.
            'I audited thoroughly.\nCritical sections audited: crypto.py:120 - constant-time compare verified correct.\nNo defects found.\nVerdict: SHIP',
            'Critical sections audited: crypto.py:120 - verified correct.\n{"findings": [], "verdict": "SHIP"}',
            '1. Critical paths - payment/charge.py:57 - idempotency verified, no double-charge.\n2. Error handling - solid.\nVerdict: SHIP',
        ]:
            with self.subTest(raw=raw):
                assert _is_clean(_rj(raw))

    def test_workflow_r3_hidden_findings_not_clean(self):
        for raw in [
            '{"findings": [], "verdict": "SHIP"}\nPriority: High\nFile: pay/charge.py\nDefect description: float() overcharges.',
            '{"findings": [], "verdict": "SHIP"}\nThe token is compared with == in auth/session.py:88, enabling a timing attack that recovers the session secret.',
            'Overall solid. the password is logged in plaintext at auth/login.py:53, which leaks credentials.\nVerdict: SHIP',
            'Minor: the retry loop in client.py:210 spins forever on 503s.\nVerdict: SHIP',
            '{"findings": [], "verdict": "SHIP"}\nMinor: the retry loop in client.py:210 spins forever.',
        ]:
            with self.subTest(raw=raw):
                b = io.StringIO()
                with contextlib.redirect_stdout(b):
                    amb.render_findings(raw, "json", api_key="", model="m")
                e = json.loads(b.getvalue())
                assert not (e["verdict"] == "SHIP" and not e.get("findings") and e["exit_code"] == 0)

    def test_workflow_r3_high_level_summary_stays_clean(self):
        raw = ('High-level summary: reviewed utils/parse.py:88 and the caller; logic is '
               'sound. No issues found.\nVerdict: SHIP')
        b = io.StringIO()
        with contextlib.redirect_stdout(b):
            amb.render_findings(raw, "json", api_key="", model="m")
        e = json.loads(b.getvalue())
        assert e["verdict"] == "SHIP" and not e.get("findings") and e["exit_code"] == 0

    def test_workflow_r4_range_msvc_heading_findings_not_clean(self):
        for raw in [
            '{"findings": [], "verdict": "SHIP"}\nHIGH — db/query.py:120-135 — SQL injection.\nVerdict: SHIP',
            '{"findings": [], "verdict": "SHIP"}\nHIGH — src/auth.c(142) — command injection.\nVerdict: SHIP',
            '{"findings": [], "verdict": "SHIP"}\n\n## CRITICAL\n\nThe reset handler at api/reset.py:88 trusts the token.',
            'Reviewed.\n\n**HIGH**\n\nThe token at auth/session.py:120 is compared with ==.\nVerdict: SHIP',
        ]:
            with self.subTest(raw=raw):
                b = io.StringIO()
                with contextlib.redirect_stdout(b):
                    amb.render_findings(raw, "json", api_key="", model="m")
                e = json.loads(b.getvalue())
                assert not (e["verdict"] == "SHIP" and not e.get("findings") and e["exit_code"] == 0)

    def test_workflow_r4_illustrative_resolved_historical_stay_clean(self):
        for raw in [
            'I found no defects.\n\nFor illustration, a finding would have looked like:\n\nHIGH (confidence: HIGH) — example.py:10 — some issue.\nVerdict: SHIP',
            'Re-audit after fixes:\n\nHIGH (confidence: HIGH) — auth.py:88 — RESOLVED: constant-time now used.\nVerdict: SHIP',
            '{"findings": [], "verdict": "SHIP"}\n\nNote: the previous audit round reported:\n\nHIGH (confidence: HIGH) — auth.py:42 — token compared with ==.',
        ]:
            with self.subTest(raw=raw):
                b = io.StringIO()
                with contextlib.redirect_stdout(b):
                    amb.render_findings(raw, "json", api_key="", model="m")
                e = json.loads(b.getvalue())
                assert e["verdict"] == "SHIP" and not e.get("findings") and e["exit_code"] == 0

    def test_prose_regexes_redos_safe_on_repeated_punctuation(self):
        for ch in ("*", " ", "-", "#"):
            inp = "**HIGH" + ch * 200000
            t = time.monotonic()
            amb.parse_prose_findings(inp)
            amb._text_has_unparsed_finding(inp)
            assert time.monotonic() - t < 2.5

    def test_high_finding_forces_non_ship_verdict(self):
        # Codex round 2: a model-stated SHIP can't coexist with a HIGH finding.
        clean = json.dumps({"findings": [{"severity": "HIGH", "confidence": "HIGH",
                                          "file": "a.py", "line": 1, "title": "bug",
                                          "defect": "d", "scenario": "s", "fix": "f"}],
                            "verdict": "SHIP"})
        env = _render_json(clean, "m")
        assert env["verdict"] == "FIX FIRST"

    def test_reducer_output_does_not_train_structured_ok(self):
        # Codex: render_findings trained structured_json=True from the reducer's own
        # JSON string (which carries _unparsed_chunks), even on partial coverage.
        amb._CAP_CACHE = None
        reducer_json = json.dumps({"findings": [], "verdict": "NEEDS WORK",
                                   "_unparsed_chunks": 1, "_repaired_chunks": 0})
        _render_json(reducer_json, "reduced/model")
        assert amb.cap_state("reduced/model", "structured_json") != "ok"

    # --- render integration + learning --------------------------------------
    def test_json_render_recovers_findings_and_is_not_partial(self):
        env = _render_json(GLM_PROSE, "z-ai/glm-5.2")
        assert env["status"] == "ok"          # recovered fully — NOT partial
        assert env["exit_code"] == 0
        assert len(env["findings"]) == 3
        assert env["verdict"] == "FIX FIRST"
        assert env.get("recovered_from_prose") is True

    def test_prose_recovery_records_model_as_structured_unreliable(self):
        _render_json(GLM_PROSE, "z-ai/glm-5.2")
        # one prose recovery = one failure outcome; a second confirms unreliable
        _render_json(GLM_PROSE, "z-ai/glm-5.2")
        assert amb.cap_state("z-ai/glm-5.2", "structured_json") == "unreliable"

    def test_clean_json_records_model_as_structured_ok(self):
        clean = json.dumps({"findings": [{"severity": "LOW", "confidence": "LOW",
                                          "file": "a.py", "line": 1, "title": "x",
                                          "defect": "x", "scenario": "s", "fix": "f"}],
                            "verdict": "NEEDS WORK"})
        _render_json(clean, "moonshotai/kimi-k2.7-code")
        assert amb.cap_state("moonshotai/kimi-k2.7-code", "structured_json") == "ok"

    def test_total_garbage_still_emits_valid_empty_envelope(self):
        env = _render_json("~~~ not parseable, not prose ~~~", "some/model")
        assert env["status"] == "partial"
        assert env["findings"] == []
        assert env["exit_code"] == amb.EXIT_PARTIAL
        assert amb.cap_state("some/model", "structured_json") != "ok"


if __name__ == "__main__":
    unittest.main()
