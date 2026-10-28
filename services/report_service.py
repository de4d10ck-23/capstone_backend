"""Versioned operational summaries from recorded data, with escaped HTML and real CSV exports."""
import csv
import hashlib
import html
import io
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

MANILA = timezone(timedelta(hours=8))
STATUS = {"undrinkable": "contaminated", "not_tested": "unknown", "pending": "unknown"}
WORKFLOW = {"done": "completed", "resolved": "completed", "dismissed": "cancelled", "rejected": "cancelled",
            "processing": "in_progress", "passed_to_chu": "escalated"}


def local_day(value):
    if not value:
        return None
    text = str(value)
    try:
        if "T" in text or " " in text:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.astimezone(MANILA).date().isoformat()
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def report_options(body, user, now=None):
    today = (now or datetime.now(timezone.utc)).astimezone(MANILA).date()
    end = body.period_end or today
    start = body.period_start or (end - timedelta(days=29))
    if start > end or end > today or (end - start).days > 365:
        raise ValueError("Choose a reporting period of up to 366 days, ending today or earlier.")
    scope = (body.barangay or "").strip()
    if user.get("role") == "barangay_official":
        scope = (user.get("barangay") or "").strip()
        if not scope:
            raise ValueError("Your account needs an assigned barangay before generating reports.")
    elif scope.lower() in ("all", "all barangays", "city-wide", ""):
        scope = None
    return {"title": body.title.strip() or f"Water Surveillance Report - {scope or 'Maasin City'}",
            "barangay": scope, "period_start": start.isoformat(), "period_end": end.isoformat(),
            "include_forecasts": body.include_forecasts, "notes": body.notes.strip()}


def read_all(query):
    rows = []
    while True:
        page = query.range(len(rows), len(rows) + 499).execute().data or []
        rows.extend(page)
        if len(page) < 500:
            return rows


def fetch_snapshot(options, user):
    from core.database import get_supabase
    sb = get_supabase()
    # Never turn a failed query into an apparent zero-incident report.
    def scoped(table):
        query = sb.table(table).select("*").order("id")
        if options["barangay"]:
            query = query.ilike("barangay", options["barangay"])
        return read_all(query)
    sources = scoped("water_locations")
    logs = []
    ids = [row["id"] for row in sources]
    for offset in range(0, len(ids), 100):
        logs.extend(read_all(sb.table("water_status_logs").select("*")
            .in_("location_id", ids[offset:offset + 100]).lte("effective_date", options["period_end"]).order("id")))
    concerns = scoped("resident_reports")
    inspections = scoped("inspection_requests")
    now = datetime.now(timezone.utc)
    forecasts = []
    if options["include_forecasts"]:
        today = now.astimezone(MANILA).date()
        rows = read_all(sb.table("water_forecasts").select("*")
            .gte("forecast_date", today.isoformat()).lte("forecast_date", (today + timedelta(days=7)).isoformat()).order("station_id").order("forecast_date"))
        allowed = {str(row["id"]) for row in sources if not row.get("archived_at")}
        forecasts = [row for row in rows if str(row["station_id"]) in allowed]
    return compile_snapshot(sources, logs, concerns, inspections, forecasts, options, user, now)


def compile_snapshot(sources, logs, concerns, inspections, forecasts, options, user, now):
    start, end = options["period_start"], options["period_end"]
    scope = options["barangay"]
    def in_scope(row):
        return not scope or (row.get("barangay") or "").strip().lower() == scope.strip().lower()
    def in_period(value):
        day = local_day(value)
        return bool(day and start <= day <= end)
    sources = [row for row in sources if in_scope(row)
        and (not local_day(row.get("created_at")) or local_day(row["created_at"]) <= end)
        and (not local_day(row.get("archived_at")) or local_day(row["archived_at"]) > end)]
    source_ids = {str(row["id"]) for row in sources}
    daily = {}
    for log in sorted(logs, key=lambda row: (str(row.get("effective_date") or ""), str(row.get("recorded_at") or ""), str(row.get("id") or ""))):
        day = local_day(log.get("effective_date"))
        if str(log.get("location_id")) in source_ids and day and day <= end:
            daily[(str(log["location_id"]), day)] = log
    latest = {}
    for (source_id, day), log in daily.items():
        latest[source_id] = log
    inventory, missing_history = [], 0
    for source in sources:
        log = latest.get(str(source["id"]))
        if log is None:
            missing_history += 1
        status = STATUS.get((log or {}).get("status"), (log or {}).get("status", "unknown"))
        if status not in ("safe", "warning", "contaminated"):
            status = "unknown"
        item = {"id": str(source["id"]), "name": source.get("full_name") or source.get("name") or "Unnamed source",
                "barangay": source.get("barangay") or "Not recorded", "source_type": source.get("source_type") or source.get("type") or "Not recorded",
                "status": status, "effective_date": (log or {}).get("effective_date"),
                "sample_date": (log or {}).get("sample_date"), "sample_time": (log or {}).get("sample_time"),
                "e_coli": (log or {}).get("e_coli"), "notes": (log or {}).get("notes") or "",
                "latitude": source.get("latitude"), "longitude": source.get("longitude"),
                "registered_at": source.get("created_at"), "added_in_period": in_period(source.get("created_at"))}
        inventory.append(item)
    inventory.sort(key=lambda item: (item["barangay"], item["name"], item["id"]))
    names = {item["id"]: item["name"] for item in inventory}
    changes = [{"source_id": sid, "source": names[sid], "effective_date": day,
                "status": STATUS.get(log.get("status"), log.get("status") or "unknown"),
                "sample_date": log.get("sample_date"), "sample_time": log.get("sample_time"),
                "e_coli": log.get("e_coli"), "notes": log.get("notes") or ""}
               for (sid, day), log in daily.items() if start <= day <= end]
    # One source/sample timestamp is one recorded check, even if its notes were edited later.
    samples = {(sid, log.get("sample_date"), log.get("sample_time")) for (sid, _), log in daily.items()
               if in_period(log.get("sample_date")) and log.get("bacteriological_exam") in ("passed", "failed")}
    def clean_description(text):
        import re
        return re.sub(r"\[(?:Attached )?Photo Proof:[^\]]*\]", "", text or "", flags=re.IGNORECASE).strip()
    concern_rows = [{"id": str(row["id"]), "title": row.get("title") or "Concern",
                     "category": row.get("type") or "concern", "barangay": row.get("barangay"),
                     "submitted_at": row.get("created_at"), "status": WORKFLOW.get(row.get("status"), row.get("status") or "pending"),
                     "description": clean_description(row.get("description")), "resolution": row.get("reason") or ""}
                    for row in concerns if in_scope(row) and in_period(row.get("created_at"))]
    ticket_rows = [{"id": str(row["id"]), "source_id": str(row.get("location_id") or ""),
                    "source": names.get(str(row.get("location_id")), "No linked registry source"),
                    "barangay": row.get("barangay"), "created_at": row.get("created_at"),
                    "status": WORKFLOW.get(row.get("status"), row.get("status") or "pending"),
                    "notes": clean_description(row.get("notes"))}
                   for row in inspections if in_scope(row) and local_day(row.get("created_at")) and local_day(row["created_at"]) <= end]
    follow_up = []
    for item in inventory:
        if item["status"] in ("contaminated", "warning", "unknown"):
            follow_up.append({"reference": item["id"], "item": item["name"], "reason": f"Recorded status: {item['status']}",
                              "action": "Review source records and arrange appropriate verification or follow-up."})
        if not item["sample_date"]:
            follow_up.append({"reference": item["id"], "item": item["name"], "reason": "No recorded sample date",
                              "action": "Complete or verify the source's test records."})
        if item["latitude"] is None or item["longitude"] is None:
            follow_up.append({"reference": item["id"], "item": item["name"], "reason": "Missing map coordinates", "action": "Verify and record the source location."})
    for item in concern_rows:
        if item["status"] in ("pending", "validated"):
            follow_up.append({"reference": item["id"], "item": item["title"], "reason": "Concern awaiting review / forwarding",
                              "action": "Review the concern and document its disposition or CHU referral."})
    for item in ticket_rows:
        if item["status"] not in ("completed", "cancelled"):
            follow_up.append({"reference": item["id"], "item": item["source"], "reason": f"Open inspection: {item['status']}",
                              "action": "Review responsibility, arrange the next processing step, and record completion or dismissal."})
    by_barangay = defaultdict(Counter)
    for item in inventory:
        by_barangay[item["barangay"]][item["status"]] += 1
        by_barangay[item["barangay"]]["total"] += 1
    outlook = []
    if options["include_forecasts"]:
        today = now.astimezone(MANILA).date()
        for offset in range(8):
            day = (today + timedelta(days=offset)).isoformat()
            rows = [row for row in forecasts if str(row.get("forecast_date")) == day and str(row.get("station_id")) in source_ids]
            counts = Counter(row.get("prediction", {}).get("predicted_status", "unknown") for row in rows)
            outlook.append({"date": day, "low": counts["safe"], "moderate": counts["warning"],
                            "high": counts["undrinkable"] + counts["contaminated"],
                            "unavailable": len(source_ids) - sum(counts[key] for key in ("safe", "warning", "undrinkable", "contaminated")),
                            "oldest_generated_at": min((row["generated_at"] for row in rows), default=None)})
    return {"schema_version": 1, **options, "generated_at": now.isoformat(),
        "prepared_by": user.get("full_name") or user.get("username") or "Staff member", "prepared_by_role": user.get("role"),
        "summary": {"sources": len(inventory), "source_status": dict(Counter(item["status"] for item in inventory)),
            "new_sources": sum(item["added_in_period"] for item in inventory), "recorded_checks": len(samples),
            "sources_with_checks": len({sid for sid, _, _ in samples}), "change_days": len(changes),
            "concerns_submitted": len(concern_rows), "concern_status": dict(Counter(item["status"] for item in concern_rows)),
            "concern_categories": dict(Counter(item["category"] for item in concern_rows)),
            "inspections_created": sum(in_period(item["created_at"]) for item in ticket_rows),
            "inspection_status": dict(Counter(item["status"] for item in ticket_rows)),
            "open_inspections": sum(item["status"] not in ("completed", "cancelled") for item in ticket_rows),
            "missing_history": missing_history},
        "barangays": [{"barangay": key, **dict(value)} for key, value in sorted(by_barangay.items())],
        "sources": inventory, "changes": changes, "concerns": concern_rows, "inspections": ticket_rows,
        "follow_up": follow_up, "forecasts": outlook,
        "methodology": [
            "Dates and reporting boundaries use Asia/Manila. Source status uses the latest saved effective-day snapshot on or before the period end; same-day revisions use the latest saved revision.",
            "Inventory includes existing registry sources registered by the period end. Sources deleted before generation cannot be reconstructed. Names, barangays, and coordinates are current registry metadata.",
            "Sources without a historical snapshot are counted as unknown. Recorded checks deduplicate source/sample date/time and require a recorded passed/failed examination; these are system records, not independently verified laboratory certificates.",
            "Concerns were submitted during the selected period. Inspection workload includes tickets created by the period end, including earlier backlog. Workflow statuses are their current values at generation, not reconstructed historical completion dates.",
            "Missing or incomplete records are identified explicitly. No chlorine, turbidity, or chemical measurements are invented, and this report does not determine regulatory compliance or certify potability.",
            "Optional model outlook describes saved predictions at generation time, independent of the reporting period. It is separate from recorded source status and laboratory findings.",
        ]}


def snapshot_hash(snapshot):
    # JSONB / PostgREST may round-trip 10.0 as 10; both represent the same snapshot value.
    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    return hashlib.sha256(json.dumps(canonical(snapshot), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def tables(snapshot):
    s = snapshot["summary"]
    metrics = [("Registered sources at period end", s["sources"]), ("Recorded safe", s["source_status"].get("safe", 0)),
        ("Recorded warning", s["source_status"].get("warning", 0)), ("Recorded contaminated", s["source_status"].get("contaminated", 0)),
        ("Unknown / no history", s["source_status"].get("unknown", 0)), ("Sources added in period", s["new_sources"]),
        ("Recorded checks in period", s["recorded_checks"]), ("Sources with recorded checks", s["sources_with_checks"]),
        ("Change days in period", s["change_days"]), ("Concerns submitted in period", s["concerns_submitted"]),
        ("Inspection tickets created in period", s["inspections_created"]), ("Open inspection backlog at generation", s["open_inspections"])]
    sections = [
        ("Executive summary", ["Measure", "Count"], metrics),
        ("Barangay source status at period end", ["Barangay", "Total", "Safe", "Warning", "Contaminated", "Unknown"],
         [[r["barangay"], r["total"], r.get("safe", 0), r.get("warning", 0), r.get("contaminated", 0), r.get("unknown", 0)] for r in snapshot["barangays"]]),
        ("Concern categories submitted in period", ["Category", "Count"], sorted(s.get("concern_categories", {}).items())),
        ("Workflow status at generation", ["Status", "Concerns submitted in period", "Inspection workload"],
         [[status, s["concern_status"].get(status, 0), s["inspection_status"].get(status, 0)]
          for status in sorted(set(s["concern_status"]) | set(s["inspection_status"]))]),
        ("Follow-up register", ["Reference", "Source", "Reason", "Suggested follow-up"], [[r[k] for k in ("reference", "item", "reason", "action")] for r in snapshot["follow_up"]]),
        ("Source inventory and recorded status", ["Source ID", "Source", "Barangay", "Type", "Status", "Effective day", "Sample date", "Sample time", "E. coli", "Notes", "Latitude", "Longitude"],
         [[r[k] for k in ("id", "name", "barangay", "source_type", "status", "effective_date", "sample_date", "sample_time", "e_coli", "notes", "latitude", "longitude")] for r in snapshot["sources"]]),
        ("New sources registered in period", ["Source ID", "Source", "Barangay", "Registered at"], [[r[k] for k in ("id", "name", "barangay", "registered_at")] for r in snapshot["sources"] if r["added_in_period"]]),
        ("Recorded status updates in period", ["Source ID", "Source", "Effective day", "Status", "Sample date", "Sample time", "E. coli", "Notes"], [[r[k] for k in ("source_id", "source", "effective_date", "status", "sample_date", "sample_time", "e_coli", "notes")] for r in snapshot["changes"]]),
        ("Concerns submitted in period - status at generation", ["Reference", "Title", "Category", "Barangay", "Submitted at", "Status", "Description", "Resolution"], [[r[k] for k in ("id", "title", "category", "barangay", "submitted_at", "status", "description", "resolution")] for r in snapshot["concerns"]]),
        ("Inspection workload - status at generation", ["Ticket", "Source", "Barangay", "Created at", "Status", "Notes"], [[r[k] for k in ("id", "source", "barangay", "created_at", "status", "notes")] for r in snapshot["inspections"]]),
    ]
    if snapshot["include_forecasts"]:
        sections.append(("Model outlook at generation - estimates, separate from recorded tests", ["Forecast date", "Low risk", "Moderate risk", "High risk", "Unavailable", "Oldest forecast generated at"],
                         [[r[k] for k in ("date", "low", "moderate", "high", "unavailable", "oldest_generated_at")] for r in snapshot["forecasts"]]))
    return sections


def display(value):
    if value is None or value == "":
        return "Not recorded"
    if isinstance(value, bool):
        return "Present" if value else "Not present"
    return str(value)


def review_rows(report):
    labels = {"ready": "Ready to send", "submitted": "Received by CHU - awaiting review", "in_review": "Under review", "done": "Done - doctor review completed"}
    return [("Review status", labels.get(report.get("review_status"), "Ready to send")),
        ("Sent to CHU", report.get("submitted_at")), ("Sent by", report.get("submitted_by_name")),
        ("Review started", report.get("review_started_at")), ("Reviewing doctor", report.get("reviewer_name")),
        ("Review completed", report.get("reviewed_at")), ("Completed by", report.get("reviewed_by_name")),
        ("Doctor review notes", report.get("review_notes"))]


def render_html(snapshot, report_id="Preview", digest=None, review=None):
    esc = lambda value: html.escape(display(value), quote=True)
    parts = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>WaterWatch Surveillance Report</title><style>body{font:12px/1.55 Arial,sans-serif;color:#17263e;max-width:1200px;margin:32px auto;padding:0 20px}header{border-bottom:3px solid #153f75;padding-bottom:20px}h1{font-size:25px;margin:8px 0}h2{font-size:17px;margin:28px 0 10px}p{white-space:pre-wrap}table{width:100%;border-collapse:collapse;font-size:10px;table-layout:auto}th,td{padding:7px;border:1px solid #d7e0ea;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#edf3f9}tr:nth-child(even){background:#f8fafc}thead{display:table-header-group}small{color:#53647b}.notes{padding:12px;background:#f0f5fb}footer{margin-top:30px;border-top:1px solid #ccd7e4;padding-top:10px;overflow-wrap:anywhere}@media print{@page{size:A4 landscape;margin:12mm}body{margin:0;padding:0;font-size:10px}h2{break-after:avoid}tr{break-inside:avoid}table{font-size:8px}header{margin-bottom:16px}}</style></head><body><header><strong>WaterWatch · Maasin City · City Health Unit</strong>",
        f"<h1>{esc(snapshot['title'])}</h1><p>Scope: {esc(snapshot['barangay'] or 'All barangays')}<br>Reporting period: {esc(snapshot['period_start'])} to {esc(snapshot['period_end'])} (Philippine time)<br>Generated: {esc(snapshot['generated_at'])}<br>Prepared by: {esc(snapshot['prepared_by'])} · {esc(snapshot['prepared_by_role'])}<br>Report reference: {esc(report_id)}</p></header>"]
    if snapshot["notes"]:
        parts.append(f"<h2>Inspector commentary</h2><p class='notes'>{esc(snapshot['notes'])}</p>")
    for title, headers, rows in tables(snapshot):
        parts.append(f"<h2>{esc(title)}</h2>")
        if not rows:
            parts.append("<p>No matching records.</p>")
            continue
        parts.append("<table><thead><tr>" + "".join(f"<th>{esc(value)}</th>" for value in headers) + "</tr></thead><tbody>")
        parts.extend("<tr>" + "".join(f"<td>{esc(value)}</td>" for value in row) + "</tr>" for row in rows)
        parts.append("</tbody></table>")
    if review:
        parts.append("<h2>CHU doctor review record</h2><p>Review progress is recorded separately from the archived data snapshot. Done records completion of document review.</p><table><tbody>")
        parts.extend(f"<tr><th>{esc(label)}</th><td>{esc(value)}</td></tr>" for label, value in review_rows(review))
        parts.append("</tbody></table>")
    parts.append("<h2>Methods and data limitations</h2><ul>" + "".join(f"<li>{esc(item)}</li>" for item in snapshot["methodology"]) + "</ul>")
    parts.append(f"<footer>Archived operational summary · Schema version {snapshot['schema_version']}<br>Data snapshot SHA-256: {esc(digest or snapshot_hash(snapshot))}<br>Prepared by: ____________________ &nbsp; Reviewed by: ____________________ &nbsp; Date: __________</footer></body></html>")
    return "".join(parts)


def render_csv(snapshot, report_id, digest, review=None):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    def row(values):
        cells = []
        for value in values:
            text = display(value)
            # Prevent user-entered text from becoming a spreadsheet formula.
            if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
                text = "'" + text
            cells.append(text)
        writer.writerow(cells)
    for values in [("WaterWatch report", snapshot["title"]), ("Reference", report_id), ("Scope", snapshot["barangay"] or "All barangays"),
                   ("Period start", snapshot["period_start"]), ("Period end", snapshot["period_end"]), ("Generated at", snapshot["generated_at"]),
                   ("Prepared by", snapshot["prepared_by"]), ("SHA-256", digest), ("Inspector commentary", snapshot["notes"])]:
        row(values)
    for title, headers, rows in tables(snapshot):
        writer.writerow([])
        row([title])
        row(headers)
        for values in rows:
            row(values)
        if not rows:
            row(["No matching records"])
    writer.writerow([])
    row(["Methods and data limitations"])
    for item in snapshot["methodology"]:
        row([item])
    if review:
        writer.writerow([])
        row(["CHU doctor review record - separate from archived data snapshot"])
        for values in review_rows(review):
            row(values)
    return "\ufeff" + output.getvalue()
