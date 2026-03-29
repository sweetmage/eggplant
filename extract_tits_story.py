#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import html
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


INTERESTING_CALLS = (
    "output",
    "output2",
    "showBust",
    "showImage",
    "showName",
    "addButton",
    "addGhostButton",
    "addDisabledButton",
    "addDisabledGhostButton",
    "addNextButton",
)
INTERESTING_SUBSTRINGS = tuple(f"{name}" for name in INTERESTING_CALLS)
INTERESTING_RG_PATTERN = (
    r"output\(|output2\(|showBust\(|showImage\(|showName\(|addButton\(|addGhostButton\(|"
    r"addDisabledButton\(|addDisabledGhostButton\(|addNextButton\("
)
INTERESTING_RG_TERMS = (
    "output(",
    "output2(",
    "showBust(",
    "showImage(",
    "showName(",
    "addButton(",
    "addGhostButton(",
    "addDisabledButton(",
    "addDisabledGhostButton(",
    "addNextButton(",
)

CALL_NAME_RE = re.compile(
    r"(?<![\w$])(?:(?:[A-Za-z_]\w*)\s*\.\s*)*"
    r"(output2?|showBust|showImage|showName|addButton|addGhostButton|addDisabledButton|addDisabledGhostButton|addNextButton)\s*\(",
    re.MULTILINE,
)
FUNCTION_DEF_RE = re.compile(
    r"(?m)^[ \t]*(?:public|private|protected|internal|static|final|override|\s)*function\s+([A-Za-z_]\w*)\s*\(",
)
EMBED_RE = re.compile(
    r"""\[Embed\(\s*source\s*=\s*"([^"]+)"[^]]*\)\]\s*public\s+(?:static\s+)?(?:var|const)\s+([A-Za-z_]\w*)\s*:\s*Class""",
    re.MULTILINE,
)
WHITESPACE_RE = re.compile(r"\s+")


def normalize_path(path: Path) -> str:
    return path.as_posix()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").replace("\ufeff", "")


def decode_string(token: str) -> str:
    try:
        return ast.literal_eval(token)
    except Exception:
        return token[1:-1]


def line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def skip_string(text: str, index: int) -> int:
    quote = text[index]
    i = index + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return len(text)


def skip_line_comment(text: str, index: int) -> int:
    next_newline = text.find("\n", index)
    return len(text) if next_newline == -1 else next_newline


def skip_block_comment(text: str, index: int) -> int:
    end = text.find("*/", index + 2)
    return len(text) if end == -1 else end + 2


def skip_ws_and_comments(text: str, index: int) -> int:
    i = index
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith("//", i):
            i = skip_line_comment(text, i)
            continue
        if text.startswith("/*", i):
            i = skip_block_comment(text, i)
            continue
        break
    return i


def is_keyword_at(text: str, index: int, keyword: str) -> bool:
    if not text.startswith(keyword, index):
        return False
    before = text[index - 1] if index > 0 else ""
    after_index = index + len(keyword)
    after = text[after_index] if after_index < len(text) else ""
    if before and (before.isalnum() or before == "_"):
        return False
    if after and (after.isalnum() or after == "_"):
        return False
    return True


def find_matching(text: str, start_index: int, open_char: str, close_char: str) -> int:
    depth = 0
    i = start_index
    while i < len(text):
        ch = text[i]
        if ch == '"' or ch == "'":
            i = skip_string(text, i)
            continue
        if text.startswith("//", i):
            i = skip_line_comment(text, i)
            continue
        if text.startswith("/*", i):
            i = skip_block_comment(text, i)
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def split_top_level(text: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    start = 0
    paren = bracket = brace = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"' or ch == "'":
            i = skip_string(text, i)
            continue
        if text.startswith("//", i):
            i = skip_line_comment(text, i)
            continue
        if text.startswith("/*", i):
            i = skip_block_comment(text, i)
            continue
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace = max(0, brace - 1)
        elif ch == delimiter and paren == 0 and bracket == 0 and brace == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    elif not parts:
        parts.append("")
    return parts


def read_statement_end(text: str, start_index: int) -> int:
    paren = bracket = brace = 0
    i = start_index
    while i < len(text):
        ch = text[i]
        if ch == '"' or ch == "'":
            i = skip_string(text, i)
            continue
        if text.startswith("//", i):
            i = skip_line_comment(text, i)
            continue
        if text.startswith("/*", i):
            i = skip_block_comment(text, i)
            continue
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif ch == "{":
            brace += 1
        elif ch == "}":
            if paren == 0 and bracket == 0 and brace == 0:
                return i + 1
            brace = max(0, brace - 1)
        elif ch == ";" and paren == 0 and bracket == 0 and brace == 0:
            return i + 1
        i += 1
    return len(text)


def parse_condition_regions(text: str) -> list[dict[str, Any]]:
    if "if" not in text and "else" not in text:
        return []
    regions: list[dict[str, Any]] = []

    def read_statement_bounds(index: int) -> tuple[int, int, bool]:
        start = skip_ws_and_comments(text, index)
        if start >= len(text):
            return start, start, False
        if text[start] == "{":
            end = find_matching(text, start, "{", "}")
            inner_start = start + 1
            inner_end = end if end != -1 else len(text)
            walk_block(inner_start, inner_end)
            return inner_start, inner_end, True
        end = read_statement_end(text, start)
        walk_block(start, end)
        return start, end, False

    def walk_block(start: int, end: int) -> None:
        i = start
        while i < end:
            i = skip_ws_and_comments(text, i)
            if i >= end:
                break

            if is_keyword_at(text, i, "if"):
                paren_start = text.find("(", i)
                if paren_start == -1 or paren_start >= end:
                    i += 2
                    continue
                paren_end = find_matching(text, paren_start, "(", ")")
                if paren_end == -1:
                    i += 2
                    continue
                cond_text = text[paren_start + 1 : paren_end].strip()
                stmt_start, stmt_end, _ = read_statement_bounds(paren_end + 1)
                regions.append(
                    {
                        "start": stmt_start,
                        "end": stmt_end,
                        "label": f"if ({WHITESPACE_RE.sub(' ', cond_text)})",
                    }
                )
                i = stmt_end

                while True:
                    j = skip_ws_and_comments(text, i)
                    if not is_keyword_at(text, j, "else"):
                        i = j
                        break
                    k = skip_ws_and_comments(text, j + 4)
                    if is_keyword_at(text, k, "if"):
                        paren_start = text.find("(", k)
                        if paren_start == -1 or paren_start >= end:
                            i = k + 2
                            break
                        paren_end = find_matching(text, paren_start, "(", ")")
                        if paren_end == -1:
                            i = k + 2
                            break
                        cond_text = text[paren_start + 1 : paren_end].strip()
                        stmt_start, stmt_end, _ = read_statement_bounds(paren_end + 1)
                        regions.append(
                            {
                                "start": stmt_start,
                                "end": stmt_end,
                                "label": f"else if ({WHITESPACE_RE.sub(' ', cond_text)})",
                            }
                        )
                        i = stmt_end
                        continue

                    stmt_start, stmt_end, _ = read_statement_bounds(k)
                    regions.append({"start": stmt_start, "end": stmt_end, "label": "else"})
                    i = stmt_end
                    break
                continue

            if text[i] == "{":
                close = find_matching(text, i, "{", "}")
                if close == -1:
                    return
                walk_block(i + 1, close)
                i = close + 1
                continue

            stmt_end = read_statement_end(text, i)
            if stmt_end <= i:
                stmt_end = i + 1
            i = stmt_end

    walk_block(0, len(text))
    regions.sort(key=lambda item: (item["start"], item["end"] - item["start"]))
    return regions


def contexts_for_position(regions: Sequence[dict[str, Any]], position: int) -> list[str]:
    matches = [item for item in regions if item["start"] <= position < item["end"]]
    matches.sort(key=lambda item: (item["start"], -(item["end"] - item["start"])))
    return [item["label"] for item in matches]


def literal_or_placeholder(expr: str) -> str:
    token = expr.strip()
    if not token:
        return ""
    if (token.startswith('"') and token.endswith('"')) or (token.startswith("'") and token.endswith("'")):
        return decode_string(token)
    return "{{ " + WHITESPACE_RE.sub(" ", token) + " }}"


def expression_to_template(expr: str) -> str:
    first_arg = split_top_level(expr, ",")[0]
    parts = split_top_level(first_arg, "+")
    rendered: list[str] = []
    for part in parts:
        piece = literal_or_placeholder(part)
        if piece:
            rendered.append(piece)
    return "".join(rendered)


def template_preview(template: str, limit: int = 140) -> str:
    text = WHITESPACE_RE.sub(" ", template.replace("\n", " ").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def decode_label(expr: str) -> str:
    label = literal_or_placeholder(expr)
    return WHITESPACE_RE.sub(" ", label).strip() or "Unlabeled"


def callback_signature(callback_expr: str) -> dict[str, Any]:
    cleaned = callback_expr.strip()
    if not cleaned:
        return {"kind": "missing", "expr": "", "name": ""}
    if cleaned.startswith("function"):
        return {"kind": "inline", "expr": cleaned, "name": "inline"}
    call_name = cleaned
    if "(" in cleaned:
        call_name = cleaned.split("(", 1)[0].strip()
    simple_name = call_name.split(".")[-1].strip()
    return {
        "kind": "named",
        "expr": cleaned,
        "name": simple_name,
        "qualified_name": call_name,
    }


def parse_parameters(signature_blob: str) -> list[str]:
    params: list[str] = []
    if not signature_blob.strip():
        return params
    for part in split_top_level(signature_blob, ","):
        token = part.strip()
        if not token:
            continue
        name = token.split(":", 1)[0].split("=", 1)[0].strip()
        if name:
            params.append(name)
    return params


def extract_named_functions(path: Path, source_root: Path, text: str | None = None) -> list[dict[str, Any]]:
    if text is None:
        text = read_text(path)
    functions: list[dict[str, Any]] = []
    for match in FUNCTION_DEF_RE.finditer(text):
        name = match.group(1)
        params_start = text.find("(", match.start())
        params_end = find_matching(text, params_start, "(", ")")
        if params_start == -1 or params_end == -1:
            continue
        brace_start = text.find("{", params_end)
        if brace_start == -1:
            continue
        brace_end = find_matching(text, brace_start, "{", "}")
        if brace_end == -1:
            continue
        signature_blob = text[params_start + 1 : params_end]
        body = text[brace_start + 1 : brace_end]
        relative_path = path.relative_to(source_root)
        functions.append(
            {
                "name": name,
                "relative_path": normalize_path(relative_path),
                "abs_path": normalize_path(path),
                "parameters": parse_parameters(signature_blob),
                "start_line": line_number(text, match.start()),
                "end_line": line_number(text, brace_end),
                "body": body,
                "body_start_index": brace_start + 1,
                "full_text": text,
            }
        )
    return functions


def extract_call_blob(source: str, start_index: int) -> tuple[str, int]:
    open_paren = source.find("(", start_index)
    if open_paren == -1:
        return "", start_index
    close_paren = find_matching(source, open_paren, "(", ")")
    if close_paren == -1:
        return "", start_index
    return source[open_paren + 1 : close_paren], close_paren + 1


def call_events_for_function(function: dict[str, Any]) -> list[dict[str, Any]]:
    body = function["body"]
    if not any(token in body for token in INTERESTING_SUBSTRINGS):
        return []
    regions = parse_condition_regions(body)
    events: list[dict[str, Any]] = []
    for match in CALL_NAME_RE.finditer(body):
        call_name = match.group(1)
        blob, _ = extract_call_blob(body, match.start())
        position = match.start()
        contexts = contexts_for_position(regions, position)
        call_line = function["start_line"] + line_number(body, position) - 1
        events.append(
            {
                "call": call_name,
                "blob": blob,
                "position": position,
                "line": call_line,
                "contexts": contexts,
            }
        )
    events.sort(key=lambda item: item["position"])
    return events


def extract_embed_map(path: Path, source_root: Path) -> dict[str, list[dict[str, str]]]:
    text = read_text(path)
    result: dict[str, list[dict[str, str]]] = {}
    for source_str, symbol in EMBED_RE.findall(text):
        image_path = (path.parent / source_str).resolve()
        result.setdefault(symbol, []).append(
            {
                "symbol": symbol,
                "source_file": normalize_path(path.relative_to(source_root)),
                "path": normalize_path(image_path),
                "relative_image_path": normalize_path(image_path.relative_to(source_root)),
            }
        )
    return result


def build_bust_and_image_maps(source_root: Path) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    bust_map: dict[str, list[dict[str, str]]] = {}
    for bust_file in sorted((source_root / "classes" / "Resources" / "Busts").glob("*.as")):
        embed_map = extract_embed_map(bust_file, source_root)
        for symbol, entries in embed_map.items():
            if not symbol.startswith("Bust_"):
                continue
            bust_name = symbol[len("Bust_") :]
            bust_map.setdefault(bust_name, []).extend(entries)

    image_map: dict[str, list[dict[str, str]]] = {}
    image_pack_file = source_root / "classes" / "Resources" / "ImagePack.as"
    if image_pack_file.exists():
        embed_map = extract_embed_map(image_pack_file, source_root)
        for symbol, entries in embed_map.items():
            image_map.setdefault(symbol, []).extend(entries)

    return bust_map, image_map


def find_candidate_scene_files(source_root: Path, folder_names: Sequence[str]) -> list[Path]:
    roots = [source_root / folder_name for folder_name in folder_names if (source_root / folder_name).exists()]
    if not roots:
        return []

    if os.name != "nt":
        try:
            rg_cmd = ["rg", "-l", "-F", "-g", "*.as"]
            for term in INTERESTING_RG_TERMS:
                rg_cmd.extend(["-e", term])
            rg_cmd.extend(str(folder) for folder in roots)
            result = subprocess.run(
                rg_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode in {0, 1}:
                paths = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
                if paths:
                    return sorted(path.resolve() for path in paths)
        except FileNotFoundError:
            pass

    paths: list[Path] = []
    scanned = 0
    for folder in roots:
        for path in sorted(folder.rglob("*.as")):
            scanned += 1
            if scanned <= 10 or scanned % 100 == 0:
                print(f"  scanning candidate file {scanned}: {path.name}")
            try:
                text = read_text(path)
            except Exception as exc:
                print(f"  skipped candidate file {normalize_path(path)}: {exc}")
                continue
            if any(token in text for token in INTERESTING_SUBSTRINGS):
                paths.append(path.resolve())
    return sorted(paths)


def merge_text_chunks(text_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for chunk in text_chunks:
        if (
            merged
            and merged[-1]["contexts"] == chunk["contexts"]
            and merged[-1]["kind"] == chunk["kind"]
            and chunk["kind"] in {"output", "output2"}
        ):
            merged[-1]["template"] += chunk["template"]
            merged[-1]["preview"] = template_preview(merged[-1]["template"])
            merged[-1]["end_line"] = chunk["line"]
        else:
            merged.append(
                {
                    "kind": chunk["kind"],
                    "template": chunk["template"],
                    "preview": chunk["preview"],
                    "line": chunk["line"],
                    "end_line": chunk["line"],
                    "contexts": list(chunk["contexts"]),
                }
            )
    return merged


def evaluate_simple_condition(condition: str, arg_map: dict[str, str]) -> bool | None:
    clean = condition.strip()
    if clean.startswith("if "):
        clean = clean[3:].strip()
    if clean.startswith("else if "):
        clean = clean[8:].strip()
    if clean.startswith("(") and clean.endswith(")"):
        clean = clean[1:-1].strip()

    if "&&" in clean or "||" in clean:
        parts = re.split(r"(\&\&|\|\|)", clean)
        values: list[bool | None] = []
        operators: list[str] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if part in {"&&", "||"}:
                operators.append(part)
                continue
            values.append(evaluate_simple_condition(part, arg_map))
        if not values:
            return None
        current = values[0]
        for idx, op in enumerate(operators, start=1):
            nxt = values[idx]
            if current is None or nxt is None:
                current = None
            elif op == "&&":
                current = current and nxt
            else:
                current = current or nxt
        return current

    comparison_patterns = [
        r"^([A-Za-z_]\w*)\s*(==|!=|<=|>=|<|>)\s*([A-Za-z_]\w*|[-]?\d+|undefined|null|true|false|\"[^\"]*\"|'[^']*')$",
        r"^([A-Za-z_]\w*)\s*=\s*=\s*([A-Za-z_]\w*|[-]?\d+)$",
    ]
    for pattern in comparison_patterns:
        match = re.match(pattern, clean)
        if not match:
            continue
        left, op, right = match.groups()[:3]
        if left not in arg_map and right not in arg_map:
            return None

        def value_of(token: str) -> Any:
            token = token.strip()
            if token in arg_map:
                raw = arg_map[token]
                if re.fullmatch(r"-?\d+", raw):
                    return int(raw)
                return raw
            if token == "undefined":
                return "undefined"
            if token == "null":
                return None
            if token == "true":
                return True
            if token == "false":
                return False
            if (token.startswith('"') and token.endswith('"')) or (token.startswith("'") and token.endswith("'")):
                return decode_string(token)
            if re.fullmatch(r"-?\d+", token):
                return int(token)
            return token

        left_value = value_of(left)
        right_value = value_of(right)
        try:
            if op == "==":
                return left_value == right_value
            if op == "!=":
                return left_value != right_value
            if op == "<":
                return left_value < right_value
            if op == ">":
                return left_value > right_value
            if op == "<=":
                return left_value <= right_value
            if op == ">=":
                return left_value >= right_value
        except Exception:
            return None
    return None


def contexts_match_args(contexts: Sequence[str], arg_map: dict[str, str]) -> bool:
    if not arg_map:
        return True
    seen_relevant = False
    for context in contexts:
        if context == "else":
            continue
        if not any(re.search(rf"\b{re.escape(name)}\b", context) for name in arg_map):
            continue
        seen_relevant = True
        outcome = evaluate_simple_condition(context, arg_map)
        if outcome is False:
            return False
    return True if seen_relevant else True


def make_inline_node_id(parent_id: str, index: int) -> str:
    return f"{parent_id}::inline::{index}"


def make_unresolved_node_id(expr: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", expr).strip("_") or "unknown"
    return f"unresolved::{slug}"


def make_bound_node_id(base_id: str, arg_values: list[str]) -> str:
    encoded = ",".join(arg_values)
    return f"{base_id}::call::{encoded}"


@dataclass
class SceneNode:
    id: str
    function_name: str
    relative_path: str
    abs_path: str
    start_line: int
    end_line: int
    parameters: list[str] = field(default_factory=list)
    title: str = ""
    title_contexts: list[str] = field(default_factory=list)
    text_blocks: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    buttons: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    derived_from: str | None = None
    bound_args: dict[str, str] = field(default_factory=dict)


def build_scene_nodes(
    source_root: Path,
    bust_map: dict[str, list[dict[str, str]]],
    image_map: dict[str, list[dict[str, str]]],
    folder_names: Sequence[str],
) -> list[SceneNode]:
    functions: list[dict[str, Any]] = []
    candidate_files = find_candidate_scene_files(source_root, folder_names)
    print(f"  candidate scene files: {len(candidate_files)}")
    for path in candidate_files:
        try:
            text = read_text(path)
            functions.extend(extract_named_functions(path, source_root, text=text))
        except Exception as exc:
            print(f"  skipped function extraction for {normalize_path(path)}: {exc}")

    name_index: dict[str, list[dict[str, Any]]] = {}
    id_index: dict[str, dict[str, Any]] = {}
    for function in functions:
        node_id = f"{function['relative_path']}::{function['name']}"
        function["id"] = node_id
        name_index.setdefault(function["name"], []).append(function)
        id_index[node_id] = function

    rough_scene_count = sum(1 for function in functions if any(token in function["body"] for token in INTERESTING_SUBSTRINGS))
    print(f"  rough scene-like functions: {rough_scene_count}")

    nodes: list[SceneNode] = []
    pending_inline_nodes: list[SceneNode] = []
    pending_placeholder_nodes: dict[str, SceneNode] = {}

    processed_scene_functions = 0
    for function in functions:
        if not any(token in function["body"] for token in INTERESTING_SUBSTRINGS):
            continue
        processed_scene_functions += 1
        if processed_scene_functions <= 10 or processed_scene_functions % 250 == 0:
            print(
                f"  parsing scene-like function {processed_scene_functions}: "
                f"{function['relative_path']}::{function['name']}"
            )
        events = call_events_for_function(function)
        text_blocks: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        buttons: list[dict[str, Any]] = []
        title = ""
        title_contexts: list[str] = []

        for index, event in enumerate(events):
            call = event["call"]
            blob = event["blob"]
            contexts = event["contexts"]
            line = event["line"]

            if call in {"output", "output2"}:
                template = expression_to_template(blob)
                if template.strip():
                    text_blocks.append(
                        {
                            "kind": call,
                            "template": template,
                            "preview": template_preview(template),
                            "line": line,
                            "contexts": contexts,
                        }
                    )
                continue

            if call == "showName":
                show_name = expression_to_template(blob).strip()
                if show_name and not title:
                    title = show_name
                    title_contexts = contexts
                continue

            if call == "showBust":
                bust_args = split_top_level(blob, ",")
                labels: list[str] = []
                resolved_paths: list[dict[str, str]] = []
                unresolved: list[str] = []
                for arg in bust_args:
                    token = arg.strip()
                    if not token:
                        continue
                    if (token.startswith('"') and token.endswith('"')) or (token.startswith("'") and token.endswith("'")):
                        bust_name = decode_string(token)
                        labels.append(bust_name)
                        resolved_paths.extend(bust_map.get(bust_name, []))
                    else:
                        unresolved.append(WHITESPACE_RE.sub(" ", token))
                images.append(
                    {
                        "kind": "bust",
                        "line": line,
                        "contexts": contexts,
                        "labels": labels,
                        "unresolved": unresolved,
                        "resolved": resolved_paths,
                    }
                )
                continue

            if call == "showImage":
                image_args = split_top_level(blob, ",")
                labels: list[str] = []
                resolved_paths: list[dict[str, str]] = []
                unresolved: list[str] = []
                for arg in image_args:
                    token = arg.strip()
                    if not token:
                        continue
                    if (token.startswith('"') and token.endswith('"')) or (token.startswith("'") and token.endswith("'")):
                        image_name = decode_string(token)
                        labels.append(image_name)
                        resolved_paths.extend(image_map.get(image_name, []))
                    else:
                        unresolved.append(WHITESPACE_RE.sub(" ", token))
                images.append(
                    {
                        "kind": "image",
                        "line": line,
                        "contexts": contexts,
                        "labels": labels,
                        "unresolved": unresolved,
                        "resolved": resolved_paths,
                    }
                )
                continue

            if call == "addNextButton":
                parts = split_top_level(blob, ",")
                callback = callback_signature(parts[0] if parts else "")
                button = {
                    "line": line,
                    "contexts": contexts,
                    "label": "Next",
                    "disabled": False,
                    "call_kind": call,
                    "target_expr": callback.get("expr", ""),
                    "target_name": callback.get("name", ""),
                    "target_id": None,
                    "arg_expr": "",
                }
                if callback["kind"] == "inline":
                    inline_id = make_inline_node_id(function["id"], index)
                    button["target_id"] = inline_id
                    pending_inline_nodes.append(
                        SceneNode(
                            id=inline_id,
                            function_name="inline callback",
                            relative_path=function["relative_path"],
                            abs_path=function["abs_path"],
                            start_line=line,
                            end_line=line,
                            title="Inline Callback",
                            text_blocks=[
                                {
                                    "kind": "code",
                                    "template": callback["expr"],
                                    "preview": template_preview(callback["expr"]),
                                    "line": line,
                                    "end_line": line,
                                    "contexts": contexts,
                                }
                            ],
                            notes=["This button uses an inline callback instead of a named scene function."],
                        )
                    )
                else:
                    button["target_id"] = callback.get("qualified_name") or callback.get("name")
                buttons.append(button)
                continue

            if call in {"addButton", "addGhostButton", "addDisabledButton", "addDisabledGhostButton"}:
                parts = split_top_level(blob, ",")
                label_expr = parts[1] if len(parts) > 1 else '"Unlabeled"'
                label = decode_label(label_expr)
                disabled = "Disabled" in call
                target_expr = parts[2] if len(parts) > 2 else ""
                arg_expr = parts[3] if len(parts) > 3 else ""
                callback = callback_signature(target_expr)
                button = {
                    "line": line,
                    "contexts": contexts,
                    "label": label,
                    "disabled": disabled,
                    "call_kind": call,
                    "target_expr": callback.get("expr", ""),
                    "target_name": callback.get("name", ""),
                    "target_id": None,
                    "arg_expr": arg_expr.strip(),
                }
                if not disabled:
                    if callback["kind"] == "inline":
                        inline_id = make_inline_node_id(function["id"], index)
                        button["target_id"] = inline_id
                        pending_inline_nodes.append(
                            SceneNode(
                                id=inline_id,
                                function_name="inline callback",
                                relative_path=function["relative_path"],
                                abs_path=function["abs_path"],
                                start_line=line,
                                end_line=line,
                                title=f"Inline Callback: {label}",
                                text_blocks=[
                                    {
                                        "kind": "code",
                                        "template": callback["expr"],
                                        "preview": template_preview(callback["expr"]),
                                        "line": line,
                                        "end_line": line,
                                        "contexts": contexts,
                                    }
                                ],
                                notes=["This choice is backed by an inline function body."],
                            )
                        )
                    elif callback["kind"] == "named":
                        button["target_id"] = callback.get("qualified_name") or callback.get("name")
                    else:
                        button["target_id"] = None
                buttons.append(button)

        node = SceneNode(
            id=function["id"],
            function_name=function["name"],
            relative_path=function["relative_path"],
            abs_path=function["abs_path"],
            start_line=function["start_line"],
            end_line=function["end_line"],
            parameters=function["parameters"],
            title=title,
            title_contexts=title_contexts,
            text_blocks=merge_text_chunks(text_blocks),
            images=images,
            buttons=buttons,
        )
        if not node.text_blocks and not node.images and not node.buttons and not node.title:
            continue
        nodes.append(node)

    nodes.extend(pending_inline_nodes)

    node_index = {node.id: node for node in nodes}
    function_nodes = {node.id: node for node in nodes if "::inline::" not in node.id and not node.id.startswith("unresolved::")}
    name_to_nodes: dict[str, list[SceneNode]] = {}
    for node in function_nodes.values():
        name_to_nodes.setdefault(node.function_name, []).append(node)

    def resolve_target(current_node: SceneNode, button: dict[str, Any]) -> str | None:
        existing = button.get("target_id")
        if existing and existing in node_index:
            return existing
        if not existing:
            return None

        arg_expr = button.get("arg_expr", "").strip()
        target_name = button.get("target_name") or existing.split(".")[-1]

        same_path_matches = [
            node for node in name_to_nodes.get(target_name, []) if node.relative_path == current_node.relative_path
        ]
        if len(same_path_matches) == 1:
            target = same_path_matches[0]
        elif len(name_to_nodes.get(target_name, [])) == 1:
            target = name_to_nodes[target_name][0]
        else:
            unresolved_id = make_unresolved_node_id(existing)
            if unresolved_id not in pending_placeholder_nodes:
                pending_placeholder_nodes[unresolved_id] = SceneNode(
                    id=unresolved_id,
                    function_name=target_name or "unresolved target",
                    relative_path=current_node.relative_path,
                    abs_path=current_node.abs_path,
                    start_line=button["line"],
                    end_line=button["line"],
                    title="Unresolved Target",
                    text_blocks=[
                        {
                            "kind": "note",
                            "template": f"Couldn't match `{existing}` to one named scene function.",
                            "preview": f"Couldn't match `{existing}` to one named scene function.",
                            "line": button["line"],
                            "end_line": button["line"],
                            "contexts": [],
                        }
                    ],
                    notes=["This usually means the button points at game systems, combat code, or a dynamic callback."],
                )
            return unresolved_id

        if arg_expr and target.parameters:
            bound_literals = []
            for piece in split_top_level(arg_expr, ","):
                value = piece.strip()
                if re.fullmatch(r"-?\d+", value):
                    bound_literals.append(value)
                elif value in {"true", "false", "undefined", "null"}:
                    bound_literals.append(value)
                elif (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                    bound_literals.append(decode_string(value))
                else:
                    return target.id

            bound_map: dict[str, str] = {}
            for idx, value in enumerate(bound_literals):
                if idx < len(target.parameters):
                    bound_map[target.parameters[idx]] = str(value)

            if bound_map:
                bound_id = make_bound_node_id(target.id, [str(v) for v in bound_literals])
                if bound_id not in node_index and bound_id not in pending_placeholder_nodes:
                    filtered_text = [
                        chunk
                        for chunk in target.text_blocks
                        if contexts_match_args(chunk.get("contexts", []), bound_map)
                    ]
                    filtered_images = [
                        image for image in target.images if contexts_match_args(image.get("contexts", []), bound_map)
                    ]
                    filtered_buttons = [
                        inner
                        for inner in target.buttons
                        if contexts_match_args(inner.get("contexts", []), bound_map)
                    ]
                    if not filtered_text and not filtered_images and not filtered_buttons:
                        filtered_text = list(target.text_blocks)
                        filtered_images = list(target.images)
                        filtered_buttons = list(target.buttons)
                    pending_placeholder_nodes[bound_id] = SceneNode(
                        id=bound_id,
                        function_name=target.function_name,
                        relative_path=target.relative_path,
                        abs_path=target.abs_path,
                        start_line=target.start_line,
                        end_line=target.end_line,
                        parameters=target.parameters,
                        title=target.title or target.function_name,
                        title_contexts=list(target.title_contexts),
                        text_blocks=list(filtered_text),
                        images=list(filtered_images),
                        buttons=list(filtered_buttons),
                        notes=[f"Bound call with arguments: {json.dumps(bound_map, ensure_ascii=False)}"],
                        derived_from=target.id,
                        bound_args=bound_map,
                    )
                return bound_id

        return target.id

    for node in nodes:
        for button in node.buttons:
            if button.get("disabled"):
                continue
            button["target_id"] = resolve_target(node, button)

    nodes.extend(pending_placeholder_nodes.values())
    nodes.sort(key=lambda item: (item.relative_path, item.start_line, item.id))
    return nodes


def collect_all_images(source_root: Path) -> list[dict[str, Any]]:
    images_root = source_root / "assets" / "images"
    records: list[dict[str, Any]] = []
    if not images_root.exists():
        return records
    for path in sorted(images_root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        records.append(
            {
                "id": normalize_path(path.relative_to(images_root)),
                "absolute_path": normalize_path(path),
                "relative_path": normalize_path(path.relative_to(source_root)),
                "size_bytes": stat.st_size,
                "extension": path.suffix.lower(),
            }
        )
    return records


def referenced_image_index(nodes: Sequence[SceneNode]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for node in nodes:
        for image in node.images:
            for resolved in image.get("resolved", []):
                path = resolved["path"]
                index.setdefault(path, set()).add(node.id)
    return index


def serialize_nodes(nodes: Sequence[SceneNode]) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    for node in nodes:
        payload = asdict(node)
        data.append(payload)
    return data


def human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def build_data_js(payload: dict[str, Any]) -> str:
    return "window.EGGPLANT_DATA = " + json.dumps(payload, indent=2, ensure_ascii=False) + ";\n"


def build_index_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Eggplant Story Export</title>
  <style>
    :root {
      --bg: #f4efe7;
      --panel: #fffaf2;
      --ink: #2a211a;
      --muted: #6b5a4d;
      --line: #d7c5b4;
      --accent: #b44f2b;
      --accent-2: #2f6f63;
      --shadow: 0 18px 45px rgba(86, 53, 29, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(180,79,43,0.18), transparent 28%),
        linear-gradient(180deg, #f8f2ea 0%, #efe7dc 100%);
      min-height: 100vh;
    }
    .shell {
      display: grid;
      grid-template-columns: minmax(280px, 360px) 1fr;
      gap: 24px;
      padding: 24px;
    }
    .panel {
      background: color-mix(in srgb, var(--panel) 92%, white);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .sidebar {
      display: flex;
      flex-direction: column;
      min-height: calc(100vh - 48px);
    }
    .side-head {
      padding: 22px 20px 14px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(255,255,255,0.68), rgba(255,255,255,0));
    }
    .eyebrow {
      font-size: 12px;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--accent);
      margin: 0 0 8px;
    }
    h1 {
      font-size: 28px;
      margin: 0 0 8px;
      line-height: 1;
    }
    .subtext, .meta, .empty-note, .chip {
      color: var(--muted);
    }
    .search-wrap {
      padding: 0 20px 18px;
      border-bottom: 1px solid var(--line);
    }
    input[type="search"] {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px 14px;
      font: inherit;
      background: rgba(255,255,255,0.85);
    }
    .scene-list {
      overflow: auto;
      padding: 10px;
    }
    .scene-row {
      width: 100%;
      text-align: left;
      border: 1px solid transparent;
      border-radius: 18px;
      background: transparent;
      padding: 14px 12px;
      cursor: pointer;
      font: inherit;
      color: inherit;
      transition: 140ms ease;
    }
    .scene-row:hover, .scene-row.active {
      background: rgba(180,79,43,0.08);
      border-color: rgba(180,79,43,0.18);
      transform: translateY(-1px);
    }
    .scene-title {
      font-size: 18px;
      margin: 0 0 6px;
    }
    .scene-path {
      font-size: 12px;
      margin-bottom: 6px;
    }
    .scene-preview {
      font-size: 14px;
      line-height: 1.35;
    }
    .viewer {
      min-height: calc(100vh - 48px);
      display: flex;
      flex-direction: column;
    }
    .viewer-head {
      padding: 24px 28px 18px;
      border-bottom: 1px solid var(--line);
      background:
        radial-gradient(circle at top right, rgba(47,111,99,0.12), transparent 28%),
        linear-gradient(180deg, rgba(255,255,255,0.72), rgba(255,255,255,0));
    }
    .viewer-body {
      padding: 24px 28px 30px;
      overflow: auto;
    }
    .top-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }
    .jump-btn, .choice-btn, .small-btn {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 10px 14px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      cursor: pointer;
      transition: 140ms ease;
    }
    .jump-btn:hover, .choice-btn:hover, .small-btn:hover {
      border-color: color-mix(in srgb, var(--accent) 45%, var(--line));
      background: rgba(180,79,43,0.07);
      transform: translateY(-1px);
    }
    .choice-btn[disabled] {
      cursor: not-allowed;
      opacity: 0.55;
      transform: none;
      background: rgba(0,0,0,0.03);
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 10px 0 0;
    }
    .chip {
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(47,111,99,0.08);
      border: 1px solid rgba(47,111,99,0.16);
      font-size: 12px;
    }
    .chunk, .image-card, .note-card {
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 18px;
      background: rgba(255,255,255,0.6);
      margin-bottom: 18px;
    }
    .chunk pre, .code-block {
      white-space: pre-wrap;
      margin: 0;
      font-family: "Georgia", serif;
      font-size: 17px;
      line-height: 1.55;
    }
    .code-block {
      font-family: "Consolas", "Menlo", monospace;
      font-size: 14px;
      background: #231d18;
      color: #f5efe7;
      padding: 16px;
      border-radius: 16px;
      overflow: auto;
    }
    .meta {
      font-size: 12px;
      margin-bottom: 10px;
    }
    .section-title {
      font-size: 12px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--accent);
      margin: 24px 0 12px;
    }
    .image-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
    }
    .image-frame {
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 10px;
      background: rgba(255,255,255,0.85);
    }
    .image-frame img {
      width: 100%;
      height: auto;
      display: block;
      border-radius: 10px;
      background: #efe5d8;
    }
    .image-caption {
      font-size: 12px;
      color: var(--muted);
      margin-top: 8px;
      word-break: break-word;
    }
    .choices {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .status-line {
      font-size: 13px;
      color: var(--muted);
      margin-top: 6px;
    }
    a { color: var(--accent-2); }
    @media (max-width: 980px) {
      .shell {
        grid-template-columns: 1fr;
      }
      .sidebar, .viewer {
        min-height: auto;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="panel sidebar">
      <div class="side-head">
        <p class="eyebrow">Eggplant Export</p>
        <h1>TiTS Script Browser</h1>
        <p class="subtext" id="summaryText"></p>
      </div>
      <div class="search-wrap">
        <input id="searchInput" type="search" placeholder="Search scenes, paths, or text">
        <div class="status-line" id="filterCount"></div>
      </div>
      <div class="scene-list" id="sceneList"></div>
    </aside>
    <main class="panel viewer">
      <div class="viewer-head">
        <div class="top-actions">
          <button class="small-btn" id="backButton" type="button">Back</button>
          <button class="small-btn" id="homeButton" type="button">Jump To Start</button>
          <a class="small-btn" href="./image-gallery.html">Image Gallery</a>
        </div>
        <div id="viewerTitleWrap"></div>
      </div>
      <div class="viewer-body" id="viewerBody"></div>
    </main>
  </div>
  <script src="./data.js"></script>
  <script>
    const data = window.EGGPLANT_DATA;
    const nodes = data.nodes;
    const nodeMap = new Map(nodes.map(node => [node.id, node]));
    const historyStack = [];
    let currentNodeId = nodes.length ? nodes[0].id : null;

    const sceneListEl = document.getElementById("sceneList");
    const viewerBodyEl = document.getElementById("viewerBody");
    const viewerTitleWrapEl = document.getElementById("viewerTitleWrap");
    const searchInputEl = document.getElementById("searchInput");
    const summaryTextEl = document.getElementById("summaryText");
    const filterCountEl = document.getElementById("filterCount");
    const backButtonEl = document.getElementById("backButton");
    const homeButtonEl = document.getElementById("homeButton");

    summaryTextEl.textContent = `${data.summary.scene_count.toLocaleString()} extracted scene nodes, ${data.summary.image_count.toLocaleString()} copied images`;

    function escapeHtml(value) {
      return value
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function makeMetaChip(text) {
      const span = document.createElement("span");
      span.className = "chip";
      span.textContent = text;
      return span;
    }

    function renderSceneList() {
      const query = searchInputEl.value.trim().toLowerCase();
      const filtered = nodes.filter(node => {
        if (!query) return true;
        const haystack = [
          node.title,
          node.function_name,
          node.relative_path,
          ...(node.text_blocks || []).slice(0, 4).map(block => block.preview || block.template || ""),
          ...(node.notes || []),
        ].join(" ").toLowerCase();
        return haystack.includes(query);
      });

      filterCountEl.textContent = `${filtered.length.toLocaleString()} scenes`;
      sceneListEl.innerHTML = "";

      if (!filtered.length) {
        const empty = document.createElement("div");
        empty.className = "empty-note";
        empty.textContent = "No matches yet.";
        sceneListEl.appendChild(empty);
        return;
      }

      for (const node of filtered) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "scene-row" + (node.id === currentNodeId ? " active" : "");
        button.addEventListener("click", () => openNode(node.id, true));

        const title = document.createElement("div");
        title.className = "scene-title";
        title.textContent = node.title || node.function_name;
        button.appendChild(title);

        const path = document.createElement("div");
        path.className = "scene-path meta";
        path.textContent = `${node.relative_path}:${node.start_line}`;
        button.appendChild(path);

        const preview = document.createElement("div");
        preview.className = "scene-preview";
        const firstText = (node.text_blocks || []).find(block => (block.preview || "").trim());
        preview.textContent = firstText ? firstText.preview : (node.notes && node.notes[0]) || "No text in this node.";
        button.appendChild(preview);

        sceneListEl.appendChild(button);
      }
    }

    function createChunk(block) {
      const wrap = document.createElement("section");
      wrap.className = block.kind === "code" ? "note-card" : "chunk";

      const meta = document.createElement("div");
      meta.className = "meta";
      const contextBits = (block.contexts || []).length ? ` · ${(block.contexts || []).join(" · ")}` : "";
      meta.textContent = `${block.kind} · line ${block.line}${contextBits}`;
      wrap.appendChild(meta);

      if (block.kind === "code") {
        const code = document.createElement("pre");
        code.className = "code-block";
        code.textContent = block.template;
        wrap.appendChild(code);
      } else {
        const pre = document.createElement("pre");
        pre.textContent = block.template;
        wrap.appendChild(pre);
      }

      return wrap;
    }

    function createImageSection(images) {
      const section = document.createElement("section");
      const title = document.createElement("div");
      title.className = "section-title";
      title.textContent = "Images";
      section.appendChild(title);

      for (const image of images) {
        const card = document.createElement("div");
        card.className = "image-card";

        const meta = document.createElement("div");
        meta.className = "meta";
        const labels = image.labels && image.labels.length ? image.labels.join(", ") : "Dynamic image call";
        const contextBits = (image.contexts || []).length ? ` · ${(image.contexts || []).join(" · ")}` : "";
        meta.textContent = `${image.kind} · ${labels} · line ${image.line}${contextBits}`;
        card.appendChild(meta);

        if (image.unresolved && image.unresolved.length) {
          const unresolved = document.createElement("div");
          unresolved.className = "status-line";
          unresolved.textContent = `Dynamic refs: ${image.unresolved.join(", ")}`;
          card.appendChild(unresolved);
        }

        const resolved = image.resolved || [];
        if (!resolved.length) {
          const none = document.createElement("div");
          none.className = "status-line";
          none.textContent = "No static image file matched this call.";
          card.appendChild(none);
        } else {
          const grid = document.createElement("div");
          grid.className = "image-grid";
          for (const item of resolved) {
            const frame = document.createElement("figure");
            frame.className = "image-frame";

            const img = document.createElement("img");
            img.loading = "lazy";
            img.src = item.export_path;
            img.alt = item.symbol || item.path;
            frame.appendChild(img);

            const caption = document.createElement("figcaption");
            caption.className = "image-caption";
            caption.textContent = item.relative_image_path || item.path;
            frame.appendChild(caption);
            grid.appendChild(frame);
          }
          card.appendChild(grid);
        }

        section.appendChild(card);
      }

      return section;
    }

    function createChoicesSection(node) {
      const section = document.createElement("section");
      const title = document.createElement("div");
      title.className = "section-title";
      title.textContent = "Choices";
      section.appendChild(title);

      const choices = document.createElement("div");
      choices.className = "choices";

      for (const button of node.buttons || []) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "choice-btn";
        btn.textContent = button.label;
        if (button.disabled) {
          btn.disabled = true;
          btn.title = "Disabled in source";
        } else if (button.target_id && nodeMap.has(button.target_id)) {
          btn.addEventListener("click", () => openNode(button.target_id, true));
        } else {
          btn.addEventListener("click", () => alert("This choice points at game code that couldn't be mapped cleanly in the export."));
        }
        choices.appendChild(btn);
      }

      if (!(node.buttons || []).length) {
        const none = document.createElement("div");
        none.className = "status-line";
        none.textContent = "No buttons were found in this node.";
        section.appendChild(none);
      } else {
        section.appendChild(choices);
      }

      return section;
    }

    function openNode(nodeId, pushHistory) {
      const node = nodeMap.get(nodeId);
      if (!node) return;
      if (pushHistory && currentNodeId && currentNodeId !== nodeId) {
        historyStack.push(currentNodeId);
      }
      currentNodeId = nodeId;

      const title = node.title || node.function_name;
      viewerTitleWrapEl.innerHTML = "";

      const eyebrow = document.createElement("div");
      eyebrow.className = "eyebrow";
      eyebrow.textContent = "Scene";
      viewerTitleWrapEl.appendChild(eyebrow);

      const h1 = document.createElement("h1");
      h1.textContent = title;
      viewerTitleWrapEl.appendChild(h1);

      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${node.relative_path}:${node.start_line} · ${node.function_name}`;
      viewerTitleWrapEl.appendChild(meta);

      const chips = document.createElement("div");
      chips.className = "chips";
      chips.appendChild(makeMetaChip(`Buttons: ${(node.buttons || []).length}`));
      chips.appendChild(makeMetaChip(`Text blocks: ${(node.text_blocks || []).length}`));
      chips.appendChild(makeMetaChip(`Images: ${(node.images || []).length}`));
      if (node.derived_from) {
        chips.appendChild(makeMetaChip(`Derived from ${node.derived_from}`));
      }
      if (node.bound_args && Object.keys(node.bound_args).length) {
        chips.appendChild(makeMetaChip(`Args ${JSON.stringify(node.bound_args)}`));
      }
      viewerTitleWrapEl.appendChild(chips);

      viewerBodyEl.innerHTML = "";

      if ((node.notes || []).length) {
        for (const note of node.notes) {
          const noteCard = document.createElement("section");
          noteCard.className = "note-card";
          const noteText = document.createElement("div");
          noteText.textContent = note;
          noteCard.appendChild(noteText);
          viewerBodyEl.appendChild(noteCard);
        }
      }

      for (const block of node.text_blocks || []) {
        viewerBodyEl.appendChild(createChunk(block));
      }

      if ((node.images || []).length) {
        viewerBodyEl.appendChild(createImageSection(node.images));
      }

      viewerBodyEl.appendChild(createChoicesSection(node));
      renderSceneList();
    }

    searchInputEl.addEventListener("input", renderSceneList);
    backButtonEl.addEventListener("click", () => {
      const previous = historyStack.pop();
      if (previous) openNode(previous, false);
    });
    homeButtonEl.addEventListener("click", () => {
      if (nodes.length) openNode(nodes[0].id, true);
    });

    renderSceneList();
    if (currentNodeId) openNode(currentNodeId, false);
  </script>
</body>
</html>
"""


def build_gallery_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Eggplant Image Gallery</title>
  <style>
    :root {
      --bg: #f4efe7;
      --panel: #fffaf2;
      --ink: #2a211a;
      --muted: #6b5a4d;
      --line: #d7c5b4;
      --accent: #b44f2b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
      color: var(--ink);
      background: linear-gradient(180deg, #f8f2ea 0%, #efe7dc 100%);
    }
    .wrap {
      max-width: 1440px;
      margin: 0 auto;
      padding: 28px 22px 40px;
    }
    .top {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 12px;
      align-items: end;
      margin-bottom: 20px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 36px;
    }
    .subtext {
      color: var(--muted);
      margin: 0;
    }
    input {
      min-width: 280px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 12px 14px;
      font: inherit;
      background: rgba(255,255,255,0.85);
    }
    .actions a {
      color: var(--accent);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
    }
    .card {
      border: 1px solid var(--line);
      border-radius: 22px;
      background: rgba(255,255,255,0.7);
      padding: 12px;
    }
    .card img {
      width: 100%;
      height: 260px;
      object-fit: contain;
      background: rgba(0,0,0,0.04);
      border-radius: 14px;
      display: block;
    }
    .meta {
      margin-top: 10px;
      font-size: 12px;
      color: var(--muted);
      word-break: break-word;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>Image Gallery</h1>
        <p class="subtext" id="gallerySummary"></p>
      </div>
      <div class="actions">
        <input id="gallerySearch" type="search" placeholder="Search image paths">
        <p class="subtext"><a href="./index.html">Back to scene browser</a></p>
      </div>
    </div>
    <div class="grid" id="galleryGrid"></div>
  </div>
  <script src="./data.js"></script>
  <script>
    const data = window.EGGPLANT_DATA;
    const images = data.images;
    const grid = document.getElementById("galleryGrid");
    const search = document.getElementById("gallerySearch");
    const summary = document.getElementById("gallerySummary");

    summary.textContent = `${images.length.toLocaleString()} copied images`;

    function render() {
      const query = search.value.trim().toLowerCase();
      const filtered = images.filter(item => !query || item.id.toLowerCase().includes(query) || item.relative_path.toLowerCase().includes(query));
      grid.innerHTML = "";
      for (const item of filtered) {
        const card = document.createElement("figure");
        card.className = "card";

        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = item.export_path;
        img.alt = item.id;
        card.appendChild(img);

        const meta = document.createElement("figcaption");
        meta.className = "meta";
        meta.textContent = `${item.id} · ${item.human_size}`;
        card.appendChild(meta);

        grid.appendChild(card);
      }
      summary.textContent = `${filtered.length.toLocaleString()} of ${images.length.toLocaleString()} copied images`;
    }

    search.addEventListener("input", render);
    render();
  </script>
</body>
</html>
"""


def copy_images(source_root: Path, export_root: Path, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    images_dir = export_root / "images"
    for item in images:
        source_path = Path(item["absolute_path"])
        relative = Path(item["id"])
        target_path = images_dir / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        updated = dict(item)
        updated["export_path"] = normalize_path(Path("images") / relative)
        updated["human_size"] = human_bytes(item["size_bytes"])
        copied.append(updated)
    return copied


def reference_images_directly(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    referenced: list[dict[str, Any]] = []
    for item in images:
        updated = dict(item)
        updated["export_path"] = Path(item["absolute_path"]).as_uri()
        updated["human_size"] = human_bytes(item["size_bytes"])
        referenced.append(updated)
    return referenced


def attach_export_paths_to_nodes(nodes: list[SceneNode], copied_images: list[dict[str, Any]]) -> None:
    export_path_index = {item["absolute_path"]: item["export_path"] for item in copied_images}
    for node in nodes:
        for image in node.images:
            for resolved in image.get("resolved", []):
                resolved["export_path"] = export_path_index.get(resolved["path"], "")


def build_payload(nodes: list[SceneNode], copied_images: list[dict[str, Any]], source_root: Path) -> dict[str, Any]:
    total_text_blocks = sum(len(node.text_blocks) for node in nodes)
    total_buttons = sum(len(node.buttons) for node in nodes)
    return {
        "source_root": normalize_path(source_root),
        "summary": {
            "scene_count": len(nodes),
            "text_block_count": total_text_blocks,
            "button_count": total_buttons,
            "image_count": len(copied_images),
        },
        "nodes": serialize_nodes(nodes),
        "images": copied_images,
    }


def run(source_root: Path, export_root: Path, folder_names: Sequence[str], copy_image_files: bool) -> None:
    if export_root.exists():
        shutil.rmtree(export_root)
    export_root.mkdir(parents=True, exist_ok=True)

    print("Building image maps...")
    bust_map, image_map = build_bust_and_image_maps(source_root)
    print("Building scene nodes...")
    nodes = build_scene_nodes(source_root, bust_map, image_map, folder_names)
    print(f"Scene nodes built: {len(nodes)}")
    print("Collecting image files...")
    all_images = collect_all_images(source_root)
    print(f"Images found: {len(all_images)}")
    if copy_image_files:
        print("Copying image files...")
        copied_images = copy_images(source_root, export_root, all_images)
    else:
        print("Linking image files in place...")
        copied_images = reference_images_directly(all_images)
    print("Attaching export paths...")
    attach_export_paths_to_nodes(nodes, copied_images)
    print("Writing viewer files...")
    payload = build_payload(nodes, copied_images, source_root)

    (export_root / "data.js").write_text(build_data_js(payload), encoding="utf-8")
    (export_root / "index.html").write_text(build_index_html(), encoding="utf-8")
    (export_root / "image-gallery.html").write_text(build_gallery_html(), encoding="utf-8")

    summary_lines = [
        "Eggplant story export complete.",
        f"Source: {normalize_path(source_root)}",
        f"Scenes: {payload['summary']['scene_count']}",
        f"Text blocks: {payload['summary']['text_block_count']}",
        f"Buttons: {payload['summary']['button_count']}",
        f"{'Copied' if copy_image_files else 'Linked'} images: {payload['summary']['image_count']}",
        f"Open: {normalize_path(export_root / 'index.html')}",
    ]
    (export_root / "README.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print("Done.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a clickable TiTS story export for the eggplant project.")
    parser.add_argument("--source-root", required=True, help="Path to the TiTS source folder.")
    parser.add_argument("--output", required=True, help="Where to write the export folder.")
    parser.add_argument(
        "--folders",
        nargs="+",
        default=["includes"],
        help="Top-level source folders to scan for scene code. Default: includes",
    )
    parser.add_argument(
        "--copy-images",
        action="store_true",
        help="Copy image files into the export instead of linking to the originals.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(
        Path(args.source_root).resolve(),
        Path(args.output).resolve(),
        args.folders,
        args.copy_images,
    )


if __name__ == "__main__":
    main()
