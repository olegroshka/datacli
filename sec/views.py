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
DOLLARS_FROM = form13f.DOLLARS_FROM.isoformat()

_DATE = "CAST(strptime({col}, '%d-%b-%Y') AS DATE)"


def register(con: Any, *, root: Path | None = None) -> dict[str, bool]:
    base = Path(root) if root is not None else sec_root()
    result = {HOLDINGS_VIEW: _register_13f(con, base)}
    result[ADV_VIEW] = _register_adv(con, base)
    result[COHORT_VIEW] = result[HOLDINGS_VIEW] and result[ADV_VIEW] and _register_cohort(con)
    return result


def _register_adv(con: Any, base: Path) -> bool:
    """``sec_adv_advisers``: one row per adviser per monthly snapshot (typed columns)."""
    store = adv.Store(base)
    if not store.snapshots_on_disk():
        return False
    glob = (store.snapshot_dir / "*.parquet").as_posix()
    con.execute(
        f"CREATE OR REPLACE VIEW {ADV_VIEW} AS "
        "SELECT CAST(snapshot_date AS DATE) AS snapshot_date, "
        'TRY_CAST(trim("Organization CRD#") AS BIGINT) AS crd_number, "SEC#" AS sec_number, '
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


def _register_cohort(con: Any) -> bool:
    """``sec_13f_manager_cohort``: each filing's hedge-fund status from the latest ADV snapshot before it."""
    con.execute(
        f"CREATE OR REPLACE VIEW {COHORT_VIEW} AS "
        "SELECT s.accession_number, s.cik, s.filing_date, s.period, c.manager_name, c.crd_number, "
        "a.snapshot_date AS adv_snapshot_date, a.advises_private_funds, a.any_hedge_funds, a.n_hedge_funds, "
        "a.n_private_funds, a.private_fund_gross_assets, a.raum_total "
        f"FROM {SUBMISSION_VIEW} s "
        f"LEFT JOIN {COVER_VIEW} c USING (accession_number) "
        f"ASOF LEFT JOIN {ADV_VIEW} a ON a.crd_number = c.crd_number AND a.snapshot_date <= s.filing_date"
    )
    return True


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
            f"- {SUBMISSION_VIEW}(accession_number, filing_date, submission_type, cik, period)",
            f"- {COVER_VIEW}(accession_number, manager_name, crd_number, sec_file_number, "
            "form13f_file_number, report_type, is_amendment, amendment_no, amendment_type)",
            f"- {UNITS_VIEW}(accession_number, crowd_dollars, log_gap, compared, evidence, value_factor)",
            "  [per filing: are its values in dollars or thousands, judged against the other filers]",
            f"- {ADV_VIEW}(snapshot_date, crd_number, sec_number, name, legal_name, advises_private_funds, "
            "any_hedge_funds, n_hedge_funds, n_private_funds, private_fund_gross_assets, raum_total)",
            "  [Form ADV adviser reports, one row per adviser per monthly snapshot 2006-2023 and from",
            "  2025-12 (the SEC lists none in between); advises_private_funds (item 7B) exists in every",
            "  era, the hedge-fund split only from 2025-12]",
            f"- {COHORT_VIEW}(accession_number, cik, filing_date, period, manager_name, crd_number, "
            "adv_snapshot_date, advises_private_funds, any_hedge_funds, n_hedge_funds, n_private_funds, "
            "private_fund_gross_assets, raum_total)",
            "  [each 13F filing joined to the latest ADV snapshot dated on or before its filing_date",
            "  by CRD number; the 13F cover page carries a CRD only for filings from 2023, so earlier",
            "  filings have NULLs here]",
        ]
    )
