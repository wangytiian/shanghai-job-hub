"""Isolated candidate extraction for disabled campus B-tier sources."""

from dataclasses import dataclass

from app.services.source_candidate_policy import candidate_from_detail, evaluate_candidate
from app.services.source_request_budget import RequestBudgetController, is_transport_timeout
from app.sources.campus_json import CampusJobDetail, CampusJsonSource, fetch_campus_announcements, fetch_campus_details


@dataclass(frozen=True)
class CampusTrialFailure:
    announcement_id: str
    source_url: str
    error_code: str
    message: str


@dataclass(frozen=True)
class CampusTrialReport:
    source_name: str
    announcement_count: int
    detail_success_count: int
    detail_failure_count: int
    non_shanghai_count: int
    missing_application_count: int
    candidates: tuple[CampusJobDetail, ...]
    details: tuple[CampusJobDetail, ...] = ()
    failures: tuple[CampusTrialFailure, ...] = ()

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)


class CampusTrialTerminalError(Exception):
    def __init__(self, report: CampusTrialReport, cause: Exception):
        super().__init__(str(cause))
        self.report = report
        self.cause = cause


def run_campus_trial(
    client,
    source: CampusJsonSource,
    *,
    pages: int = 1,
    list_limit: int = 10,
    detail_limit: int = 3,
    request_timeout_seconds: int = 12,
    total_seconds: int = 600,
    request_interval_seconds: float = 1.0,
) -> CampusTrialReport:
    """Extract review candidates without creating Jobs or changing source permissions."""
    if not 1 <= detail_limit <= 30:
        raise ValueError("详情试采数量必须在1到30条之间")
    if not 1 <= list_limit <= 30:
        raise ValueError("列表试采数量必须在1到30条之间")
    controller = RequestBudgetController(
        total_seconds=total_seconds,
        request_timeout_seconds=request_timeout_seconds,
        interval_seconds=request_interval_seconds,
    )
    announcements = fetch_campus_announcements(
        client,
        source,
        pages=pages,
        timeout=float(request_timeout_seconds),
        request_interval=request_interval_seconds,
        controller=controller,
    )[:list_limit]
    candidates: list[CampusJobDetail] = []
    parsed_details: list[CampusJobDetail] = []
    failures: list[CampusTrialFailure] = []
    detail_success_count = detail_failure_count = non_shanghai_count = missing_application_count = 0
    for announcement in announcements[: min(detail_limit, list_limit)]:
        try:
            details = fetch_campus_details(
                client,
                source,
                announcement,
                timeout=float(request_timeout_seconds),
                controller=controller,
            )
            if not details:
                raise ValueError("详情未返回岗位")
            detail_success_count += 1
            parsed_details.extend(details)
            for detail in details:
                decision = evaluate_candidate(
                    candidate_from_detail(detail, source_listing_url=source.base_url)
                )
                is_non_shanghai = "LOCATION_NOT_SHANGHAI" in decision.reason_codes
                if is_non_shanghai:
                    non_shanghai_count += 1
                if not is_non_shanghai and any(
                    code.startswith("APPLICATION_") for code in decision.reason_codes
                ):
                    missing_application_count += 1
                if decision.verdict == "qualified":
                    candidates.append(detail)
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            terminal = (
                status_code in {401, 403, 429, 483}
                or (status_code is not None and status_code >= 500)
                or is_transport_timeout(exc)
                or "timeout" in str(exc).lower()
            )
            if terminal:
                detail_failure_count += 1
                failures.append(
                    CampusTrialFailure(
                        announcement.parent_id,
                        announcement.detail_url,
                        "TERMINAL_ERROR",
                        str(exc)[:500],
                    )
                )
                report = CampusTrialReport(
                    source.name,
                    len(announcements),
                    detail_success_count,
                    detail_failure_count,
                    non_shanghai_count,
                    missing_application_count,
                    tuple(candidates),
                    tuple(parsed_details),
                    tuple(failures),
                )
                raise CampusTrialTerminalError(report, exc) from exc
            detail_failure_count += 1
            failures.append(
                CampusTrialFailure(
                    announcement_id=announcement.parent_id,
                    source_url=announcement.detail_url,
                    error_code="DETAIL_FETCH_FAILED",
                    message=str(exc)[:500],
                )
            )
    return CampusTrialReport(
        source_name=source.name,
        announcement_count=len(announcements),
        detail_success_count=detail_success_count,
        detail_failure_count=detail_failure_count,
        non_shanghai_count=non_shanghai_count,
        missing_application_count=missing_application_count,
        candidates=tuple(candidates),
        details=tuple(parsed_details),
        failures=tuple(failures),
    )
