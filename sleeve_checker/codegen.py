"""codegen.py — generate check functions from free-text descriptions.

A user writes a check criterion in plain Japanese; ``generate_check_code``
asks an LLM (OpenAI gpt-4o-mini, same setup as layer_classifier) to emit a
Python function::

    def check(floor, ctx):
        ...
        return [ctx.result(severity="NG", sleeve=s, message="...")]

The generated source is validated by AST (no imports, no dunders, no
dangerous builtins) before it is ever stored, and executed in a restricted
namespace by ``run_generated_check``.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from .models import CheckResult, FloorData


class CodegenError(Exception):
    """Raised when generation or validation of a check fails."""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
あなたは建築設備のスリーブ施工図をチェックする Python 関数を書くエキスパートです。
ユーザーが日本語で「チェック基準」を書きます。あなたはその基準を判定する関数を
**Python コードだけ**で出力します。説明文・マークダウン・コードフェンスは禁止です。

必ず次のシグネチャの関数 1 つだけを定義してください:

    def check(floor, ctx):
        results = []
        ...
        return results

## floor: FloorData（チェック対象の階）
- floor.sleeves: list[Sleeve]
- floor.grid_lines: list[GridLine]      # axis_label:str, direction:"H"|"V", position:float
- floor.dim_lines: list[DimLine]        # measurement:float, defpoint1/2/3:(x,y), angle, text_override
- floor.wall_lines: list[WallLine]      # start,end:(x,y), material:str, is_exterior:bool
- floor.step_lines: list[StepLine]      # start,end:(x,y)
- floor.column_lines: list[ColumnLine]  # start,end:(x,y)
- floor.recess_polygons: list[RecessPolygon]  # vertices:list[(x,y)]
- floor.slab_labels: list[SlabLabel]    # x,y, slab_no, level, thickness
- floor.pn_labels: list[PnLabel]        # x,y, text, number
- floor.water_gradients: list[WaterGradient]  # x,y, direction

### Sleeve のフィールド
- s.id:str, s.center:(x,y), s.diameter:float
- s.label_text, s.diameter_text, s.fl_text: str|None  （図面のラベル文字）
- s.pn_number: str|None   （例 "P-N-12"）
- s.discipline: str       （"衛生"/"空調"/"電気"/"その他"）
- s.shape: "round"|"rect", s.width, s.height
- s.orientation: "vertical"|"horizontal"|""

## ctx: ヘルパー
- ctx.re, ctx.math
- ctx.point_to_segment_distance(point, seg_start, seg_end) -> float
- ctx.points_match(p1, p2, tol=5.0) -> bool
- ctx.point_in_polygon(point, vertices) -> bool
- ctx.point_on_any_segment(point, segments, tol=5.0) -> bool
- ctx.lower_walls            # 直下階の wall_lines（無ければ空リスト）
- ctx.wall_thickness         # dict[str,float] 材質→壁厚mm
- ctx.result(severity=..., sleeve=..., message=..., related_coords=..., rule=..., expected=..., found=..., fix_hint=...)
  severity は "OK" / "NG" / "WARNING"。スリーブ単位の判定では sleeve= に対象 Sleeve を渡す。

## ルール
- import 文は書かない（ctx.re / ctx.math を使う）。ファイル・ネットワーク・eval/exec は禁止。
- 各スリーブを判定する場合は floor.sleeves をループし、OK の場合も ctx.result(severity="OK", sleeve=s, ...) を返すと一覧で件数が見える。
- 図面全体の判定なら sleeve=None のまま 1 件以上返す。
- 必ず list を返す。
"""

_FEWSHOT_USER = "スリーブ近傍のラベルに設備種別が記載されていること。"
_FEWSHOT_ASSISTANT = '''\
def check(floor, ctx):
    results = []
    for s in floor.sleeves:
        if s.label_text and s.label_text.strip():
            results.append(ctx.result(severity="OK", sleeve=s, message="設備種別記載あり"))
        else:
            results.append(ctx.result(
                severity="NG", sleeve=s, message="設備種別ラベルなし",
                rule="スリーブ近傍のラベルに設備種別が記載されていること",
                found="ラベルなし", fix_hint="設備種別コードを含むラベルを追記する",
            ))
    return results'''


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove ```python ... ``` fences if the model added them anyway."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    return t.strip()


def generate_check_code(name: str, description: str) -> str:
    """Generate and validate a check function. Returns the source string.

    Raises CodegenError on LLM failure or validation failure.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    import os
    if not os.getenv("OPENAI_API_KEY"):
        raise CodegenError("OPENAI_API_KEY が設定されていません。")

    try:
        from openai import OpenAI
    except ImportError as e:
        raise CodegenError(f"openai SDK が見つかりません: {e}")

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()
    user = f"チェック名: {name}\nチェック基準:\n{description}"

    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _FEWSHOT_USER},
                {"role": "assistant", "content": _FEWSHOT_ASSISTANT},
                {"role": "user", "content": user},
            ],
        )
        code = resp.choices[0].message.content or ""
    except Exception as e:
        raise CodegenError(f"LLM 呼び出し失敗: {type(e).__name__}: {e}")

    code = _strip_fences(code)
    validate_check_code(code)
    return code


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_FORBIDDEN_CALLS = {
    "open", "exec", "eval", "compile", "__import__",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "input",
}


def validate_check_code(code: str) -> None:
    """Raise CodegenError unless *code* is a safe ``def check(floor, ctx)``."""
    if not code.strip():
        raise CodegenError("生成コードが空です。")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise CodegenError(f"構文エラー: {e}")

    # Must define a top-level `check` function taking (floor, ctx).
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    check_fn = next((f for f in funcs if f.name == "check"), None)
    if check_fn is None:
        raise CodegenError("トップレベルに def check(floor, ctx) が必要です。")
    arg_names = [a.arg for a in check_fn.args.args]
    if arg_names[:2] != ["floor", "ctx"]:
        raise CodegenError("check の引数は (floor, ctx) である必要があります。")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise CodegenError("import は使用できません（ctx.re / ctx.math を使う）。")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise CodegenError(f"ダンダー属性は禁止です: {node.attr}")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise CodegenError(f"ダンダー名は禁止です: {node.id}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALLS:
                raise CodegenError(f"使用禁止の関数: {node.func.id}")


# ---------------------------------------------------------------------------
# Sandbox execution
# ---------------------------------------------------------------------------

_SAFE_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in (
        "len", "range", "min", "max", "abs", "round", "sorted", "enumerate",
        "zip", "list", "dict", "set", "tuple", "float", "int", "str", "bool",
        "any", "all", "sum", "isinstance", "map", "filter", "reversed",
        "True", "False", "None",
    )
    if name in (__builtins__ if isinstance(__builtins__, dict) else dir(__builtins__))
}


def run_generated_check(code: str, floor: FloorData, ctx: Any) -> list[CheckResult]:
    """Execute a generated check in a restricted namespace and return results.

    Validation is re-run defensively. Any execution error propagates to the
    caller (the registry runner wraps each check in try/except).
    """
    validate_check_code(code)
    sandbox_globals: dict[str, Any] = {
        "__builtins__": dict(_SAFE_BUILTINS),
    }
    exec(compile(code, "<generated_check>", "exec"), sandbox_globals)
    fn = sandbox_globals.get("check")
    if not callable(fn):
        raise CodegenError("check 関数が定義されていません。")
    out = fn(floor, ctx)
    if not isinstance(out, list):
        raise CodegenError("check は list を返す必要があります。")
    return [r for r in out if isinstance(r, CheckResult)]
