"""Register read-only SEC views on a DuckDB connection.

- ``sec_13f_submission`` -- one row per filing: ``filing_date`` (the
  point-in-time fact), ``period`` (the quarter end reported), ``cik``.
- ``sec_13f_coverpage`` -- the manager's name, ``crd_number`` (joins Form ADV),
  report type and amendment fields.
- ``sec_13f_holdings`` -- every information-table row with its filing's
  ``filing_date``, ``period``, manager and amendment fields, ``value_usd``
  and ``shares``. Nothing is deduplicated: an amendment appears next to the
  filing it amends, put and call rows next to share rows. Consumers resolve
  amendments (``is_amendment``, ``amendment_type``) and must never use a row
  before its ``filing_date``.

- ``sec_13f_manager_match`` -- one row per 13F filer (``cik``): the Form ADV
  ``crd_number`` it maps to and how (``match_kind``: ``crd`` from a cover
  page since 2023, else ``cik`` when an ADV adviser carries the same CIK,
  else ``name`` when the normalised manager name equals exactly one
  adviser's business or legal name; ambiguous names match nothing). The
  cohort view uses it for filings that carry no CRD (DD-002 WP16).
- ``sec_13f_filings_effective`` / ``sec_13f_holdings_effective`` -- the
  amendment rule applied (DD-002 WP9 step 1): per ``(cik, period)`` the latest
  original or ``RESTATEMENT`` filing is the base and every ``NEW HOLDINGS``
  amendment filed on or after it is added; an untyped amendment counts as a
  restatement, an addition with no base stands alone. ``effective_filing_date``
  is the date of the last filing that shaped the manager's holdings for the
  period (the point-in-time column of the resolved set); a row's own
  ``filing_date`` can be earlier. Nothing is cut by lateness here: a
  restatement filed years later replaces the original in this view. The long
  ladder applies a lateness cutoff through ``effective_filings_sql``.

``value_usd`` multiplies the reported value by the filing's ``value_factor``
from ``FILING_UNITS`` (``form13f.build_units``): the form switched from
thousands of dollars to dollars for filings on or after 2023-01-03, but some
filers lagged, so each filing is classified against the other filers of the
same securities. ``units_evidence`` says how (``with_crowd``, ``below_crowd``,
``above_crowd``, or ``assumed`` from the date alone).

Best-effort: views appear only once data exists. Idempotent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sec import adv, form13f
from sec.config import sec_root

SUBMISSION_VIEW = "sec_13f_submission"
COVER_VIEW = "sec_13f_coverpage"
HOLDINGS_VIEW = "sec_13f_holdings"
UNITS_VIEW = "sec_13f_filing_units"
STATE_VIEW = "sec_13f_state"
ADV_VIEW = "sec_adv_advisers"
COHORT_VIEW = "sec_13f_manager_cohort"
MATCH_VIEW = "sec_13f_manager_match"
FILINGS_EFFECTIVE_VIEW = "sec_13f_filings_effective"
#: ``match_kind`` values, in priority order.
MATCH_KINDS: tuple[str, ...] = ("crd", "cik", "name")
#: Suffixes and fillers dropped from a manager name before an exact comparison.
_NAME_NOISE = (
    r"\b(LLC|L L C|LP|L P|INC|INCORPORATED|LTD|LIMITED|CORP|CORPORATION|CO|COMPANY|PLC|LLP|THE|AND|&)\b"
)


def name_key_sql(column: str) -> str:
    """SQL for the normalised form of a manager name: upper case, letters and digits only,
    the corporate suffixes and fillers removed, single spaces, trimmed."""
    return (
        "nullif(trim(regexp_replace(regexp_replace(regexp_replace(upper("
        f"{column}), '[^A-Z0-9 ]', ' ', 'g'), '{_NAME_NOISE}', ' ', 'g'), ' +', ' ', 'g')), '')"
    )
HOLDINGS_EFFECTIVE_VIEW = "sec_13f_holdings_effective"
#: ``filing_kind`` values of an effective filing.
FILING_KINDS: tuple[str, ...] = ("original", "restatement", "addition")
DOLLARS_FROM = form13f.DOLLARS_FROM.isoformat()

_DATE = "CAST(strptime({col}, '%d-%b-%Y') AS DATE)"


def register(con: Any, *, root: Path | None = None) -> dict[str, bool]:
    base = Path(root) if root is not None else sec_root()
    result = {HOLDINGS_VIEW: _register_13f(con, base)}
    result[HOLDINGS_EFFECTIVE_VIEW] = result[HOLDINGS_VIEW] and register_effective(con)
    result[ADV_VIEW] = _register_adv(con, base)
    result[MATCH_VIEW] = result[HOLDINGS_VIEW] and result[ADV_VIEW] and register_match(con)
    result[COHORT_VIEW] = result[MATCH_VIEW] and register_cohort(con)
    return result


def _register_adv(con: Any, base: Path) -> bool:
    """``sec_adv_advisers``: one row per adviser per monthly snapshot (typed columns)."""
    store = adv.Store(base)
    if not store.snapshots_on_disk():
        return False
    glob = (store.snapshot_dir / "*.parquet").as_posix()
    columns = {
        r[0]
        for r in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{glob}', union_by_name=true)"
        ).fetchall()
    }
    cik_sql = 'TRY_CAST(trim("CIK#") AS BIGINT)' if "CIK#" in columns else "NULL::BIGINT"
    con.execute(
        f"CREATE OR REPLACE VIEW {ADV_VIEW} AS "
        "SELECT CAST(snapshot_date AS DATE) AS snapshot_date, "
        'TRY_CAST(trim("Organization CRD#") AS BIGINT) AS crd_number, "SEC#" AS sec_number, '
        f"{cik_sql} AS cik, "
        '"Primary Business Name" AS name, '
        '"Legal Name" AS legal_name, '
        'upper("7B") = \'Y\' AS advises_private_funds, '
        'CASE WHEN "Any Hedge Funds" IS NULL OR "Any Hedge Funds" = \'\' THEN NULL '
        'ELSE upper("Any Hedge Funds") = \'Y\' END AS any_hedge_funds, '
        'TRY_CAST("Total number of Hedge funds" AS INTEGER) AS n_hedge_funds, '
        'TRY_CAST("Count of Private Funds - 7B(1)" AS INTEGER) AS n_private_funds, '
        'TRY_CAST("Total Gross Assets of Private Funds" AS DOUBLE) AS private_fund_gross_assets, '
        'TRY_CAST("5F(2)(c)" AS DOUBLE) AS raum_total '
        f"FROM read_parquet('{glob}', union_by_name=true)"
    )
    return True


def effective_filings_sql(late_days: int | None = None) -> str:
    """SQL for the filings that shape each ``(cik, period)``'s effective holdings (S20).

    One row per effective filing: ``accession_number, cik, period, filing_date,
    filing_kind``. With ``late_days`` a filing dated more than that many days
    after its period is ignored altogether, so the result is what a reader on
    ``period + late_days`` could have known: a late restatement then leaves
    the original in place instead of replacing it.
    """
    cutoff = "" if late_days is None else f" AND s.filing_date <= s.period + {int(late_days)}"
    return (
        "WITH _typed AS ("
        "SELECT s.accession_number, s.cik, s.period, s.filing_date, "
        "CASE WHEN c.amendment_type = 'NEW HOLDINGS' THEN 'addition' "
        "WHEN c.amendment_type = 'RESTATEMENT' OR coalesce(c.is_amendment, FALSE) "
        "OR s.submission_type LIKE '%/A' THEN 'restatement' ELSE 'original' END AS filing_kind "
        f"FROM {SUBMISSION_VIEW} s LEFT JOIN {COVER_VIEW} c USING (accession_number) "
        "WHERE s.submission_type LIKE '13F-HR%' AND s.period IS NOT NULL "
        f"AND s.filing_date IS NOT NULL{cutoff}), "
        "_base AS (SELECT accession_number, cik, period, filing_date, filing_kind FROM ("
        "SELECT *, row_number() OVER (PARTITION BY cik, period "
        "ORDER BY filing_date DESC, accession_number DESC) AS _rn "
        "FROM _typed WHERE filing_kind <> 'addition') WHERE _rn = 1), "
        "_additions AS (SELECT t.accession_number, t.cik, t.period, t.filing_date, t.filing_kind "
        "FROM _typed t LEFT JOIN _base b USING (cik, period) "
        "WHERE t.filing_kind = 'addition' AND (b.accession_number IS NULL OR t.filing_date >= b.filing_date)) "
        "SELECT * FROM _base UNION ALL SELECT * FROM _additions"
    )


def register_effective(con: Any) -> bool:
    """``sec_13f_filings_effective`` and ``sec_13f_holdings_effective`` over the raw views."""
    con.execute(
        f"CREATE OR REPLACE VIEW {FILINGS_EFFECTIVE_VIEW} AS "
        "SELECT e.accession_number, e.cik, e.period, e.filing_date, e.filing_kind, "
        "max(e.filing_date) OVER (PARTITION BY e.cik, e.period) AS effective_filing_date, "
        "count(*) OVER (PARTITION BY e.cik, e.period) AS n_effective_filings "
        f"FROM ({effective_filings_sql()}) e"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW {HOLDINGS_EFFECTIVE_VIEW} AS "
        "SELECT h.*, e.filing_kind, e.effective_filing_date, e.n_effective_filings "
        f"FROM {HOLDINGS_VIEW} h JOIN {FILINGS_EFFECTIVE_VIEW} e USING (accession_number)"
    )
    return True


def register_match(con: Any) -> bool:
    """``sec_13f_manager_match``: one ADV ``crd_number`` per 13F filer, by CRD, CIK or unique name.

    The name of a filer is the one on its latest cover page; ADV names come
    from every snapshot, so a renamed adviser still matches under either
    name. A normalised name shared by several CRDs (affiliates, successors)
    matches nothing: an ambiguous match is worse than none (DD-002 WP16).
    """
    con.execute(
        f"CREATE OR REPLACE VIEW {MATCH_VIEW} AS "
        "WITH filer AS ("
        "SELECT cik, arg_max(c.crd_number, s.filing_date) FILTER (WHERE c.crd_number IS NOT NULL) AS crd_number, "
        f"{name_key_sql('arg_max(c.manager_name, s.filing_date)')} AS name_key "
        f"FROM {SUBMISSION_VIEW} s JOIN {COVER_VIEW} c USING (accession_number) "
        "WHERE s.submission_type LIKE '13F-HR%' GROUP BY cik), "
        "adv_cik AS ("
        f"SELECT cik, crd_number FROM {ADV_VIEW} WHERE cik IS NOT NULL GROUP BY cik, crd_number), "
        "cik_unique AS (SELECT cik, any_value(crd_number) AS crd_number FROM adv_cik GROUP BY cik HAVING count(*) = 1), "
        "adv_names AS ("
        f"SELECT {name_key_sql('name')} AS key, crd_number FROM {ADV_VIEW} "
        f"UNION SELECT {name_key_sql('legal_name')} AS key, crd_number FROM {ADV_VIEW}), "
        "name_unique AS (SELECT key, any_value(crd_number) AS crd_number FROM adv_names "
        "WHERE key IS NOT NULL AND crd_number IS NOT NULL GROUP BY key HAVING count(*) = 1) "
        "SELECT f.cik, coalesce(f.crd_number, k.crd_number, n.crd_number) AS crd_number, "
        "CASE WHEN f.crd_number IS NOT NULL THEN 'crd' WHEN k.crd_number IS NOT NULL THEN 'cik' "
        "WHEN n.crd_number IS NOT NULL THEN 'name' END AS match_kind "
        "FROM filer f "
        "LEFT JOIN cik_unique k ON k.cik = TRY_CAST(f.cik AS BIGINT) "
        "LEFT JOIN name_unique n ON n.key = f.name_key"
    )
    return True


def register_cohort(con: Any) -> bool:
    """``sec_13f_manager_cohort``: each filing's hedge-fund status from the latest ADV snapshot before it.

    The adviser is the cover page's CRD when the filing carries one, else
    the filer's match (``sec_13f_manager_match``); ``match_kind`` says which.
    """
    con.execute(
        f"CREATE OR REPLACE VIEW {COHORT_VIEW} AS "
        "SELECT s.accession_number, s.cik, s.filing_date, s.period, c.manager_name, "
        "coalesce(c.crd_number, m.crd_number) AS crd_number, "
        "CASE WHEN c.crd_number IS NOT NULL THEN 'crd' ELSE m.match_kind END AS match_kind, "
        "a.snapshot_date AS adv_snapshot_date, a.advises_private_funds, a.any_hedge_funds, a.n_hedge_funds, "
        "a.n_private_funds, a.private_fund_gross_assets, a.raum_total "
        f"FROM {SUBMISSION_VIEW} s "
        f"LEFT JOIN {COVER_VIEW} c USING (accession_number) "
        f"LEFT JOIN {MATCH_VIEW} m ON m.cik = s.cik "
        f"ASOF LEFT JOIN {ADV_VIEW} a ON a.crd_number = coalesce(c.crd_number, m.crd_number) "
        "AND a.snapshot_date <= s.filing_date"
    )
    return True


_register_cohort = register_cohort


def _register_13f(con: Any, base: Path) -> bool:
    store = form13f.Store(base)
    if not store.archives_on_disk():
        return False

    def glob(table: str) -> str:
        return (store.table_dir(table) / "*.parquet").as_posix()

    con.execute(
        f"CREATE OR REPLACE VIEW {SUBMISSION_VIEW} AS "
        "SELECT ACCESSION_NUMBER AS accession_number, "
        f"{_DATE.format(col='FILING_DATE')} AS filing_date, "
        "SUBMISSIONTYPE AS submission_type, CIK AS cik, "
        f"{_DATE.format(col='PERIODOFREPORT')} AS period "
        f"FROM read_parquet('{glob('SUBMISSION')}', union_by_name=true)"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW {COVER_VIEW} AS "
        "SELECT ACCESSION_NUMBER AS accession_number, "
        "FILINGMANAGER_NAME AS manager_name, TRY_CAST(CRDNUMBER AS BIGINT) AS crd_number, "
        "NULLIF(SECFILENUMBER, '') AS sec_file_number, "
        "NULLIF(FORM13FFILENUMBER, '') AS form13f_file_number, REPORTTYPE AS report_type, "
        "upper(coalesce(ISAMENDMENT, '')) IN ('Y', 'TRUE') AS is_amendment, "
        "NULLIF(AMENDMENTNO, '') AS amendment_no, NULLIF(AMENDMENTTYPE, '') AS amendment_type "
        f"FROM read_parquet('{glob('COVERPAGE')}', union_by_name=true)"
    )
    units_dir = store.table_dir(form13f.UNITS_TABLE)
    if units_dir.is_dir() and any(units_dir.glob("*.parquet")):
        con.execute(
            f"CREATE OR REPLACE VIEW {UNITS_VIEW} AS "
            "SELECT accession_number, crowd_dollars, log_gap, compared, evidence, value_factor "
            f"FROM read_parquet('{glob(form13f.UNITS_TABLE)}', union_by_name=true)"
        )
    else:  # archives converted before the units table existed: the date rule alone
        con.execute(
            f"CREATE OR REPLACE VIEW {UNITS_VIEW} AS "
            f"SELECT accession_number, filing_date >= DATE '{DOLLARS_FROM}' AS crowd_dollars, "
            "NULL::DOUBLE AS log_gap, 0 AS compared, 'assumed' AS evidence, "
            f"CASE WHEN filing_date < DATE '{DOLLARS_FROM}' THEN 1000.0 ELSE 1.0 END AS value_factor "
            f"FROM {SUBMISSION_VIEW}"
        )
    con.execute(
        f"CREATE OR REPLACE VIEW {HOLDINGS_VIEW} AS "
        "SELECT i.ACCESSION_NUMBER AS accession_number, s.cik, c.manager_name, c.crd_number, "
        "s.filing_date, s.period, s.submission_type, c.is_amendment, c.amendment_type, "
        "c.report_type, i.CUSIP AS cusip, i.NAMEOFISSUER AS issuer, "
        "i.TITLEOFCLASS AS title_of_class, "
        "TRY_CAST(i.VALUE AS DOUBLE) * coalesce(u.value_factor, "
        f"CASE WHEN s.filing_date < DATE '{DOLLARS_FROM}' THEN 1000 ELSE 1 END) AS value_usd, "
        "coalesce(u.evidence, 'assumed') AS units_evidence, "
        "TRY_CAST(i.SSHPRNAMT AS DOUBLE) AS shares, i.SSHPRNAMTTYPE AS share_type, "
        "NULLIF(i.PUTCALL, '') AS put_call, i.INVESTMENTDISCRETION AS discretion "
        f"FROM read_parquet('{glob('INFOTABLE')}', union_by_name=true) i "
        f"JOIN {SUBMISSION_VIEW} s ON s.accession_number = i.ACCESSION_NUMBER "
        f"LEFT JOIN {COVER_VIEW} c ON c.accession_number = i.ACCESSION_NUMBER "
        f"LEFT JOIN {UNITS_VIEW} u ON u.accession_number = i.ACCESSION_NUMBER"
    )
    if store.state_path.exists():
        con.execute(
            f"CREATE OR REPLACE VIEW {STATE_VIEW} AS SELECT * FROM "
            f"read_csv_auto('{store.state_path.as_posix()}', all_varchar=true)"
        )
    return True


def schema_snippet() -> str:
    return "\n".join(
        [
            "SEC Form 13F views (institutional managers' quarterly long holdings):",
            f"- {HOLDINGS_VIEW}(accession_number, cik, manager_name, crd_number, filing_date, period, "
            "submission_type, is_amendment, amendment_type, report_type, cusip, issuer, title_of_class, "
            "value_usd, units_evidence, shares, share_type, put_call, discretion)",
            "  [one row per reported position; period = quarter end, filing_date = when it became",
            "  public (up to 45 days later, the point-in-time column); share_type SH = shares, PRN =",
            "  bond principal; put_call NULL = the security itself; amendments are NOT resolved;",
            "  very large, always filter by period, filing_date, cik or cusip]",
            f"- {HOLDINGS_EFFECTIVE_VIEW}(... the same columns ..., filing_kind, effective_filing_date, "
            "n_effective_filings)",
            "  [amendments resolved: per (cik, period) the latest original or RESTATEMENT is the base",
            "  and NEW HOLDINGS amendments filed on or after it are added; effective_filing_date =",
            "  the last filing that shaped the manager's holdings for the period, use it ASOF]",
            f"- {FILINGS_EFFECTIVE_VIEW}(accession_number, cik, period, filing_date, filing_kind, "
            "effective_filing_date, n_effective_filings)",
            f"- {SUBMISSION_VIEW}(accession_number, filing_date, submission_type, cik, period)",
            f"- {COVER_VIEW}(accession_number, manager_name, crd_number, sec_file_number, "
            "form13f_file_number, report_type, is_amendment, amendment_no, amendment_type)",
            f"- {UNITS_VIEW}(accession_number, crowd_dollars, log_gap, compared, evidence, value_factor)",
            "  [per filing: are its values in dollars or thousands, judged against the other filers]",
            f"- {ADV_VIEW}(snapshot_date, crd_number, sec_number, cik, name, legal_name, advises_private_funds, "
            "any_hedge_funds, n_hedge_funds, n_private_funds, private_fund_gross_assets, raum_total)",
            "  [Form ADV adviser reports, one row per adviser per monthly snapshot 2006-2023 and from",
            "  2025-12 (the SEC lists none in between); advises_private_funds (item 7B) exists in every",
            "  era, the hedge-fund split only from 2025-12]",
            f"- {COHORT_VIEW}(accession_number, cik, filing_date, period, manager_name, crd_number, match_kind, "
            "adv_snapshot_date, advises_private_funds, any_hedge_funds, n_hedge_funds, n_private_funds, "
            "private_fund_gross_assets, raum_total)",
            "  [each 13F filing joined to the latest ADV snapshot dated on or before its filing_date;",
            "  the adviser is the cover page's CRD (filings from 2023, match_kind 'crd') or the filer's",
            "  match by CIK or unique normalised name ('cik', 'name'); NULL = unmatched or ambiguous]",
            f"- {MATCH_VIEW}(cik, crd_number, match_kind)",
            "  [one row per 13F filer: the ADV adviser it maps to and how]",
        ]
    )
