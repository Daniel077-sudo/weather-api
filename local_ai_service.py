import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from utils import analyze_text_risk, build_recommended_action
from weather_service import build_alternative_location, risk_rank


RULES_PATH = Path(__file__).resolve().parent / "data" / "local_ai_rules.json"


@lru_cache(maxsize=1)
def load_local_ai_rules() -> Dict[str, Any]:
    with RULES_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def _format_template(value: str, location: str) -> str:
    return (value or "").format(location=location or "目的地")


def infer_activity_intent(text: str, fallback: str = "commuting") -> str:
    rules = load_local_ai_rules()
    text = text or ""
    for item in rules.get("activity_intents", []):
        if any(keyword in text for keyword in item.get("keywords", [])):
            return item.get("intent") or fallback
    return fallback or "commuting"


def _match_risk_rule(risk_tags: List[str], intent: str) -> Dict[str, Any]:
    rules = load_local_ai_rules()
    tag_set = set(risk_tags or [])
    for item in rules.get("risk_rules", []):
        if intent not in item.get("activity_intents", []):
            continue
        if tag_set.intersection(item.get("risk_tags", [])):
            return item
    return {}


def _match_incident_rule(incidents: List[Dict[str, Any]]) -> Dict[str, Any]:
    rules = load_local_ai_rules()
    incident_types = {item.get("incident_type") for item in incidents or []}
    for item in rules.get("incident_rules", []):
        if incident_types.intersection(item.get("incident_types", [])):
            return item
    return {}


def build_local_ai_suggestion(
    event: Dict[str, Any],
    weather_payload: Dict[str, Any] | None = None,
    risk: Dict[str, Any] | None = None,
    nearby_incidents: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    rules = load_local_ai_rules()
    weather_payload = weather_payload or {}
    risk = risk or {}
    nearby_incidents = nearby_incidents or []

    location = (
        event.get("location")
        or "".join(part for part in [event.get("city"), event.get("district")] if part)
        or "目的地"
    )
    text = " ".join(
        str(part)
        for part in [
            event.get("title") or "",
            event.get("activity") or "",
            event.get("transport_type") or "",
            location,
        ]
        if part
    )
    intent = infer_activity_intent(text, event.get("activity") or "commuting")
    text_risk = analyze_text_risk(text)
    risk_tags = sorted(set((risk.get("risk_tags") or []) + (text_risk.get("risk_tags") or [])))
    risk_level = risk.get("risk_level") or text_risk.get("risk_level") or "low"

    matched_rules: List[str] = []
    selected = _match_risk_rule(risk_tags, intent)
    if selected:
        matched_rules.append(selected.get("id", "risk_rule"))
        suggestion = {
            "intent": intent,
            "risk_summary": _format_template(selected.get("risk_summary", ""), location),
            "recommended_action": _format_template(selected.get("recommended_action", ""), location),
            "alternative_location": _format_template(selected.get("alternative_location", ""), location),
            "confidence": selected.get("confidence", 0.75),
        }
    else:
        default = rules.get("default", {})
        action = build_recommended_action(risk_level, risk_tags, location)
        suggestion = {
            "intent": intent or default.get("intent", "commuting"),
            "risk_summary": action or default.get("risk_summary", ""),
            "recommended_action": action or default.get("recommended_action", ""),
            "alternative_location": build_alternative_location(event.get("city") or "", event.get("district") or "", risk_tags),
            "confidence": default.get("confidence", 0.55),
        }

    incident_rule = _match_incident_rule(nearby_incidents)
    if incident_rule:
        matched_rules.append(incident_rule.get("id", "incident_rule"))
        suggestion["risk_summary"] = _format_template(incident_rule.get("risk_summary", suggestion["risk_summary"]), location)
        suggestion["recommended_action"] = _format_template(incident_rule.get("recommended_action", suggestion["recommended_action"]), location)
        suggestion["confidence"] = min(
            0.95,
            float(suggestion.get("confidence") or 0.55) + float(incident_rule.get("confidence_boost") or 0),
        )

    if nearby_incidents:
        links = [item.get("source_url") for item in nearby_incidents[:3] if item.get("source_url")]
        if links:
            suggestion["recommended_action"] = f"{suggestion['recommended_action']} 參考連結：{'、'.join(links)}"

    suggestion["suggestion_source"] = "local_rules"
    suggestion["matched_rules"] = matched_rules
    suggestion["risk_level"] = risk_level
    suggestion["risk_tags"] = risk_tags
    suggestion["gemini_required"] = False
    suggestion["rules_version"] = rules.get("version", "")
    suggestion["weather_source"] = weather_payload.get("source") or ""
    suggestion["confidence"] = round(float(suggestion.get("confidence") or 0.55), 2)
    if risk_rank(risk_level) >= 2 and suggestion["confidence"] < 0.75:
        suggestion["confidence"] = 0.75
    return suggestion
