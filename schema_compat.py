from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

_ASCII_KEY_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_\- ]{1,60}:\s*", re.IGNORECASE)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return items
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        if isinstance(value, dict):
            items.append(value)
    return items


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "；".join(part for part in (text(item) for item in value) if part)
    if isinstance(value, dict):
        return "；".join(
            f"{str(key).replace('_', ' ')}：{text(item)}"
            for key, item in value.items()
            if text(item)
        )
    return str(value).strip()


def first_text(data: Any, *keys: str) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = text(data.get(key))
        if value:
            return value
    return ""


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        return [part.strip() for part in stripped.splitlines() if part.strip()]
    return [value]


def strip_kind_prefix(value: Any) -> str:
    return _ASCII_KEY_PREFIX_RE.sub("", text(value), count=1)


def embedded_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return None
    source = value.strip()
    starts = [index for index in (source.find("{"), source.find("[")) if index >= 0]
    if starts:
        source = source[min(starts) :]
    try:
        parsed = ast.literal_eval(source)
    except (ValueError, SyntaxError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def keyed(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return None
    if ":" in value:
        prefix, rest = value.split(":", 1)
        parsed = embedded_dict(rest)
        if parsed is not None:
            parsed.setdefault("_key", prefix.strip())
            return parsed
    parsed = embedded_dict(value)
    return dict(parsed) if parsed is not None else None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = re.sub(r"\s+", "", value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _split_trigger(text_value: str) -> tuple[str, str]:
    body = strip_kind_prefix(text_value)
    for separator in ("：", ":"):
        if separator in body:
            trigger, manifestation = body.split(separator, 1)
            if trigger.strip():
                return trigger.strip(" 。；;，,"), manifestation.strip(" 。；;，,")
    return body.strip(" 。；;，,"), ""


def normalize_profile(raw: Any) -> dict[str, Any]:
    profile = raw.get("identity_profile", raw) if isinstance(raw, dict) else {}
    if not isinstance(profile, dict):
        profile = {}
    result = dict(profile)
    result["full_name"] = first_text(profile, "full_name", "name", "display_name")
    result["english_name"] = first_text(
        profile, "english_name", "foreign_name", "name_en", "english"
    )
    codename = first_text(profile, "nxx_codename", "codename")
    if not codename:
        for item in as_list(profile.get("covert_identity") or profile.get("covert_identities")):
            match = re.search(r"代号\s*[:：]?\s*([^）)；;，,]+)", text(item))
            if match:
                codename = match.group(1).strip()
                break
    result["nxx_codename"] = codename

    occupations = as_list(profile.get("occupations") or profile.get("occupation"))
    if not occupations:
        occupations = as_list(profile.get("public_identity")) + as_list(
            profile.get("covert_identity")
        )
    cleaned_occupations = _dedupe([text(item) for item in occupations if text(item)])
    result["occupations"] = cleaned_occupations
    occupation_text = "；".join(cleaned_occupations)
    if occupation_text:
        result["occupation"] = occupation_text
    gender = first_text(profile, "gender", "sex")
    if gender:
        result["gender"] = gender

    result["residence_and_jurisdiction"] = first_text(
        profile,
        "residence_and_jurisdiction",
        "residence",
        "residence_city",
        "jurisdiction",
    )
    result["educational_background"] = first_text(
        profile, "educational_background", "academic_background", "education"
    )
    result["medical_condition"] = first_text(
        profile,
        "medical_condition",
        "somatic_health_status",
        "health_status",
        "physical_condition",
    )
    result["specialized_skills"] = first_text(
        profile, "specialized_skills", "skills", "abilities"
    )
    result["companion_animal"] = first_text(
        profile, "companion_animal", "pet", "pet_animal"
    )

    if "core_relationships" in profile:
        result["core_relationships"] = normalize_core_relationships(
            profile.get("core_relationships")
        )
    return result


def normalize_core_relationships(raw: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in as_list(raw):
        data = keyed(item)
        if data is None:
            continue
        target = first_text(data, "target", "name", "person", "_key")
        relation = first_text(
            data, "relation", "relationship_nature", "relation_type", "description"
        )
        if target and relation:
            result.append({"target": target, "relation": relation})
    return result


def normalize_timeline(raw: Any) -> list[dict[str, str]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("timeline_events") or raw.get("timeline") or raw.get("events") or []
    result: list[dict[str, str]] = []
    for item in as_list(items):
        data = keyed(item)
        if data is None:
            description = text(item)
            if description:
                result.append({"life_stage": "", "description": description})
            continue
        stage = first_text(
            data, "life_stage", "stage", "period", "phase", "time", "age_range"
        )
        description = first_text(
            data, "description", "event", "summary", "content", "detail"
        )
        if description:
            result.append({"life_stage": stage, "description": description})
    return result


_BODY_LABELS = {
    "facial_features": "面部",
    "hair_style": "发型",
    "eye_color": "瞳色",
    "physique_and_athleticism": "体态",
    "height_cm": "身高",
    "blood_type": "血型",
    "body_type": "体型",
    "build": "体格",
    "crisis_manifestations": "发作表现",
    "behavioral_implications": "行为影响",
}

_NOISE_KEYS = (
    "evidence",
    "source",
    "provenance",
    "citation",
    "location",
    "hash",
)


def _body_metrics(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    parts = []
    for key, item in value.items():
        lowered = str(key).lower()
        if any(noise in lowered for noise in _NOISE_KEYS):
            continue
        rendered = text(item)
        if not rendered:
            continue
        label = _BODY_LABELS.get(lowered, str(key).replace("_", " "))
        parts.append(f"{label}：{rendered}")
    return parts


def normalize_body(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    if isinstance(data.get("embodied_identity"), dict):
        data = data["embodied_identity"]
    result = dict(data)

    parts: list[str] = []
    for key in ("body_features", "physical_description", "appearance"):
        value = first_text(data, key)
        if value:
            parts.append(value)
    for key in ("appearance_traits", "body_metrics"):
        parts.extend(_body_metrics(data.get(key)))
    for key in ("body_conditioning", "physical_capacities"):
        value = first_text(data, key)
        if value:
            parts.append(value)
    height = first_text(data, "height")
    blood_type = first_text(data, "blood_type")
    metrics = data.get("body_metrics")
    metric_keys = (
        {str(key).lower() for key in metrics} if isinstance(metrics, dict) else set()
    )
    if height and "height_cm" not in metric_keys and "height" not in metric_keys:
        parts.append(f"身高 {height}")
    if blood_type and "blood_type" not in metric_keys:
        parts.append(f"血型 {blood_type}")
    result["body_features"] = "；".join(_dedupe(parts))

    conditions: list[str] = []
    for key in (
        "physical_conditions",
        "somatic_pathology",
        "pathological_conditions",
        "conditions",
    ):
        for item in as_list(data.get(key)):
            value = text(item)
            if value:
                conditions.append(value)
    limitations = data.get("symptomatic_limitations")
    if isinstance(limitations, dict):
        for key, value in limitations.items():
            rendered = text(value)
            if rendered:
                conditions.append(f"{key.replace('_', ' ')}：{rendered}")
    elif limitations:
        conditions.extend(text(item) for item in as_list(limitations) if text(item))
    result["physical_conditions"] = _dedupe(conditions)
    result["body_self_image"] = first_text(data, "body_self_image", "body_confidence")
    for key in (
        "appearance_traits",
        "body_metrics",
        "symptomatic_limitations",
        "physiological_affective_responses",
    ):
        value = result.get(key)
        if isinstance(value, dict):
            result[key] = {
                name: item
                for name, item in value.items()
                if text(item)
                and not any(
                    noise in str(name).lower() for noise in _NOISE_KEYS
                )
            }
    return result


def normalize_triggers(raw: Any) -> list[dict[str, str]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("emotional_triggers") or raw.get("triggers") or []
    result: list[dict[str, str]] = []
    for item in as_list(items):
        data = keyed(item)
        if data is not None:
            trigger = first_text(
                data, "trigger", "stimulus", "situation", "scene", "_key"
            )
            manifestation = first_text(
                data,
                "behavioral_manifestation",
                "manifestation",
                "response",
                "behavior",
                "description",
            )
        else:
            trigger, manifestation = _split_trigger(text(item))
        if trigger:
            result.append(
                {"trigger": trigger, "behavioral_manifestation": manifestation}
            )
    return result


def normalize_dialogue(raw: Any) -> list[dict[str, str]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("dialogue_examples") or raw.get("examples") or []
    result: list[dict[str, str]] = []
    for item in as_list(items):
        data = keyed(item)
        if data is not None:
            context = first_text(data, "context", "scene", "situation")
            tone = first_text(
                data, "emotional_tone", "emotion", "intent", "tone", "pattern"
            )
            quotes = as_list(
                data.get("quotes")
                or data.get("lines")
                or data.get("utterances")
                or data.get("examples")
            )
            if quotes:
                values = [text(quote) for quote in quotes if text(quote)]
                if values:
                    result.append(
                        {
                            "quote": "」「".join(values),
                            "context": context,
                            "emotional_tone": tone,
                        }
                    )
                continue
            quote = first_text(data, "quote", "text", "line", "utterance")
            if quote:
                result.append(
                    {
                        "quote": quote,
                        "context": context,
                        "emotional_tone": tone,
                    }
                )
            continue
        value = text(item)
        if value:
            context = ""
            quote = value
            for separator in ("：", ":"):
                if separator in value:
                    head, tail = value.split(separator, 1)
                    if head.strip() and tail.strip():
                        context, quote = head.strip(), tail.strip()
                    break
            result.append(
                {"quote": quote, "context": context, "emotional_tone": ""}
            )
    return result


def normalize_style(raw: Any) -> dict[str, Any]:
    style = dict(raw) if isinstance(raw, dict) else {}
    if isinstance(style.get("expression_style"), dict):
        style.update(style["expression_style"])

    notes = first_text(style, "notes", "description")
    summary = first_text(style, "summary", "behavioral_summary")
    style["notes"] = notes or summary
    style["summary"] = summary
    style["behavioral_notes"] = first_text(
        style, "behavioral_notes", "behavioral_implications"
    )

    speech = style.get("speech_habits")
    speech = speech if isinstance(speech, dict) else {}

    addressing = style.get("addressing_habits")
    if not isinstance(addressing, dict):
        address_style = first_text(speech, "address_style") or first_text(
            style, "address_style"
        )
        addressing = {"称呼": address_style} if address_style else {}
    style["addressing_habits"] = addressing

    catchphrases = style.get("catchphrases_and_particles")
    if not isinstance(catchphrases, dict):
        catchphrases = {}
        tics = speech.get("verbal_tics_and_frequencies")
        if isinstance(tics, dict):
            catchphrases.update(
                {key: text(value) for key, value in tics.items() if text(value)}
            )
        elif tics:
            catchphrases["口头禅"] = text(tics)
        for key in ("catchphrases", "verbal_tics"):
            for item in as_list(style.get(key)):
                value = text(item)
                if value:
                    catchphrases[value[:24]] = value
    style["catchphrases_and_particles"] = catchphrases

    patterns = style.get("communication_patterns")
    if not isinstance(patterns, dict):
        patterns = {"情境反应": text(patterns)} if text(patterns) else {}
    style["communication_patterns"] = patterns

    vocal = style.get("vocal_characteristics")
    style["vocal_characteristics"] = vocal if isinstance(vocal, dict) else {}
    return style


def normalize_relationships(raw: Any) -> list[dict[str, str]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("relationships") or []
    entries: list[dict[str, Any]] = []
    for item in as_list(items):
        data = keyed(item)
        if data is not None:
            entries.append(data)

    result: list[dict[str, str]] = []
    by_target: dict[str, int] = {}

    def add(target: str, description: str, relation_type: str = "") -> None:
        target = target.strip()
        description = description.strip()
        if not target:
            return
        if target in by_target:
            index = by_target[target]
            if len(description) > len(result[index]["description"]):
                result[index]["description"] = description
            if relation_type and not result[index].get("type"):
                result[index]["type"] = relation_type
            return
        by_target[target] = len(result)
        result.append(
            {"target": target, "description": description, "type": relation_type}
        )

    teammate_names = {
        "zusr_artem_wing": "左然",
        "vyn_richter": "莫弈",
        "marius_von_hagen": "陆景和",
    }
    for item in entries:
        key = str(item.get("_key") or "").strip()
        target = first_text(item, "target", "name")
        description = first_text(
            item,
            "description",
            "relational_significance",
            "interpersonal_dynamic",
            "relationship_nature",
            "dynamics",
        )
        relation_type = first_text(item, "type", "relation_type")
        roles = first_text(item, "identity_and_roles")

        if key == "nxx_teammates":
            teammates = item.get("nxx_teammates")
            if isinstance(teammates, dict):
                for name, detail in teammates.items():
                    add(teammate_names.get(name, name), text(detail), "NXX 队友")
            continue

        if key in {"female_protagonist", "heroine_rose", "female_lead"} or target in {
            "女主",
            "女主角",
        }:
            target = "女主角（蔷薇/Rosa）"
            if roles:
                description = f"{roles}；{description}" if description else roles
            relation_type = relation_type or "青梅竹马/恋人"
        elif key in {"pet_peanut", "pet_bird_peanut"} or "花生" in target:
            target = "鹩哥“花生”"
            relation_type = relation_type or "宠物伙伴"
        elif key in {
            "physician_yang_xiao",
            "doctor_colleague_yang_xiao",
        } or "扬笑" in target:
            target = "扬笑"
            relation_type = relation_type or "主治医师"

        target = target.replace("（蔷薇）", "（蔷薇/Rosa）")
        target = target.replace("（代号蔷薇 / Rosa）", "（蔷薇/Rosa）")
        add(target, description, relation_type)
    return result


def normalize_failures(raw: Any) -> list[dict[str, str]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("failure_patterns") or []
    result: list[dict[str, str]] = []
    for item in as_list(items):
        data = keyed(item)
        if data is None:
            continue
        name = first_text(data, "pattern_name", "name", "pattern", "_key")
        trigger = first_text(data, "trigger", "stimulus", "situation")
        consequences = first_text(
            data, "consequences", "consequence", "impact", "outcome"
        ) or first_text(data, "manifestation", "behavior", "description")
        if name:
            result.append(
                {
                    "pattern_name": name,
                    "trigger": trigger,
                    "consequences": consequences,
                }
            )
    return result


def normalize_decisions(raw: Any) -> list[dict[str, str]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("decision_heuristics") or raw.get("mental_models") or []
    result: list[dict[str, str]] = []
    for item in as_list(items):
        data = keyed(item)
        if data is None:
            continue
        name = first_text(data, "name", "model", "principle", "_key")
        description = first_text(
            data,
            "description",
            "heuristic",
            "strategy",
            "model",
            "principle",
            "summary",
        )
        if name and description:
            result.append({"name": name, "description": description})
    return result


def normalize_contradictions(raw: Any) -> list[dict[str, str]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("contradictions") or []
    result: list[dict[str, str]] = []
    for item in as_list(items):
        data = keyed(item)
        if data is None:
            continue
        theme = first_text(
            data, "theme", "aspect", "name", "manifestation", "type", "id"
        )
        thesis = first_text(data, "thesis")
        antithesis = first_text(data, "antithesis")
        synthesis = first_text(data, "synthesis")
        description = first_text(data, "description", "manifestation")
        if theme or description or thesis:
            result.append(
                {
                    "theme": theme,
                    "thesis": thesis,
                    "antithesis": antithesis,
                    "synthesis": synthesis,
                    "description": description,
                }
            )
    return result


def normalize_temperament(raw: Any) -> str:
    data = raw if isinstance(raw, dict) else {}
    if isinstance(data.get("temperament"), dict):
        data = data["temperament"]
    return first_text(data, "temperament", "core_profile", "description", "summary")


def normalize_self_narrative(raw: Any) -> list[dict[str, str]]:
    items = raw
    if isinstance(raw, dict):
        items = raw.get("self_narrative_evidence") or raw.get("self_narrative") or []
    result: list[dict[str, str]] = []
    for item in as_list(items):
        data = keyed(item)
        if data is None:
            continue
        pattern = first_text(
            data, "narrative_pattern", "pattern", "theme", "name"
        )
        mechanism = first_text(
            data,
            "psychological_mechanism",
            "evidence_content",
            "description",
            "mechanism",
        )
        if pattern and mechanism:
            result.append(
                {"narrative_pattern": pattern, "psychological_mechanism": mechanism}
            )
    return result


def normalize_attachment(raw: Any) -> list[str]:
    data = raw if isinstance(raw, dict) else {}
    if isinstance(data.get("attachment_patterns"), dict):
        data = data["attachment_patterns"]
    lines: list[str] = []
    for key in ("core_style", "primary_style", "manifestation", "dynamics"):
        value = first_text(data, key)
        if value:
            lines.append(value)
    return _dedupe(lines)


def normalize_compiled_components(components: dict[str, Any]) -> dict[str, Any]:
    result = dict(components)
    if "identity_profile" in result:
        result["identity_profile"] = normalize_profile(result["identity_profile"])
    if "timeline_events" in result:
        result["timeline_events"] = normalize_timeline(result["timeline_events"])
    if "embodied_identity" in result:
        raw_body = result["embodied_identity"]
        body = normalize_body(raw_body)
        raw_keys = set(raw_body) if isinstance(raw_body, dict) else set()
        if "body_features" not in raw_keys:
            body.pop("body_features", None)
        if "physical_conditions" not in raw_keys:
            body.pop("physical_conditions", None)
        result["embodied_identity"] = body
    if "emotional_triggers" in result:
        result["emotional_triggers"] = normalize_triggers(
            result["emotional_triggers"]
        )
    if "dialogue_examples" in result:
        result["dialogue_examples"] = normalize_dialogue(
            result["dialogue_examples"]
        )
    if "expression_style" in result:
        result["expression_style"] = normalize_style(result["expression_style"])
    if "relationships" in result:
        result["relationships"] = normalize_relationships(result["relationships"])
    if "failure_patterns" in result:
        result["failure_patterns"] = normalize_failures(result["failure_patterns"])
    if "decision_heuristics" in result:
        result["decision_heuristics"] = normalize_decisions(
            result["decision_heuristics"]
        )
    if "mental_models" in result:
        result["mental_models"] = normalize_decisions(result["mental_models"])
    if "contradictions" in result:
        result["contradictions"] = normalize_contradictions(
            result["contradictions"]
        )
    if "temperament" in result:
        temperament = normalize_temperament(result["temperament"])
        result["temperament"] = {"temperament": temperament} if temperament else {}
    if "self_narrative_evidence" in result:
        result["self_narrative_evidence"] = normalize_self_narrative(
            result["self_narrative_evidence"]
        )
    if "attachment_patterns" in result:
        attachment = normalize_attachment(result["attachment_patterns"])
        result["attachment_patterns"] = attachment
    return result
